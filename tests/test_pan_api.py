"""测试 Pan123 API 业务方法（login, get_dir, delete, rename, share, mkdir 等）"""
import json
from unittest.mock import patch, MagicMock, call

import pytest
import requests

from src.app.common.api import Pan123


def _mock_response(status_code=200, json_data=None, headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {"code": 0, "message": "success"}
    resp.headers = headers or {}
    return resp


@pytest.fixture
def pan():
    """创建 Pan123 实例，跳过 __init__ 中的网络请求"""
    with patch.object(Pan123, "__init__", lambda self, **kw: None):
        p = Pan123()
        p.user_name = "testuser"
        p.password = "testpwd"
        p.authorization = "Bearer token123"
        p.devicetype = "MI5"
        p.osversion = "Android_12"
        p.loginuuid = "abc123"
        p.cookies = None
        p.recycle_list = None
        p.list = []
        p.total = 0
        p.parent_file_name_list = []
        p.all_file = False
        p.file_page = 0
        p.file_list = []
        p.dir_list = []
        p.name_dict = {}
        p.parent_file_id = 0
        p.parent_file_list = [0]

        # 创建带重试的 session（和 Pan123.__init__ 一致）
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        retry = Retry(total=3, backoff_factor=0.5, allowed_methods=["GET", "POST", "PUT", "HEAD"], raise_on_status=False)
        adapter = HTTPAdapter(max_retries=retry)
        p.session = requests.Session()
        p.session.mount("https://", adapter)
        p.session.mount("http://", adapter)

        p.header_logined = {
            "user-agent": "123pan/v2.4.0(Android_12;Xiaomi)",
            "authorization": p.authorization,
            "accept-encoding": "gzip",
            "content-type": "application/json",
            "osversion": p.osversion,
            "loginuuid": p.loginuuid,
            "platform": "android",
            "devicetype": p.devicetype,
            "devicename": "Xiaomi",
            "host": "www.123pan.com",
            "app-version": "61",
            "x-app-version": "2.4.0",
        }
        return p


class TestLogin:
    def test_login_success(self, pan):
        login_resp = _mock_response(200, {"code": 200, "data": {"token": "newtoken"}, "message": "ok"})
        login_resp.headers = {"Set-Cookie": "sid=abc123; Path=/"}

        with patch.object(pan.session, "post", return_value=login_resp) as mock_post, \
             patch.object(pan, "save_file"):
            code = pan.login()
            assert code == 200
            assert pan.authorization == "Bearer newtoken"
            assert pan.cookies["sid"] == "abc123"

    def test_login_wrong_password(self, pan):
        login_resp = _mock_response(200, {"code": 4001, "message": "密码错误"})

        with patch.object(pan.session, "post", return_value=login_resp):
            code = pan.login()
            assert code == 4001

    def test_login_network_error(self, pan):
        with patch.object(pan.session, "post", side_effect=requests.exceptions.ConnectionError("连接失败")):
            with pytest.raises(requests.exceptions.ConnectionError):
                pan.login()


class TestGetDir:
    def test_get_dir_success(self, pan):
        resp = _mock_response(200, {
            "code": 0, "message": "ok",
            "data": {"Total": 2, "InfoList": [
                {"FileName": "a.txt", "FileId": 1},
                {"FileName": "b.txt", "FileId": 2},
            ]}
        })

        with patch.object(pan.session, "get", return_value=resp):
            code, items = pan.get_dir_by_id(0)
            assert code == 0
            assert len(items) == 2

    def test_get_dir_empty(self, pan):
        resp = _mock_response(200, {
            "code": 0, "data": {"Total": 0, "InfoList": []}
        })

        with patch.object(pan.session, "get", return_value=resp):
            code, items = pan.get_dir_by_id(0)
            assert code == 0
            assert items == []

    def test_get_dir_server_error(self, pan):
        resp = _mock_response(200, {"code": -1, "message": "内部错误"})

        with patch.object(pan.session, "get", return_value=resp):
            code, items = pan.get_dir_by_id(0)
            assert code == -1
            assert items == []

    def test_get_dir_network_timeout(self, pan):
        with patch.object(pan.session, "get", side_effect=requests.exceptions.Timeout("超时")):
            code, items = pan.get_dir_by_id(0)
            assert code == -1
            assert items == []


class TestRename:
    def test_rename_success(self, pan):
        resp = _mock_response(200, {"code": 0, "message": "ok"})

        with patch.object(pan.session, "post", return_value=resp) as mock_post:
            result = pan.rename_file(123, "new_name.txt")
            assert result is True
            args, kwargs = mock_post.call_args
            assert "rename" in args[0]
            assert json.loads(kwargs["data"])["fileName"] == "new_name.txt"

    def test_rename_failure(self, pan):
        resp = _mock_response(200, {"code": -1, "message": "文件名已存在"})

        with patch.object(pan.session, "post", return_value=resp):
            result = pan.rename_file(123, "dup.txt")
            assert result is False


class TestShare:
    def test_share_success(self, pan):
        resp = _mock_response(200, {"code": 0, "data": {"ShareKey": "abc123"}, "message": "ok"})

        with patch.object(pan.session, "post", return_value=resp):
            url = pan.share([1, 2, 3])
            assert "abc123" in url

    def test_share_failure(self, pan):
        resp = _mock_response(200, {"code": -1, "message": "分享失败"})

        with patch.object(pan.session, "post", return_value=resp):
            with pytest.raises(RuntimeError, match="分享失败"):
                pan.share([1])

    def test_share_empty_list(self, pan):
        with pytest.raises(ValueError, match="文件ID列表为空"):
            pan.share([])


class TestDeleteFile:
    def test_delete_by_num(self, pan):
        pan.list = [{"FileName": "a.txt", "FileId": 1, "Type": 0, "Size": 100, "Etag": "x", "S3KeyFlag": "y"}]
        resp = _mock_response(200, {"code": 0, "message": "ok"})

        with patch.object(pan.session, "post", return_value=resp) as mock_post:
            pan.delete_file(0)
            mock_post.assert_called_once()

    def test_delete_invalid_num(self, pan):
        with pytest.raises(IndexError):
            pan.delete_file(999)

    def test_delete_non_digit_string(self, pan):
        with pytest.raises(ValueError):
            pan.delete_file("abc")


class TestMkdir:
    def test_mkdir_new_folder(self, pan):
        pan.list = []
        resp = _mock_response(200, {
            "code": 0, "data": {"FileId": 42, "Info": {"FileId": 42}}, "message": "ok"
        })

        with patch.object(pan.session, "post", return_value=resp) as mock_post, \
             patch.object(pan, "get_dir"):
            result = pan.mkdir("new_folder")
            assert result == 42

    def test_mkdir_existing_folder(self, pan):
        pan.list = [{"FileName": "existing", "FileId": 10, "Type": 1}]

        result = pan.mkdir("existing")
        assert result == 10

    def test_mkdir_api_failure(self, pan):
        pan.list = []
        resp = _mock_response(200, {"code": -1, "message": "创建失败"})

        with patch.object(pan.session, "post", return_value=resp):
            result = pan.mkdir("fail_folder")
            assert result is None


class TestRecycle:
    def test_recycle_success(self, pan):
        resp = _mock_response(200, {
            "code": 0, "data": {"InfoList": [{"FileId": 1}, {"FileId": 2}]}, "message": "ok"
        })

        with patch.object(pan.session, "get", return_value=resp):
            pan.recycle()
            assert len(pan.recycle_list) == 2
