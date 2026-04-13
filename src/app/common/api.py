import hashlib
import json
import math
import os
import queue
import random
import re
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .database import Database, UPLOAD_PART_SIZE, _safe_int, _safe_float, get_upload_part_size
from .concurrency import (
    RATE_LIMIT_CODES, MAX_RATE_LIMITS, RATE_LIMIT_BACKOFF,
    PROGRESS_INTERVAL, slow_start_scheduler, _ProgressAggregator,
)
from .const import all_device_type, all_os_versions
from .log import get_logger
from .filename_utils import sanitize_filename

logger = get_logger(__name__)


class _RWLock:
    """简易读写锁：多读并行，写独占。"""

    def __init__(self):
        self._cond = threading.Condition(threading.Lock())
        self._readers = 0
        self._writer = False

    @contextmanager
    def rlock(self):
        with self._cond:
            while self._writer:
                self._cond.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._cond:
                self._readers -= 1
                if self._readers == 0:
                    self._cond.notify_all()

    @contextmanager
    def wlock(self):
        with self._cond:
            while self._writer or self._readers > 0:
                self._cond.wait()
            self._writer = True
        try:
            yield
        finally:
            with self._cond:
                self._writer = False
                self._cond.notify_all()


INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 30.0
BACKOFF_MULTIPLIER = 2
MAX_CREATE_DIR_RETRIES = 10


class _ProgressFileIO:
    """按需从文件读取并上报进度的 file-like 对象，零预分配。"""
    _REPORT_SIZE = 256 * 1024

    def __init__(self, file_path, offset, size, callback):
        self._f = open(file_path, "rb")  # noqa: SIM115
        self._f.seek(offset)
        self._size = size
        self._remaining = size
        self._cb = callback
        self._pending = 0
        self.reported = 0

    def read(self, size=-1):
        if self._remaining <= 0:
            if self._pending > 0:
                self._cb(self._pending)
                self.reported += self._pending
                self._pending = 0
            return b""
        if size < 0 or size > self._remaining:
            size = self._remaining
        chunk = self._f.read(size)
        if chunk:
            self._remaining -= len(chunk)
            if self._remaining < 0:
                logger.warning("_ProgressFileIO: _remaining 变为负值 (%d)", self._remaining)
            self._pending += len(chunk)
            if self._pending >= self._REPORT_SIZE:
                self._cb(self._pending)
                self.reported += self._pending
                self._pending = 0
        elif self._remaining > 0:
            logger.warning(
                "_ProgressFileIO: 文件在 offset=%d 处提前结束 (剩余 %d 字节未读)",
                self._f.tell(), self._remaining,
            )
        return chunk

    def __len__(self):
        return self._size

    def close(self):
        if self._f and not self._f.closed:
            self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False


class RateLimitError(RuntimeError):
    """API 返回 HTTP 429 限流。"""


class TokenExpiredError(RuntimeError):
    """token 过期且无法自动刷新（QR 登录无密码）。"""


def _parse_json_response(response):
    """统一解析 JSON 响应，避免裸 .json() 崩溃。"""
    try:
        return response.json()
    except (ValueError, KeyError) as exc:
        raise RuntimeError(
            f"JSON 解析失败 (HTTP {response.status_code}): {response.text[:200]}"
        ) from exc
    finally:
        response.close()


def _calculate_file_md5(
    file_path, fsize, task=None, signals=None, speed_tracker=None,
    emit_progress=True,
):
    md5 = hashlib.md5()
    bytes_read = 0
    last_pct = -1
    with open(file_path, "rb") as f:
        while True:
            data = f.read(1024 * 1024)
            if not data:
                break
            md5.update(data)
            bytes_read += len(data)
            if task and getattr(task, "is_cancelled", False):
                return "已取消", ""
            if task and getattr(task, "pause_requested", False):
                return "已暂停", ""
            if emit_progress and fsize > 0:
                pct = int(bytes_read * 100 / fsize)
                if pct != last_pct:
                    last_pct = pct
                    if signals:
                        signals.progress.emit(pct)
                    if speed_tracker:
                        speed_tracker.record(bytes_read)
    return None, md5.hexdigest()


def _reset_transient_failure_count(counter):
    counter[0] = 0


class Pan123:
    """123云盘API客户端类"""

    def __init__(
        self,
        readfile=True,
        user_name="",
        password="",
        authorization="",
        input_pwd=False,
    ):

        # 设备信息（优先从配置读取，否则随机生成）
        self.devicetype = random.choice(all_device_type)
        self.osversion = random.choice(all_os_versions)
        self.loginuuid = uuid.uuid4().hex

        self.on_token_expired = None  # callback: 通知 UI token 过期需重新登录
        self.recycle_list = None
        self.parent_file_name_list = []
        self.all_file = False
        self.file_page = 0
        # 创建带重试的 session，仅在网络错误时重试
        db = Database.instance()
        retry = Retry(
            total=3,
            backoff_factor=_safe_float(db.get_config("retryBackoffFactor", 0.5), 0.5, 0.1, 10.0),
            allowed_methods=["GET", "PUT", "HEAD"],
            raise_on_status=False,
            status_forcelist=[500, 502, 504],
        )
        adapter = HTTPAdapter(pool_connections=16, pool_maxsize=32, max_retries=retry)
        self.session = requests.Session()
        self.session.mount("https://", adapter)
        self._session_lock = _RWLock()
        if readfile:
            self.read_ini(user_name, password, input_pwd, authorization)
        else:
            if not authorization and (user_name == "" or password == ""):
                raise Exception("用户名或密码为空")
            self.user_name = user_name
            self.password = password
            self.authorization = authorization
        self.header_logined = {
            "user-agent": "123pan/v2.4.0(" + self.osversion + ";Xiaomi)",
            "authorization": self.authorization,
            "accept-encoding": "gzip",
            "content-type": "application/json",
            "osversion": self.osversion,
            "loginuuid": self.loginuuid,
            "platform": "android",
            "devicetype": self.devicetype,
            "devicename": "Xiaomi",
            "app-version": "61",
            "x-app-version": "2.4.0",
        }
        self.parent_file_id = 0  # 路径，文件夹的id,0为根目录
        self.parent_file_list = [0]
        self._login_lock = threading.Lock()

    def close(self):
        """关闭 session，释放连接池资源。"""
        try:
            self.session.close()
        except Exception:
            pass

    def login(self):
        with self._login_lock:
            return self._login_without_lock()

    def _login_without_lock(self):
        """登录123云盘账户并获取授权令牌。
        注意：直接使用 session.post 而非 _raw_request，
        因为调用方 _refresh_token_for_request 已持有 wlock，
        _raw_request 会尝试获取 rlock 导致死锁（_RWLock 不可重入）。
        """
        data = {"type": 1, "passport": self.user_name, "password": self.password}
        login_res = self.session.post(
            "https://www.123pan.com/b/api/user/sign_in",
            headers=self.header_logined,
            data=json.dumps(data),
            timeout=(5, 15),
        )

        res_sign = _parse_json_response(login_res)
        res_code_login = res_sign.get("code", -1)
        if res_code_login != 200:
            logger.error("code = 1 Error: %s", res_code_login)
            logger.error(res_sign.get("message", ""))
            return res_code_login

        token = res_sign.get("data", {}).get("token", "")
        if not token:
            logger.error("登录响应缺少 token")
            return -1
        self.authorization = "Bearer " + token
        self.header_logined["authorization"] = self.authorization
        self.save_file()
        return res_code_login

    def _prepare_request_kwargs(self, kwargs):
        request_kwargs = dict(kwargs)
        headers = request_kwargs.get("headers")
        if headers is None:
            request_kwargs["headers"] = self.header_logined
            return request_kwargs

        merged_headers = dict(headers)
        merged_headers["authorization"] = self.header_logined["authorization"]
        request_kwargs["headers"] = merged_headers
        return request_kwargs

    def _refresh_token_for_request(self, request_authorization):
        # 密码为空时无法通过重新登录刷新 token
        if not self.password:
            if self.on_token_expired:
                self.on_token_expired()
            raise TokenExpiredError("token 过期且无保存密码，无法自动刷新")
        with self._session_lock.wlock():
            with self._login_lock:
                current_authorization = self.header_logined["authorization"]
                if request_authorization != current_authorization:
                    return
                login_code = self._login_without_lock()
            if login_code not in (0, 200):
                raise RuntimeError(f"token 刷新失败: {login_code}")

    def _raw_request(self, method, url, **kwargs):
        """基础请求包装：_session_lock + 限流检测。"""
        with self._session_lock.rlock():
            response = method(url, **kwargs)
        if response.status_code in RATE_LIMIT_CODES:
            raise RateLimitError(f"API 返回 {response.status_code} 限流: {url}")
        return response

    def _api_request(self, method, url, max_token_refreshes=1, **kwargs):
        token_refreshes = 0
        while True:
            request_kwargs = self._prepare_request_kwargs(kwargs)
            request_headers = request_kwargs.get("headers", {})
            request_authorization = request_headers.get("authorization")
            response = self._raw_request(method, url, **request_kwargs)
            try:
                response_json = response.json()
            except ValueError:
                return response
            if response_json.get("code") != 2 or token_refreshes >= max_token_refreshes:
                return response
            self._refresh_token_for_request(request_authorization)
            token_refreshes += 1

    def save_file(self):
        """将账户信息保存到配置文件"""
        try:
            db = Database.instance()
            remember_password = bool(db.get_config("rememberPassword", False))
            stay_logged_in = bool(db.get_config("stayLoggedIn", True))
            db.set_many_config({
                "userName": self.user_name,
                "passWord": "",
                "authorization": "",
                "deviceType": self.devicetype,
                "osVersion": self.osversion,
                "loginuuid": self.loginuuid,
            })
            from .credential_store import save_credential, delete_credential
            if remember_password:
                save_credential("passWord", self.password)
            else:
                delete_credential("passWord")
            if stay_logged_in:
                save_credential("authorization", self.authorization)
            else:
                delete_credential("authorization")
            logger.info("账号已保存")
        except Exception as e:
            logger.error("保存账号失败: %s", e)

    def get_dir_by_id(self, file_id, all=False, limit=100, search_data=""):
        """按文件夹ID获取文件列表（支持分页）"""
        get_pages = 3
        page = 1
        length_now = 0
        lists = []
        total = -1
        times = 0
        while (length_now < total or total == -1) and (times < get_pages or all):
            base_url = "https://www.123pan.com/b/api/file/list/new"
            params = {
                "driveId": 0,
                "limit": limit,
                "next": 0,
                "orderBy": "file_id",
                "orderDirection": "desc",
                "parentFileId": str(file_id),
                "trashed": False,
                "SearchData": search_data,
                "Page": str(page),
                "OnlyLookAbnormalFile": 0,
            }
            try:
                response = self._api_request(
                    self.session.get,
                    base_url, headers=self.header_logined, params=params, timeout=30
                )
            except RateLimitError:
                logger.error("获取文件列表触发 429 限流")
                return -1, []
            except requests.exceptions.Timeout:
                logger.error("请求超时: %s", base_url)
                return -1, []
            except requests.exceptions.ConnectionError as e:
                logger.error("连接失败: %s", e)
                return -1, []
            except requests.exceptions.RequestException as e:
                logger.error("请求异常: %s", e)
                return -1, []
            text = _parse_json_response(response)
            res_code_getdir = text.get("code", -1)
            if res_code_getdir != 0:
                logger.error("code = 2 Error: %s", res_code_getdir)
                logger.error(text.get("message", ""))
                return res_code_getdir, []
            data = text.get("data") or {}
            lists_page = data.get("InfoList") or []
            lists += lists_page
            if total <= 0:
                total = data.get("Total", 0)
            length_now += len(lists_page)
            page += 1
            times += 1
            if times % 5 == 0:
                logger.warning(
                    "警告：文件夹内文件过多: %s/%s", length_now, total
                )
                logger.info("为防止对服务器造成影响，暂停3秒")
                time.sleep(3)

        return res_code_getdir, lists

    def link_by_fileDetail(self, file_detail, showlink=True):
        """按文件详情获取下载链接"""
        type_detail = file_detail.get("Type", 0)

        if type_detail == 1:
            down_request_url = "https://www.123pan.com/a/api/file/batch_download_info"
            down_request_data = {"fileIdList": [{"fileId": int(file_detail.get("FileId", 0))}]}

        else:
            down_request_url = "https://www.123pan.com/a/api/file/download_info"
            down_request_data = {
                "driveId": 0,
                "etag": file_detail.get("Etag", ""),
                "fileId": file_detail.get("FileId", 0),
                "s3keyFlag": file_detail.get("S3KeyFlag", False),
                "type": file_detail.get("Type", 0),
                "fileName": file_detail.get("FileName", ""),
                "size": file_detail.get("Size", 0),
            }

        link_res = self._api_request(
            self.session.post,
            down_request_url,
            headers=self.header_logined,
            data=json.dumps(down_request_data),
            timeout=10,
        )
        try:
            link_res_json = _parse_json_response(link_res)
        finally:
            link_res.close()
        res_code_download = link_res_json.get("code", -1)
        if res_code_download != 0:
            logger.error("获取下载链接失败，返回码: %s", res_code_download)
            logger.error(link_res_json.get("message", ""))
            return res_code_download
        down_load_url = link_res_json.get("data", {}).get("DownloadUrl", "")
        if not down_load_url:
            logger.error("响应中缺少 DownloadUrl")
            return -1
        with self._raw_request(
            self.session.get, down_load_url, timeout=10, allow_redirects=False
        ) as next_resp:
            next_to_get = next_resp.text
        url_pattern = re.compile(r"""href=["'](https?://[^"']+)["']""")
        matches = url_pattern.findall(next_to_get)
        if not matches:
            logger.error("未找到重定向链接，响应内容: %s", next_to_get[:200])
            return -1
        redirect_url = matches[0]
        if showlink:
            parsed = urlparse(redirect_url)
            logger.info("获取下载链接成功: %s", parsed.hostname)

        return redirect_url

    def recycle(self):
        """获取回收站列表"""
        recycle_id = 0
        url = (
            "https://www.123pan.com/a/api/file/list/new?driveId=0&limit=100&next=0"
            "&orderBy=fileId&orderDirection=desc&parentFileId="
            + str(recycle_id)
            + "&trashed=true&Page=1"
        )
        recycle_res = self._api_request(
            self.session.get,
            url,
            headers=self.header_logined,
            timeout=10,
        )
        json_recycle = _parse_json_response(recycle_res)
        recycle_list = json_recycle.get("data", {}).get("InfoList", [])
        self.recycle_list = recycle_list

    def delete_file(self, file_detail, operation=True):
        """删除或恢复文件"""
        data_delete = {
            "driveId": 0,
            "fileTrashInfoList": [file_detail],
            "operation": operation,
        }
        delete_res = self._api_request(
            self.session.post,
            "https://www.123pan.com/a/api/file/trash",
            data=json.dumps(data_delete),
            headers=self.header_logined,
            timeout=10,
        )
        dele_json = _parse_json_response(delete_res)
        if dele_json.get("code", -1) != 0:
            raise RuntimeError(
                "删除文件失败: " + json.dumps(dele_json, ensure_ascii=False)
            )
        logger.debug("删除文件响应: %s", dele_json)
        message = dele_json.get("message", "")
        logger.info("删除文件消息: %s", message)

    def rename_file(self, file_id, new_name):
        """重命名文件或文件夹"""
        data = {"driveId": 0, "fileId": file_id, "fileName": new_name}
        rename_res = self._api_request(
            self.session.post,
            "https://www.123pan.com/a/api/file/rename",
            data=json.dumps(data),
            headers=self.header_logined,
            timeout=10,
        )
        rename_json = _parse_json_response(rename_res)
        code = rename_json.get("code", -1)
        logger.debug("重命名文件响应: %s", rename_json)
        if code != 0:
            message = rename_json.get("message", "")
            logger.error("重命名失败: %s", message)
            return False
        logger.info("重命名成功: %s", new_name)
        return True

    def move_file(self, file_id_list, target_parent_id):
        """移动文件或文件夹到目标目录"""
        data = {
            "fileIdList": [{"FileId": fid} for fid in file_id_list],
            "parentFileId": target_parent_id,
        }
        move_res = self._api_request(
            self.session.post,
            "https://www.123pan.com/b/api/file/mod_pid",
            data=json.dumps(data),
            headers=self.header_logined,
            timeout=10,
        )
        move_json = _parse_json_response(move_res)
        code = move_json.get("code", -1)
        logger.debug("移动文件响应: %s", move_json)
        if code != 0:
            message = move_json.get("message", "")
            logger.error("移动文件失败: %s", message)
            return False
        logger.info("移动文件成功: %s 个文件", len(file_id_list))
        return True

    def user_info(self):
        """获取用户信息（已用空间、总容量、VIP 等）"""
        res = self._api_request(
            self.session.get,
            "https://api.123pan.cn/b/api/user/info",
            headers=self.header_logined,
            timeout=10,
        )
        res_json = _parse_json_response(res)
        code = res_json.get("code", -1)
        if code != 0:
            message = res_json.get("message", "")
            logger.error("获取用户信息失败: %s", message)
            return None
        return res_json.get("data")

    def qr_generate(self):
        """获取二维码登录会话（uniID + url）。"""
        headers = {
            "loginuuid": self.loginuuid,
            "app-version": "3",
            "platform": "web",
            "content-type": "application/json;charset=UTF-8",
        }
        res = self._raw_request(
            self.session.get,
            "https://login.123pan.com/api/user/qr-code/generate",
            headers=headers,
            timeout=10,
        )
        res_json = _parse_json_response(res)
        code = res_json.get("code", -1)
        if code != 0:
            raise RuntimeError(
                f"获取二维码失败: code={code}, "
                f"message={res_json.get('message', '')}"
            )
        data = res_json.get("data", {})
        return {"uniID": data.get("uniID", ""), "url": data.get("url", "")}

    def qr_poll(self, uni_id):
        """轮询二维码扫码状态。

        返回 dict:
        - loginStatus: 0=等待扫码, 1=已扫码待确认, 2=拒绝, 3=确认登录, 4=过期
        - scanPlatform: 4=微信, 7=123云盘App (仅 code=200 时从 login_type 取)
        - token: JWT token (仅 App 扫码确认时直接返回)
        """
        headers = {
            "loginuuid": self.loginuuid,
            "app-version": "3",
            "platform": "web",
            "content-type": "application/json;charset=UTF-8",
        }
        res = self._raw_request(
            self.session.get,
            "https://login.123pan.com/api/user/qr-code/result",
            headers=headers,
            params={"uniID": uni_id},
            timeout=10,
        )
        res_json = _parse_json_response(res)
        code = res_json.get("code", -1)
        data = res_json.get("data", {})

        # code=200 表示用户确认登录（前端映射为 loginStatus=3）
        if code == 200:
            return {
                "loginStatus": 3,
                "scanPlatform": data.get("login_type", 0),
                "token": data.get("token", ""),
            }

        if code != 0:
            raise RuntimeError(
                f"轮询扫码状态失败: code={code}, "
                f"message={res_json.get('message', '')}"
            )
        return {
            "loginStatus": data.get("loginStatus", -1),
            "scanPlatform": data.get("scanPlatform", 0),
        }

    def qr_wx_code(self, uni_id):
        """微信扫码登录：用 uniID 换取 wxCode，再用 wxCode 换 token。"""
        headers = {
            "loginuuid": self.loginuuid,
            "app-version": "3",
            "platform": "web",
            "content-type": "application/json;charset=UTF-8",
        }
        res = self._raw_request(
            self.session.post,
            "https://login.123pan.com/api/user/qr-code/wx_code",
            headers=headers,
            json={"uniID": uni_id},
            timeout=10,
        )
        res_json = _parse_json_response(res)
        code = res_json.get("code", -1)
        if code != 0:
            raise RuntimeError(
                f"获取 wxCode 失败: code={code}, "
                f"message={res_json.get('message', '')}"
            )
        data = res_json.get("data", {})
        return data.get("wxCode", "")

    def file_details(self, file_ids):
        """获取文件/文件夹详情"""
        data = {"file_ids": file_ids}
        res = self._api_request(
            self.session.post,
            "https://www.123pan.com/b/api/restful/goapi/v1/file/details",
            data=json.dumps(data),
            headers=self.header_logined,
            timeout=10,
        )
        res_json = _parse_json_response(res)
        code = res_json.get("code", -1)
        if code != 0:
            message = res_json.get("message", "")
            logger.error("获取文件详情失败: %s", message)
            return None
        data = res_json.get("data")
        logger.debug("file_details 响应 paths: %s", data.get('paths') if data else None)
        return data

    def share(self, file_id_list, share_pwd=""):
        """分享文件"""
        if not file_id_list:
            raise ValueError("文件ID列表为空")
        data = {
            "driveId": 0,
            "expiration": "2099-12-12T08:00:00+08:00",
            "fileIdList": file_id_list,
            "shareName": "123云盘分享",
            "sharePwd": share_pwd or "",
            "event": "shareCreate",
        }
        share_res = self._api_request(
            self.session.post,
            "https://www.123pan.com/a/api/share/create",
            headers=self.header_logined,
            data=json.dumps(data),
            timeout=10,
        )
        share_res_json = _parse_json_response(share_res)
        if share_res_json.get("code", -1) != 0:
            raise RuntimeError(f"分享失败: {share_res_json.get('message', '')}")
        share_key = share_res_json.get("data", {}).get("ShareKey", "")
        if not share_key:
            raise RuntimeError("分享响应缺少 ShareKey")
        share_url = "https://www.123pan.com/s/" + share_key
        return share_url

    def read_ini(
        self,
        user_name,
        password,
        input_pwd,
        authorization="",
    ):
        """从配置文件读取账号信息"""
        try:
            db = Database.instance()
            deviceType = db.get_config("deviceType", "")
            osVersion = db.get_config("osVersion", "")
            loginuuid = db.get_config("loginuuid", "")
            if deviceType:
                self.devicetype = deviceType
            if osVersion:
                self.osversion = osVersion
            if loginuuid:
                self.loginuuid = loginuuid
            user_name = db.get_config("userName", user_name) or user_name
            from .credential_store import load_credential
            password = load_credential("passWord") or password
            authorization = load_credential("authorization") or authorization
        except Exception as e:
            logger.error("获取配置失败: %s", e)
            if user_name == "" or password == "":
                raise RuntimeError("无法从配置获取账号信息") from e

        self.user_name = user_name
        self.password = password
        self.authorization = authorization

    def mkdir(self, dirname, parent_id=None, remakedir=False):
        """创建文件夹"""
        pid = parent_id if parent_id is not None else self.parent_file_id
        if not remakedir:
            code, items = self.get_dir_by_id(pid, limit=100)
            if code == 0:
                for i in items:
                    if i["FileName"] == dirname:
                        return i["FileId"]

        return self._create_directory(pid, dirname)

    def _create_directory(self, parent_id, dirname):
        """在指定父目录下创建文件夹。"""
        url = "https://www.123pan.com/a/api/file/upload_request"
        data_mk = {
            "driveId": 0,
            "etag": "",
            "fileName": dirname,
            "parentFileId": parent_id,
            "size": 0,
            "type": 1,
            "duplicate": 1,
            "NotReuse": True,
            "event": "newCreateFolder",
            "operateType": 1,
        }
        res_mk = self._api_request(
            self.session.post,
            url,
            headers=self.header_logined,
            data=json.dumps(data_mk),
            timeout=10,
        )
        try:
            res_json = res_mk.json()
        except json.decoder.JSONDecodeError:
            logger.error("创建失败")
            logger.error(res_mk.text)
            raise RuntimeError(f"创建目录 '{dirname}' 响应解析失败")
        code_mkdir = res_json.get("code", -1)

        if code_mkdir == 0:
            logger.info("创建成功: %s", res_json.get('data', {}).get('FileId'))
            return res_json.get("data", {}).get("Info", {}).get("FileId")
        logger.error("创建失败: %s", res_json)
        raise RuntimeError(
            f"创建目录 '{dirname}' 失败: code={code_mkdir}, "
            f"message={res_json.get('message', '')}"
        )

    def _create_directory_with_backoff(self, parent_id, dirname):
        """创建目录，遇到 429 时进行指数退避重试。"""
        backoff = INITIAL_BACKOFF_SECONDS
        for attempt in range(MAX_CREATE_DIR_RETRIES + 1):
            try:
                return self._create_directory(parent_id, dirname)
            except RateLimitError as exc:
                if attempt >= MAX_CREATE_DIR_RETRIES:
                    raise RuntimeError(
                        f"创建目录 '{dirname}' 触发 429 限流，"
                        f"已重试 {MAX_CREATE_DIR_RETRIES} 次"
                    ) from exc
                logger.warning(
                    "目录创建 429，退避 %.1fs（第 %d 次）",
                    backoff,
                    attempt + 1,
                )
                time.sleep(backoff)
                backoff = min(backoff * BACKOFF_MULTIPLIER, MAX_BACKOFF_SECONDS)

    def _get_dir_items_by_id(self, parent_id):
        """获取指定目录下的完整文件列表，不污染当前分页状态。"""
        code, items = self.get_dir_by_id(parent_id, all=True, limit=100)
        if code != 0:
            raise RuntimeError(f"获取目录失败，返回码: {code}")
        return items

    def _get_child_directory_map(self, parent_id):
        """返回指定目录下子目录名到目录 ID 的映射。"""
        child_dirs = {}
        for item in self._get_dir_items_by_id(parent_id):
            if int(item.get("Type", 0) or 0) != 1:
                continue
            child_dirs[item.get("FileName", "")] = int(item.get("FileId", 0) or 0)
        return child_dirs

    @staticmethod
    def _choose_available_directory_name(existing_names, dirname):
        """根据已有目录名生成可用名称。"""
        if dirname not in existing_names:
            return dirname

        index = 1
        while True:
            candidate = f"{dirname}({index})"
            if candidate not in existing_names:
                return candidate
            index += 1

    def ensure_directory(self, parent_id, dirname):
        """确保指定父目录下存在目标子目录。"""
        child_dirs = self._get_child_directory_map(parent_id)
        if dirname in child_dirs:
            return child_dirs[dirname]

        created_id = self._create_directory_with_backoff(parent_id, dirname)
        if not created_id:
            raise RuntimeError(f"创建目录失败: {dirname}")
        return created_id

    def prepare_folder_upload(self, local_dir, target_parent_id, merge=False):
        """创建远端目录结构，并返回文件上传计划。

        merge=True: 顶层目录不重命名，使用已有目录 ID；
                    遍历子目录时复用已存在的同名子目录；
                    跳过远端已存在的同名文件。
        merge=False: 保持原有行为（冲突则重命名）。
        """
        local_dir_path = Path(local_dir)
        if not local_dir_path.exists():
            raise FileNotFoundError(f"文件夹不存在: {local_dir_path}")
        if not local_dir_path.is_dir():
            raise NotADirectoryError(f"不是文件夹: {local_dir_path}")

        top_level_dirs = self._get_child_directory_map(target_parent_id)

        if merge and sanitize_filename(local_dir_path.name) in top_level_dirs:
            # 合并模式：复用已有顶层目录
            root_name = sanitize_filename(local_dir_path.name)
            root_dir_id = top_level_dirs[root_name]
            created_dir_count = 0
        else:
            root_name = self._choose_available_directory_name(
                set(top_level_dirs), sanitize_filename(local_dir_path.name)
            )
            root_dir_id = self._create_directory_with_backoff(target_parent_id, root_name)
            if not root_dir_id:
                raise RuntimeError(f"创建顶层目录失败: {root_name}")
            created_dir_count = 1

        dir_id_map = {Path("."): root_dir_id}
        file_targets = []

        try:
            for current_root, dir_names, file_names in os.walk(local_dir_path):
                dir_names.sort()
                file_names.sort()
                current_path = Path(current_root)
                relative_root = current_path.relative_to(local_dir_path)
                remote_parent_id = dir_id_map[relative_root or Path(".")]

                # merge 模式下获取远端子目录映射和文件名集合
                existing_child_dirs: dict[str, int] = {}
                existing_file_names: set[str] = set()
                if merge:
                    existing_child_dirs = self._get_child_directory_map(remote_parent_id)
                    for item in self._get_dir_items_by_id(remote_parent_id):
                        if int(item.get("Type", 0) or 0) == 0:
                            existing_file_names.add(sanitize_filename(item.get("FileName", "")))

                for dir_name in dir_names:
                    sanitized_dir = sanitize_filename(dir_name)
                    child_relative = relative_root / sanitized_dir
                    if merge and sanitized_dir in existing_child_dirs:
                        dir_id_map[child_relative] = existing_child_dirs[sanitized_dir]
                        continue
                    child_dir_id = self._create_directory_with_backoff(
                        remote_parent_id,
                        sanitized_dir,
                    )
                    if not child_dir_id:
                        raise RuntimeError(f"创建目录失败: {child_relative}")
                    dir_id_map[child_relative] = child_dir_id
                    created_dir_count += 1

                for file_name in file_names:
                    if merge and sanitize_filename(file_name) in existing_file_names:
                        continue
                    file_targets.append({
                        "file_name": file_name,
                        "local_path": str(current_path / file_name),
                        "target_dir_id": remote_parent_id,
                        "file_size": (current_path / file_name).stat().st_size,
                    })
        except Exception:
            if not merge:
                # 非合并模式：失败时回滚顶层目录
                try:
                    self.delete_file(
                        {"FileId": root_dir_id, "Type": 1, "FileName": root_name},
                    )
                except Exception as cleanup_exc:
                    logger.warning("回滚上传目录失败: %s", cleanup_exc)
            raise

        return {
            "root_dir_id": root_dir_id,
            "root_dir_name": root_name,
            "created_dir_count": created_dir_count,
            "file_targets": file_targets,
        }

    def upload_file_stream(
        self, file_path, dup_choice=1, task_id=None, signals=None, task=None,
        speed_tracker=None, resume_info=None, parent_id=0, file_name_override=None,
    ):
        """上传文件（分块），支持断点续传、progress 回调与取消/暂停控制。"""
        tid = uuid.uuid4().hex[:6]
        file_path = file_path.replace('"', "")
        if os.name == "nt":
            file_path = file_path.replace("\\", "/")
        file_path_obj = Path(file_path)
        file_name = sanitize_filename(file_name_override or file_path_obj.name)
        if not file_path_obj.exists():
            raise FileNotFoundError("文件不存在")
        if file_path_obj.is_dir():
            raise IsADirectoryError("不支持文件夹上传")
        fsize = file_path_obj.stat().st_size
        logger.debug("[T-%s] 开始上传: file=%s, size=%s", tid, file_name, fsize)
        headers = self.header_logined.copy()
        readable_hash = None

        if resume_info and resume_info.get("upload_id"):
            stored_hash = resume_info.get("etag", "")
            if not stored_hash:
                logger.warning("断点续传缺少 etag，改为重新上传")
                resume_info = None
            else:
                # 快速检查：文件大小 + mtime 未变则跳过 MD5 校验
                stored_mtime = resume_info.get("file_mtime", 0)
                stored_size = resume_info.get("file_size", 0)
                current_mtime = file_path_obj.stat().st_mtime
                if stored_mtime and stored_size == fsize and current_mtime == stored_mtime:
                    logger.info("文件大小与修改时间未变，跳过 MD5 校验")
                    readable_hash = stored_hash
                else:
                    if signals and hasattr(signals, "status"):
                        signals.status.emit("校验中")
                    stop_state, readable_hash = _calculate_file_md5(
                        file_path,
                        fsize,
                        task=task,
                        emit_progress=False,
                    )
                    if stop_state:
                        return stop_state
                    if readable_hash != stored_hash:
                        logger.warning("检测到本地文件已变更，放弃旧续传会话并重新上传")
                        if signals:
                            signals.progress.emit(0)
                        if speed_tracker:
                            speed_tracker.reset()
                        resume_info = None

        if resume_info and resume_info.get("upload_id"):
            # ---- 断点续传：复用已有 S3 session ----
            bucket = resume_info["bucket"]
            storage_node = resume_info["storage_node"]
            upload_key = resume_info["upload_key"]
            upload_id = resume_info["upload_id"]
            up_file_id = resume_info["up_file_id"]
            block_size = resume_info.get("block_size", UPLOAD_PART_SIZE)
            total_parts = resume_info.get("total_parts") or (
                math.ceil(fsize / block_size) if fsize > 0 else 1
            )
            done_parts = resume_info.get("done_parts", set())

            # C4: 用服务端实际存在的 parts 与本地记录做交集
            try:
                list_data = {
                    "bucket": bucket,
                    "key": upload_key,
                    "uploadId": upload_id,
                    "storageNode": storage_node,
                }
                list_res = self._api_request(
                    self.session.post,
                    "https://www.123pan.com/b/api/file/s3_list_upload_parts",
                    headers=headers,
                    data=json.dumps(list_data),
                    timeout=30,
                )
                list_json = _parse_json_response(list_res)
                if list_json.get("code", -1) == 0:
                    server_parts_data = list_json.get("data", {}).get("parts") or []
                    server_part_numbers = {int(p.get("PartNumber", 0)) for p in server_parts_data}
                    done_parts = done_parts & server_part_numbers
                else:
                    logger.warning(
                        "验证服务端已上传分块返回异常，将重新上传所有分块: %s",
                        json.dumps(list_json, ensure_ascii=False),
                    )
                    done_parts = set()
            except Exception as exc:
                logger.warning("验证服务端已上传分块失败，将重新上传所有分块: %s", exc)
                done_parts = set()

            logger.info(
                "[T-%s] 断点续传: done=%s/%s", tid, len(done_parts), total_parts
            )
            if signals and hasattr(signals, "status"):
                signals.status.emit("上传中")
        else:
            # ---- 全新上传：先校验文件 MD5 ----
            if readable_hash is None:
                if signals and hasattr(signals, "status"):
                    signals.status.emit("校验中")
                logger.debug("[T-%s] MD5 校验开始", tid)
                stop_state, readable_hash = _calculate_file_md5(
                    file_path,
                    fsize,
                    task=task,
                    signals=signals,
                    speed_tracker=speed_tracker,
                )
                if stop_state:
                    return stop_state
                logger.debug("[T-%s] MD5 校验完成: etag=%s", tid, readable_hash)

            list_up_request = {
                "driveId": 0,
                "etag": readable_hash,
                "fileName": file_name,
                "parentFileId": parent_id,
                "size": fsize,
                "type": 0,
                "duplicate": 0,
            }
            url = "https://www.123pan.com/b/api/file/upload_request"
            logger.debug("[T-%s] upload_request 发送, etag=%s", tid, readable_hash)
            res = self._api_request(
                self.session.post,
                url,
                headers=headers,
                data=json.dumps(list_up_request),
                timeout=30,
            )
            res_json = _parse_json_response(res)
            code = res_json.get("code", -1)
            if code == 5060:
                list_up_request["duplicate"] = dup_choice
                res = self._api_request(
                    self.session.post,
                    url,
                    headers=headers,
                    data=json.dumps(list_up_request),
                    timeout=30,
                )
                res_json = _parse_json_response(res)
                code = res_json.get("code", -1)
            if code != 0:
                raise RuntimeError(
                    "上传请求失败: " + json.dumps(res_json, ensure_ascii=False)
                )
            data = res_json.get("data") or {}
            if data.get("Reuse"):
                logger.debug("[T-%s] 秒传成功", tid)
                return "复用上传成功"
            bucket = data.get("Bucket", "")
            storage_node = data.get("StorageNode", "")
            upload_key = data.get("Key", "")
            upload_id = data.get("UploadId", "")
            up_file_id = data.get("FileId", 0)
            block_size = get_upload_part_size()
            total_parts = math.ceil(fsize / block_size) if fsize > 0 else 1
            done_parts = set()

            # 回传 S3 session 信息给 UI 持久化
            if signals and hasattr(signals, "session_info"):
                signals.session_info.emit({
                    "bucket": bucket, "storage_node": storage_node,
                    "upload_key": upload_key, "upload_id": upload_id,
                    "up_file_id": up_file_id, "total_parts": total_parts,
                    "block_size": block_size, "etag": readable_hash,
                    "file_mtime": file_path_obj.stat().st_mtime,
                })

            # 校验完成，切换到上传状态并重置进度
            if signals and hasattr(signals, "status"):
                signals.status.emit("上传中")
            if signals:
                signals.progress.emit(0)

        # 0 字节文件：跳过分片上传和 speed_tracker.reset()，直接完成
        if fsize == 0:
            if signals:
                signals.progress.emit(100)
            comp_data = {
                "bucket": bucket, "key": upload_key,
                "uploadId": upload_id, "storageNode": storage_node,
                "parts": [],
            }
            complete_res = self._api_request(
                self.session.post,
                "https://www.123pan.com/b/api/file/s3_complete_multipart_upload",
                headers=headers,
                data=json.dumps(comp_data),
                timeout=30,
            )
            complete_json = _parse_json_response(complete_res)
            if complete_json.get("code", -1) != 0:
                raise RuntimeError(
                    "0字节文件合并失败: " + json.dumps(complete_json, ensure_ascii=False)
                )
            close_data = {"fileId": up_file_id}
            for _attempt in range(5):
                if _attempt > 0:
                    time.sleep(min(2 ** _attempt, 8))
                close_res = self._api_request(
                    self.session.post,
                    "https://www.123pan.com/b/api/file/upload_complete",
                    headers=headers,
                    data=json.dumps(close_data),
                    timeout=30,
                )
                cr = _parse_json_response(close_res)
                if cr.get("code", -1) == 0:
                    logger.debug("[T-%s] 0字节文件上传完成", tid)
                    return up_file_id
            raise RuntimeError("0字节文件 upload_complete 超时")

        if speed_tracker:
            speed_tracker.reset()

        # 初始化 multipart upload session（123pan 要求在获取 presigned URL 前调用）
        init_data = {
            "bucket": bucket,
            "key": upload_key,
            "uploadId": upload_id,
            "storageNode": storage_node,
        }
        init_res = self._api_request(
            self.session.post,
            "https://www.123pan.com/b/api/file/s3_list_upload_parts",
            headers=headers,
            data=json.dumps(init_data),
            timeout=30,
        )
        init_json = _parse_json_response(init_res)
        if init_json.get("code", -1) != 0:
            raise RuntimeError(
                "初始化上传会话失败: " + json.dumps(init_json, ensure_ascii=False)
            )
        logger.debug("[T-%s] S3 session 初始化完成", tid)

        # 构建分块任务队列（跳过已完成的块）
        part_queue = queue.Queue()
        done_bytes = 0
        for part_num in range(1, total_parts + 1):
            offset = (part_num - 1) * block_size
            size = min(block_size, fsize - offset)
            if part_num in done_parts:
                done_bytes += size
            else:
                part_queue.put({"part_number": part_num, "offset": offset, "size": size})

        max_workers = min(
            max(
                1,
                _safe_int(
                    Database.instance().get_config("maxUploadThreads", 16),
                    default=16, min_val=1, max_val=16,
                ),
            ),
            16,
        )
        if total_parts == 1:
            max_workers = 1

        logger.debug("上传任务并发上限: %s, 分块数: %s, 已完成: %s", max_workers, total_parts, len(done_parts))
        logger.debug("[T-%s] 分块上传开始: parts=%s, workers=%s", tid, total_parts, max_workers)
        active_workers = [0]
        allowed_workers = [1]
        failed = [False]
        transient_failure_count = [0]
        progress_lock = threading.Lock()
        worker_feedback = threading.Event()
        probe_thread_name = [None]

        aggregator = _ProgressAggregator(fsize, speed_tracker, signals, PROGRESS_INTERVAL)
        aggregator.set_initial(done_bytes)
        aggregator.start()

        # 续传时立即发送已有进度
        if done_bytes > 0 and signals and fsize:
            signals.progress.emit(int(done_bytes * 100 / fsize))

        # 记录初始点，使第一块完成后即可计算速度
        if speed_tracker:
            speed_tracker.record(done_bytes)

        def _is_stopped():
            if task and getattr(task, "is_cancelled", False):
                return True
            if task and getattr(task, "pause_requested", False):
                return True
            return False

        def _fetch_presigned_url(part):
            pn = part["part_number"]
            url_data = {
                "bucket": bucket, "key": upload_key,
                "partNumberEnd": pn + 1, "partNumberStart": pn,
                "uploadId": upload_id, "StorageNode": storage_node,
            }
            url_res = self._api_request(
                self.session.post,
                "https://www.123pan.com/b/api/file/s3_repare_upload_parts_batch",
                headers=headers, data=json.dumps(url_data), timeout=30,
            )
            url_json = _parse_json_response(url_res)
            if url_json.get("code", -1) != 0:
                raise requests.RequestException(
                    "获取上传链接失败: " + json.dumps(url_json, ensure_ascii=False)
                )
            presigned_urls = url_json.get("data", {}).get("presignedUrls", {})
            url = presigned_urls.get(str(pn), "")
            if not url:
                raise requests.RequestException(f"分块 {pn} 无 presigned URL")
            return url

        uploaded_parts_map = {}
        uploaded_parts_lock = threading.Lock()

        def _upload_worker():
            wid = uuid.uuid4().hex[:4]
            max_retries = _safe_int(
                Database.instance().get_config("retryMaxAttempts", 3), 3, 0, 5
            )
            probe_promoted = False
            with progress_lock:
                active_workers[0] += 1
                if signals and hasattr(signals, "conn_info"):
                    signals.conn_info.emit(active_workers[0], max_workers)
            logger.debug("[T-%s W-%s] 启动: active=%s", tid, wid, active_workers[0])

            prefetched_part = None
            prefetched_result = [None, None]  # [url, exception]
            prefetch_thread = None
            _current_part = None  # 追踪当前 part，用于 finally 回队

            def _requeue_prefetch():
                """将预取的 part 放回队列并清理预取状态。"""
                nonlocal prefetched_part, prefetched_result, prefetch_thread
                if prefetched_part is not None:
                    part_queue.put(prefetched_part)
                    prefetched_part = None
                    prefetched_result = [None, None]
                    if prefetch_thread is not None:
                        prefetch_thread.join(timeout=5)
                        prefetch_thread = None

            try:
                while not failed[0]:
                    if _is_stopped():
                        logger.debug("[T-%s W-%s] 因任务停止退出", tid, wid)
                        return
                    with progress_lock:
                        is_probe = threading.current_thread().name == probe_thread_name[0]
                        if not is_probe and active_workers[0] > allowed_workers[0] and active_workers[0] > 1:
                            logger.debug(
                                "[T-%s W-%s] 超出并发上限退出: active=%s, allowed=%s",
                                tid, wid,
                                active_workers[0],
                                allowed_workers[0],
                            )
                            return

                    # 1. 获取当前 part 和 URL
                    if prefetched_part is not None:
                        part = prefetched_part
                        if prefetch_thread is not None:
                            prefetch_thread.join(timeout=10)
                            prefetch_thread = None
                        url = prefetched_result[0]
                        prefetched_part = None
                        prefetched_result = [None, None]
                    else:
                        try:
                            part = part_queue.get_nowait()
                        except queue.Empty:
                            logger.debug("[T-%s W-%s] 队列为空，退出", tid, wid)
                            return
                        url = None

                    _current_part = part  # 追踪当前 part 用于 finally 回队

                    pn = part["part_number"]
                    offset = part["offset"]
                    size = part["size"]

                    logger.debug(
                        "[T-%s W-%s] 获取分块 %s, 队列剩余=%s",
                        tid, wid, pn, part_queue.qsize(),
                    )

                    # 2. 启动下一个 part 的 URL 预取
                    try:
                        next_part = part_queue.get_nowait()
                    except queue.Empty:
                        next_part = None

                    if next_part is not None:
                        prefetched_part = next_part

                        def _prefetch(target_part):
                            try:
                                prefetched_result[0] = _fetch_presigned_url(target_part)
                            except Exception as exc:
                                prefetched_result[1] = exc

                        prefetch_thread = threading.Thread(
                            target=_prefetch, args=(next_part,), daemon=True,
                        )
                        prefetch_thread.start()

                    # 3. 上传当前分块
                    attempt = 0
                    while True:
                        progress_io = None
                        if _is_stopped():
                            logger.debug("[T-%s W-%s] 分块 %s 上传中停止", tid, wid, pn)
                            return
                        try:
                            if url is None:
                                url = _fetch_presigned_url(part)
                            def _on_chunk(n):
                                nonlocal probe_promoted
                                if not probe_promoted:
                                    with progress_lock:
                                        if threading.current_thread().name == probe_thread_name[0]:
                                            probe_thread_name[0] = None
                                            if allowed_workers[0] < max_workers:
                                                allowed_workers[0] += 1
                                    probe_promoted = True
                                    logger.debug("[T-%s W-%s] probe 转正, allowed=%s", tid, wid, allowed_workers[0])
                                    worker_feedback.set()
                                aggregator.record(n)

                            progress_io = _ProgressFileIO(file_path, offset, size, _on_chunk)
                            with self.session.put(url, data=progress_io, timeout=(10, 120)) as resp:
                                if resp.status_code in RATE_LIMIT_CODES:
                                    aggregator.record(-progress_io.reported)
                                    with progress_lock:
                                        transient_failure_count[0] += 1
                                        if transient_failure_count[0] > MAX_RATE_LIMITS:
                                            failed[0] = True
                                            logger.error("[T-%s W-%s] 限流次数过多，上传终止", tid, wid)
                                            return
                                        if threading.current_thread().name == probe_thread_name[0]:
                                            probe_thread_name[0] = None
                                        else:
                                            new_limit = max(1, active_workers[0] - 1)
                                            if new_limit < allowed_workers[0]:
                                                allowed_workers[0] = new_limit
                                    # 回队当前 part
                                    part_queue.put(part)
                                    _current_part = None  # 已回队
                                    worker_feedback.set()
                                    logger.debug(
                                        "[T-%s W-%s] 分块 %s 命中 %s，回队，allowed=%s",
                                        tid, wid, pn, resp.status_code, allowed_workers[0],
                                    )
                                    time.sleep(RATE_LIMIT_BACKOFF)
                                    break
                                resp.raise_for_status()
                                part_etag = resp.headers.get("ETag", "")
                                with uploaded_parts_lock:
                                    uploaded_parts_map[pn] = {"ETag": str(part_etag), "PartNumber": pn}
                                if signals and hasattr(signals, "part_done"):
                                    signals.part_done.emit(pn, part_etag)
                                with progress_lock:
                                    _reset_transient_failure_count(transient_failure_count)
                                    if allowed_workers[0] < max_workers:
                                        allowed_workers[0] = min(max_workers, allowed_workers[0] + 1)
                                worker_feedback.set()
                                logger.debug("[T-%s W-%s] 分块 %s 上传成功", tid, wid, pn)
                                _current_part = None  # 已成功处理
                                break
                        except Exception as exc:
                            if progress_io is not None and progress_io.reported > 0:
                                aggregator.record(-progress_io.reported)
                            is_conn_err = isinstance(exc, (requests.ConnectionError, requests.Timeout))
                            is_rate_limit_err = isinstance(exc, RateLimitError)
                            if is_rate_limit_err:
                                with progress_lock:
                                    transient_failure_count[0] += 1
                                    if transient_failure_count[0] > MAX_RATE_LIMITS:
                                        failed[0] = True
                                        logger.error("[T-%s W-%s] 限流次数过多，上传终止", tid, wid)
                                        return
                                    if threading.current_thread().name == probe_thread_name[0]:
                                        probe_thread_name[0] = None
                                    else:
                                        new_limit = max(1, active_workers[0] - 1)
                                        if new_limit < allowed_workers[0]:
                                            allowed_workers[0] = new_limit
                                part_queue.put(part)
                                _current_part = None  # 已回队
                                worker_feedback.set()
                                logger.warning(
                                    "[T-%s W-%s] 分块 %s 获取上传链接触发限流，回队重试: %s",
                                    tid, wid, pn, exc,
                                )
                                time.sleep(RATE_LIMIT_BACKOFF)
                                break
                            if attempt >= max_retries:
                                if is_conn_err:
                                    with progress_lock:
                                        transient_failure_count[0] += 1
                                        if transient_failure_count[0] > MAX_RATE_LIMITS:
                                            failed[0] = True
                                            logger.error("[T-%s W-%s] 连接错误次数过多，上传终止", tid, wid)
                                            return
                                        if threading.current_thread().name == probe_thread_name[0]:
                                            probe_thread_name[0] = None
                                        else:
                                            allowed_workers[0] = max(1, allowed_workers[0] - 1)
                                        logger.debug(
                                            "[T-%s W-%s] 连接被重置，降低并发至 %s",
                                            tid, wid, allowed_workers[0],
                                        )
                                    part_queue.put(part)
                                    _current_part = None  # 已回队
                                    worker_feedback.set()
                                    logger.warning(
                                        "[T-%s W-%s] 分块 %s 重试 %s 次仍失败，回队: %s",
                                        tid, wid, pn, attempt, exc,
                                    )
                                    return  # worker 退出，调度器补充新 worker
                                logger.error(
                                    "[T-%s W-%s] 分块 %s 上传失败（已重试 %s 次）: %s",
                                    tid, wid, pn, attempt, exc,
                                )
                                failed[0] = True
                                worker_feedback.set()
                                return
                            attempt += 1
                            url = None  # 重试时重新获取 presigned URL
                            logger.warning(
                                "[T-%s W-%s] 分块 %s 第 %s 次重试: %s", tid, wid, pn, attempt, exc
                            )
                            time.sleep(attempt)
                        finally:
                            if progress_io is not None:
                                progress_io.close()
            finally:
                # 统一回队：任何退出路径都确保 part 和预取不丢失
                if _current_part is not None:
                    part_queue.put(_current_part)
                    logger.debug("[T-%s W-%s] 回队当前 part", tid, wid)
                _requeue_prefetch()
                with progress_lock:
                    if threading.current_thread().name == probe_thread_name[0]:
                        probe_thread_name[0] = None
                    active_workers[0] -= 1
                    if signals and hasattr(signals, "conn_info"):
                        signals.conn_info.emit(
                            active_workers[0], max_workers
                        )
                worker_feedback.set()
                logger.debug("[T-%s W-%s] 退出: active=%s", tid, wid, active_workers[0])

        def _notify_conn(active, _allowed):
            if signals and hasattr(signals, "conn_info"):
                signals.conn_info.emit(active, max_workers)

        slow_start_scheduler(
            worker_fn=_upload_worker,
            max_workers=max_workers,
            part_queue=part_queue,
            progress_lock=progress_lock,
            active_workers=active_workers,
            allowed_workers=allowed_workers,
            failed=failed,
            probe_thread_name=probe_thread_name,
            worker_feedback=worker_feedback,
            is_stopped_fn=_is_stopped,
            notify_conn_fn=_notify_conn,
            thread_prefix="upload_worker",
        )

        aggregator.stop()
        aggregator.emit_final()
        if signals and hasattr(signals, "conn_info"):
            signals.conn_info.emit(0, max_workers)

        logger.debug(
            "[T-%s] 分块上传结束: uploaded=%s/%s, failed=%s",
            tid,
            aggregator.cumulative,
            fsize,
            failed[0],
        )

        if task and getattr(task, "is_cancelled", False):
            # 取消上传时尝试 abort S3 multipart upload
            try:
                abort_data = {
                    "bucket": bucket, "key": upload_key,
                    "uploadId": upload_id, "storageNode": storage_node,
                }
                self._api_request(
                    self.session.post,
                    "https://www.123pan.com/b/api/file/s3_abort_multipart_upload",
                    headers=headers,
                    data=json.dumps(abort_data),
                    timeout=10,
                )
                logger.debug("[T-%s] S3 multipart upload 已 abort", tid)
            except Exception as abort_exc:
                logger.warning("[T-%s] S3 abort 失败（可忽略）: %s", tid, abort_exc)
            return "已取消"
        if task and getattr(task, "pause_requested", False):
            return "已暂停"
        if failed[0]:
            raise RuntimeError("分块上传失败")

        # ---- 合并分块 ----
        logger.debug("[T-%s] 合并分块...", tid)
        uploaded_comp_data = {
            "bucket": bucket,
            "key": upload_key,
            "uploadId": upload_id,
            "storageNode": storage_node,
            "parts": sorted(uploaded_parts_map.values(), key=lambda p: p["PartNumber"]),
        }
        # C2: 检查 s3_list_upload_parts 和 s3_complete_multipart_upload 返回值
        list_res = self._api_request(
            self.session.post,
            "https://www.123pan.com/b/api/file/s3_list_upload_parts",
            headers=headers,
            data=json.dumps(uploaded_comp_data),
            timeout=30,
        )
        list_json = _parse_json_response(list_res)
        if list_json.get("code", -1) != 0:
            raise RuntimeError(
                "列出上传分块失败: " + json.dumps(list_json, ensure_ascii=False)
            )

        complete_res = self._api_request(
            self.session.post,
            "https://www.123pan.com/b/api/file/s3_complete_multipart_upload",
            headers=headers,
            data=json.dumps(uploaded_comp_data),
            timeout=30,
        )
        complete_json = _parse_json_response(complete_res)
        if complete_json.get("code", -1) != 0:
            raise RuntimeError(
                "合并上传分块失败: " + json.dumps(complete_json, ensure_ascii=False)
            )

        # M19: 轮询 upload_complete 替代盲等 sleep(3)
        close_up_session_data = {"fileId": up_file_id}
        max_complete_retries = 5
        for attempt in range(max_complete_retries):
            if attempt > 0:
                time.sleep(min(2 ** attempt, 8))
            close_res = self._api_request(
                self.session.post,
                "https://www.123pan.com/b/api/file/upload_complete",
                headers=headers,
                data=json.dumps(close_up_session_data),
                timeout=30,
            )
            cr = _parse_json_response(close_res)
            if cr.get("code", -1) == 0:
                logger.debug("[T-%s] upload_complete 确认成功", tid)
                return up_file_id
            if attempt < max_complete_retries - 1:
                logger.warning("upload_complete 未就绪 (attempt %d): %s", attempt + 1, cr.get("message", ""))
                continue
            raise RuntimeError(
                "上传完成确认失败: " + json.dumps(cr, ensure_ascii=False)
            )
        return up_file_id


# ==================== 工具函数和任务管理模块 ====================


def format_file_size(size):
    """格式化文件大小"""
    if size > 1073741824:
        return f"{round(size / 1073741824, 2)} GB"
    elif size > 1048576:
        return f"{round(size / 1048576, 2)} MB"
    elif size > 1024:
        return f"{round(size / 1024, 2)} KB"
    else:
        return f"{size} B"
