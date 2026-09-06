"""测试 Pan123 API 业务方法（login, get_dir, delete, rename, share, mkdir 等）"""
import hashlib
import json
import threading
import time
from pathlib import Path, PosixPath
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, call

import pytest
import requests

from src.app.common import database as database_module
from src.app.common.database import Database
from src.app.common.api import (
    Pan123, RateLimitError, UPLOAD_PART_SIZE, _RWLock, _ProgressFileIO,
    _PrefetchResultSlot, _calculate_file_md5, _parse_json_response,
    _reset_transient_failure_count, format_file_size,
)
from src.app.view.transfer_interface import UploadThread


def _mock_response(status_code=200, json_data=None, headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {"code": 0, "message": "success"}
    resp.headers = headers or {}
    return resp


def _find_api_payload(mock_api, url_suffix):
    for call_ in mock_api.call_args_list:
        if call_.args[1].endswith(url_suffix):
            return json.loads(call_.kwargs.get("data", "{}"))
    raise AssertionError(f"未找到接口调用: {url_suffix}")


def _use_temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "123pan-open.db"
    monkeypatch.setattr(database_module, "_get_db_path", lambda: db_path)
    Database.reset()
    return Database.instance()


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
        p._login_lock = threading.Lock()
        p._session_lock = _RWLock()

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

        with patch.object(pan.session, "post", return_value=login_resp), \
             patch.object(pan, "save_file"):
            code = pan.login()
            assert code == 200
            assert pan.authorization == "Bearer newtoken"

    def test_login_wrong_password(self, pan):
        login_resp = _mock_response(200, {"code": 4001, "message": "密码错误"})

        with patch.object(pan.session, "post", return_value=login_resp):
            code = pan.login()
            assert code == 4001

    def test_login_network_error(self, pan):
        with patch.object(pan.session, "post", side_effect=requests.exceptions.ConnectionError("连接失败")):
            with pytest.raises(requests.exceptions.ConnectionError):
                pan.login()

    def test_save_file_respects_persistence_preferences(self, pan, tmp_path, monkeypatch):
        db = _use_temp_db(tmp_path, monkeypatch)
        db.set_many_config({
            "rememberPassword": False,
            "stayLoggedIn": False,
        })

        with patch("src.app.common.credential_store.delete_credential") as mock_del:
            pan.save_file()
            mock_del.assert_any_call("passWord")
            mock_del.assert_any_call("authorization")

        assert db.get_config("userName", "") == "testuser"
        assert db.get_config("passWord", "") == ""
        assert db.get_config("authorization", "") == ""

    def test_save_file_persists_credentials_when_enabled(self, pan, tmp_path, monkeypatch):
        db = _use_temp_db(tmp_path, monkeypatch)
        db.set_many_config({
            "rememberPassword": True,
            "stayLoggedIn": True,
        })

        with patch("src.app.common.credential_store.save_credential") as mock_save:
            pan.save_file()
            mock_save.assert_any_call("passWord", "testpwd")
            mock_save.assert_any_call("authorization", "Bearer token123")


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

    def test_get_dir_by_id_no_side_effects(self, pan):
        pan.file_page = 7
        pan.total = 99
        pan.all_file = True
        pan.list = [{"FileId": 1}]
        resp = _mock_response(
            200,
            {"code": 0, "data": {"Total": 1, "InfoList": [{"FileId": 2}]}}
        )

        with patch.object(pan.session, "get", return_value=resp):
            code, items = pan.get_dir_by_id(123)

        assert code == 0
        assert items == [{"FileId": 2}]
        # 不再修改实例属性
        assert pan.file_page == 7
        assert pan.total == 99
        assert pan.all_file is True
        assert pan.list == [{"FileId": 1}]


class TestTokenRefresh:
    def test_auto_refresh_on_code_2(self, pan):
        expired = _mock_response(200, {"code": 2, "message": "expired"})
        success = _mock_response(200, {"code": 0, "message": "ok"})

        def fake_login():
            pan.authorization = "Bearer refreshed"
            pan.header_logined["authorization"] = pan.authorization
            return 200

        with patch.object(pan.session, "get", side_effect=[expired, success]) as mock_get, \
             patch.object(pan, "_login_without_lock", side_effect=fake_login) as mock_login:
            response = pan._api_request(
                pan.session.get,
                "https://example.com/list",
                headers=pan.header_logined.copy(),
            )

        assert response is success
        assert mock_login.call_count == 1
        assert mock_get.call_args_list[1].kwargs["headers"]["authorization"] == "Bearer refreshed"
        expired.close.assert_called_once()

    def test_max_one_refresh(self, pan):
        expired = _mock_response(200, {"code": 2, "message": "expired"})

        with patch.object(pan.session, "get", side_effect=[expired, expired]) as mock_get, \
             patch.object(pan, "_login_without_lock", return_value=200) as mock_login:
            response = pan._api_request(
                pan.session.get,
                "https://example.com/list",
                headers=pan.header_logined.copy(),
            )

        assert response is expired
        assert mock_get.call_count == 2
        assert mock_login.call_count == 1

    def test_login_failure_raises(self, pan):
        expired = _mock_response(200, {"code": 2, "message": "expired"})

        with patch.object(pan.session, "get", return_value=expired), \
             patch.object(pan, "_login_without_lock", return_value=4001):
            with pytest.raises(RuntimeError, match="token 刷新失败"):
                pan._api_request(
                    pan.session.get,
                    "https://example.com/list",
                    headers=pan.header_logined.copy(),
                )

    def test_non_json_response_returns_raw(self, pan):
        response = MagicMock()
        response.json.side_effect = json.decoder.JSONDecodeError("bad", "", 0)

        with patch.object(pan.session, "get", return_value=response):
            result = pan._api_request(
                pan.session.get,
                "https://example.com/raw",
                headers=pan.header_logined.copy(),
            )

        assert result is response

    def test_login_lock_prevents_concurrent_refresh(self, pan):
        results = []
        login_calls = 0

        def fake_get(_url, **kwargs):
            auth = kwargs["headers"]["authorization"]
            code = 2 if auth == "Bearer token123" else 0
            return _mock_response(200, {"code": code, "message": "ok"})

        def fake_login():
            nonlocal login_calls
            login_calls += 1
            time.sleep(0.05)
            pan.authorization = "Bearer refreshed"
            pan.header_logined["authorization"] = pan.authorization
            return 200

        def call_api():
            response = pan._api_request(
                pan.session.get,
                "https://example.com/list",
                headers=pan.header_logined.copy(),
            )
            results.append(response.json()["code"])

        with patch.object(pan.session, "get", side_effect=fake_get), \
             patch.object(pan, "_login_without_lock", side_effect=fake_login):
            threads = [threading.Thread(target=call_api) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        assert sorted(results) == [0, 0]
        assert login_calls == 1

    def test_normal_code_passes_through(self, pan):
        success = _mock_response(200, {"code": 0, "message": "ok"})

        with patch.object(pan.session, "get", return_value=success), \
             patch.object(pan, "_login_without_lock") as mock_login:
            response = pan._api_request(
                pan.session.get,
                "https://example.com/list",
                headers=pan.header_logined.copy(),
            )

        assert response is success
        mock_login.assert_not_called()

    def test_create_directory_parses_response_with_close(self, pan):
        response = _mock_response(
            200,
            {"code": 0, "data": {"Info": {"FileId": 321}}},
        )

        with patch.object(pan, "_api_request", return_value=response):
            result = pan._create_directory(7, "docs")

        assert result == 321
        response.close.assert_called_once()


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


class TestDownloadLink:
    def test_link_by_file_detail_prefers_location_header(self, pan):
        api_resp = _mock_response(
            200,
            {"code": 0, "data": {"DownloadUrl": "https://example.com/jump"}},
        )
        redirect_resp = MagicMock()
        redirect_resp.__enter__.return_value = redirect_resp
        redirect_resp.__exit__.return_value = False
        redirect_resp.headers = {"Location": "https://download.example/file.bin"}
        redirect_resp.text = '<a href="https://download.example/old.bin">old</a>'

        with patch.object(pan, "_api_request", return_value=api_resp), \
             patch.object(pan, "_raw_request", return_value=redirect_resp):
            result = pan.link_by_fileDetail({"Type": 0, "FileId": 1}, showlink=False)

        assert result == "https://download.example/file.bin"

    def test_link_by_file_detail_falls_back_to_html_href(self, pan):
        api_resp = _mock_response(
            200,
            {"code": 0, "data": {"DownloadUrl": "https://example.com/jump"}},
        )
        redirect_resp = MagicMock()
        redirect_resp.__enter__.return_value = redirect_resp
        redirect_resp.__exit__.return_value = False
        redirect_resp.headers = {}
        redirect_resp.text = '<a href="https://download.example/file.bin">download</a>'

        with patch.object(pan, "_api_request", return_value=api_resp), \
             patch.object(pan, "_raw_request", return_value=redirect_resp):
            result = pan.link_by_fileDetail({"Type": 0, "FileId": 1}, showlink=False)

        assert result == "https://download.example/file.bin"

    def test_link_by_file_detail_returns_error_when_no_location_or_href(self, pan):
        api_resp = _mock_response(
            200,
            {"code": 0, "data": {"DownloadUrl": "https://example.com/jump"}},
        )
        redirect_resp = MagicMock()
        redirect_resp.__enter__.return_value = redirect_resp
        redirect_resp.__exit__.return_value = False
        redirect_resp.headers = {}
        redirect_resp.text = "no redirect here"

        with patch.object(pan, "_api_request", return_value=api_resp), \
             patch.object(pan, "_raw_request", return_value=redirect_resp):
            result = pan.link_by_fileDetail({"Type": 0, "FileId": 1}, showlink=False)

        assert result == -1


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
    def test_delete_by_detail_dict(self, pan):
        file_detail = {"FileId": 1, "Type": 0, "FileName": "a.txt"}
        resp = _mock_response(200, {"code": 0, "message": "ok"})

        with patch.object(pan.session, "post", return_value=resp) as mock_post:
            pan.delete_file(file_detail)
            mock_post.assert_called_once()

    def test_delete_accepts_detail_dict(self, pan):
        file_detail = {"FileId": 1, "Type": 0, "FileName": "a.txt"}
        resp = _mock_response(200, {"code": 0, "message": "ok"})

        with patch.object(pan.session, "post", return_value=resp) as mock_post:
            pan.delete_file(file_detail)

        mock_post.assert_called_once()


class TestMkdir:
    def test_mkdir_new_folder(self, pan):
        getdir_resp = _mock_response(200, {"code": 0, "data": {"InfoList": [], "Total": 0}, "message": "ok"})
        create_resp = _mock_response(200, {
            "code": 0, "data": {"FileId": 42, "Info": {"FileId": 42}}, "message": "ok"
        })

        with patch.object(pan, "get_dir_by_id", return_value=(0, [])):
            with patch.object(pan.session, "post", return_value=create_resp):
                result = pan.mkdir("new_folder")
                assert result == 42

    def test_mkdir_existing_folder(self, pan):
        existing = [{"FileName": "existing", "FileId": 10, "Type": 1}]

        with patch.object(pan, "get_dir_by_id", return_value=(0, existing)):
            result = pan.mkdir("existing")
            assert result == 10

    def test_mkdir_api_failure(self, pan):
        resp = _mock_response(200, {"code": -1, "message": "创建失败"})

        with patch.object(pan, "get_dir_by_id", return_value=(0, [])):
            with patch.object(pan.session, "post", return_value=resp):
                with pytest.raises(RuntimeError, match="创建目录 'fail_folder' 失败"):
                    pan.mkdir("fail_folder")


class TestFolderUploadPlan:
    def test_choose_available_directory_name_appends_suffix(self, pan):
        name = pan._choose_available_directory_name(
            {"资料", "资料(1)"},
            "资料",
        )

        assert name == "资料(2)"

    def test_ensure_directory_reuses_existing_child(self, pan):
        with patch.object(
            pan,
            "_get_child_directory_map",
            return_value={"docs": 99},
        ), patch.object(pan, "_create_directory") as mock_create:
            result = pan.ensure_directory(0, "docs")

        assert result == 99
        mock_create.assert_not_called()

    def test_prepare_folder_upload_creates_remote_tree(self, pan, tmp_path):
        root = tmp_path / "资料"
        child = root / "子目录"
        child.mkdir(parents=True)
        (root / "a.txt").write_text("a", encoding="utf-8")
        (child / "b.txt").write_text("bb", encoding="utf-8")

        created = []
        next_id = 100

        def fake_create(parent_id, dirname):
            nonlocal next_id
            next_id += 1
            created.append((parent_id, dirname))
            return next_id

        def fake_dir_map(parent_id, *, normalize_names=False):
            if parent_id == 0:
                return {"资料": 7}
            return {}

        with patch.object(
            pan,
            "_get_child_directory_map",
            side_effect=fake_dir_map,
        ), patch.object(
            pan,
            "_create_directory_with_backoff",
            side_effect=fake_create,
        ):
            plan = pan.prepare_folder_upload(root, 0)

        assert created == [(0, "资料(1)"), (101, "子目录")]
        assert plan["root_dir_name"] == "资料(1)"
        assert plan["root_dir_id"] == 101
        assert plan["created_dir_count"] == 2
        assert plan["file_targets"] == [
            {
                "file_name": "a.txt",
                "file_size": 1,
                "local_path": str(root / "a.txt"),
                "target_dir_id": 101,
            },
            {
                "file_name": "b.txt",
                "file_size": 2,
                "local_path": str(child / "b.txt"),
                "target_dir_id": 102,
            },
        ]


class TestCreateDirectory:
    def test_429_backoff_retries(self, pan):
        with patch.object(
            pan,
            "_create_directory",
            side_effect=[RateLimitError("429"), "dir_id_123"],
        ) as mock_create, patch("src.app.common.api.time.sleep") as mock_sleep:
            result = pan._create_directory_with_backoff(0, "docs")

        assert result == "dir_id_123"
        assert mock_create.call_count == 2
        mock_sleep.assert_called_once_with(1.0)

    def test_429_exponential_backoff_timing(self, pan):
        with patch.object(
            pan,
            "_create_directory",
            side_effect=[
                RateLimitError("429"),
                RateLimitError("429"),
                RateLimitError("429"),
                "dir_id_123",
            ],
        ), patch("src.app.common.api.time.sleep") as mock_sleep:
            result = pan._create_directory_with_backoff(0, "docs")

        assert result == "dir_id_123"
        assert [call.args[0] for call in mock_sleep.call_args_list] == [1.0, 2.0, 4.0]

    def test_429_max_backoff_cap(self, pan):
        side_effect = [RateLimitError("429")] * 6 + ["dir_id_123"]

        with patch.object(pan, "_create_directory", side_effect=side_effect), \
             patch("src.app.common.api.time.sleep") as mock_sleep:
            result = pan._create_directory_with_backoff(0, "docs")

        assert result == "dir_id_123"
        assert max(call.args[0] for call in mock_sleep.call_args_list) <= 30.0

    def test_429_max_retries_exhausted(self, pan):
        side_effect = [RateLimitError("429")] * 11

        with patch.object(pan, "_create_directory", side_effect=side_effect), \
             patch("src.app.common.api.time.sleep") as mock_sleep:
            with pytest.raises(RuntimeError, match="已重试 10 次"):
                pan._create_directory_with_backoff(0, "docs")

        assert len(mock_sleep.call_args_list) == 10

    def test_success_no_backoff(self, pan):
        with patch.object(pan, "_create_directory", return_value="dir_id_123"), \
             patch("src.app.common.api.time.sleep") as mock_sleep:
            result = pan._create_directory_with_backoff(0, "docs")

        assert result == "dir_id_123"
        mock_sleep.assert_not_called()

    def test_api_error_raises(self, pan):
        with patch.object(
            pan,
            "_create_directory",
            side_effect=RuntimeError("创建失败"),
        ), patch("src.app.common.api.time.sleep") as mock_sleep:
            with pytest.raises(RuntimeError, match="创建失败"):
                pan._create_directory_with_backoff(0, "docs")

        mock_sleep.assert_not_called()

    def test_prepare_folder_upload_uses_backoff(self, pan, tmp_path):
        root = tmp_path / "资料"
        root.mkdir()
        (root / "a.txt").write_text("a", encoding="utf-8")

        with patch.object(pan, "_get_child_directory_map", return_value={}), \
             patch.object(
                 pan,
                 "_create_directory_with_backoff",
                 return_value=101,
             ) as mock_create:
            plan = pan.prepare_folder_upload(root, 0)

        assert plan["root_dir_id"] == 101
        mock_create.assert_called_once_with(0, "资料")

    def test_prepare_folder_upload_merge_uses_normalized_remote_directory_names(self, pan, tmp_path, monkeypatch):
        root = tmp_path / "docs."
        child = root / "sub."
        child.mkdir(parents=True)
        (child / "a.txt").write_text("a", encoding="utf-8")

        monkeypatch.setattr("src.app.common.filename_utils.sys.platform", "win32")

        def fake_dir_map(parent_id, *, normalize_names=False):
            if parent_id == 0:
                return {"docs": 7}
            if parent_id == 7:
                return {"sub": 8}
            return {}

        def fake_items(parent_id):
            if parent_id == 7:
                return [{"Type": 1, "FileName": "sub.", "FileId": 8}]
            if parent_id == 8:
                return []
            return [{"Type": 1, "FileName": "docs.", "FileId": 7}]

        with patch.object(pan, "_get_child_directory_map", side_effect=fake_dir_map), \
             patch.object(pan, "_get_dir_items_by_id", side_effect=fake_items), \
             patch.object(pan, "_create_directory_with_backoff") as mock_create:
            plan = pan.prepare_folder_upload(root, 0, merge=True)

        assert plan["root_dir_id"] == 7
        assert plan["created_dir_count"] == 0
        assert plan["file_targets"] == [
            {
                "file_name": "a.txt",
                "file_size": 1,
                "local_path": str(child / "a.txt"),
                "target_dir_id": 8,
            },
        ]
        mock_create.assert_not_called()


class TestUploadFileStream:
    def test_parent_id_parameter(self, pan, tmp_path):
        local_file = tmp_path / "demo.txt"
        local_file.write_text("demo", encoding="utf-8")
        response = _mock_response(200, {"code": 0, "data": {"Reuse": True}})

        with patch.object(pan, "_api_request", side_effect=[response]) as mock_api:
            result = pan.upload_file_stream(str(local_file), parent_id=999)
            assert result == "复用上传成功"
            payload = json.loads(mock_api.call_args.kwargs.get("data", "{}"))
            assert payload["parentFileId"] == 999

    def test_parent_id_default_zero(self, pan, tmp_path):
        local_file = tmp_path / "demo.txt"
        local_file.write_text("demo", encoding="utf-8")
        response = _mock_response(200, {"code": 0, "data": {"Reuse": True}})

        with patch.object(pan, "_api_request", side_effect=[response]) as mock_api:
            result = pan.upload_file_stream(str(local_file))
            assert result == "复用上传成功"
            payload = json.loads(mock_api.call_args.kwargs.get("data", "{}"))
            assert payload["parentFileId"] == 0

    def test_new_upload_uses_five_mb_part_size(self, pan, tmp_path):
        local_file = tmp_path / "demo.txt"
        local_file.write_text("demo", encoding="utf-8")
        signal_payloads = []

        class _Signal:
            def emit(self, value):
                signal_payloads.append(value)

        class _Signals:
            progress = _Signal()
            session_info = _Signal()

        upload_res = _mock_response(
            200,
            {
                "code": 0,
                "data": {
                    "Reuse": False,
                    "Bucket": "bucket",
                    "StorageNode": "node",
                    "Key": "key",
                    "UploadId": "upload-id",
                    "FileId": 123,
                },
            },
        )
        init_res = _mock_response(200, {"code": 0, "message": "ok"})
        get_link_res = _mock_response(
            200,
            {
                "code": 0,
                "data": {"presignedUrls": {"1": "https://upload.example/1"}},
            },
        )
        ok_res = _mock_response(200, {"code": 0, "message": "ok"})
        put_res = MagicMock()
        put_res.status_code = 200
        put_res.headers = {"ETag": '"etag-1"'}
        put_res.raise_for_status.return_value = None

        with patch.object(
            pan,
            "_api_request",
            side_effect=[upload_res, init_res, get_link_res, ok_res, ok_res, ok_res],
        ), patch.object(pan.session, "put", return_value=put_res), \
             patch("src.app.common.api.Database.instance") as mock_db:
            mock_db.return_value.get_config.return_value = 1
            pan.upload_file_stream(str(local_file), parent_id=999, signals=_Signals())

        session = next(p for p in signal_payloads if isinstance(p, dict))
        assert session["block_size"] == UPLOAD_PART_SIZE

    def test_resume_with_empty_server_parts_reuploads_local_parts(self, pan, tmp_path):
        local_file = tmp_path / "resume.txt"
        local_file.write_text("demo", encoding="utf-8")
        resume_info = {
            "bucket": "bucket",
            "storage_node": "node",
            "upload_key": "key",
            "upload_id": "upload-id",
            "up_file_id": 123,
            "total_parts": 1,
            "block_size": UPLOAD_PART_SIZE,
            "done_parts": {1},
            "etag": hashlib.md5(b"demo").hexdigest(),
        }

        server_parts_res = _mock_response(200, {"code": 0, "data": {"parts": []}})
        init_res = _mock_response(200, {"code": 0, "message": "ok"})
        get_link_res = _mock_response(
            200,
            {"code": 0, "data": {"presignedUrls": {"1": "https://upload.example/1"}}},
        )
        ok_res = _mock_response(200, {"code": 0, "message": "ok"})
        put_res = MagicMock()
        put_res.status_code = 200
        put_res.headers = {"ETag": '"etag-1"'}
        put_res.raise_for_status.return_value = None

        with patch.object(
            pan,
            "_api_request",
            side_effect=[server_parts_res, init_res, get_link_res, ok_res, ok_res, ok_res],
        ), \
             patch.object(pan.session, "put", return_value=put_res) as mock_put, \
             patch("src.app.common.api.Database.instance") as mock_db:
            mock_db.return_value.get_config.return_value = 1

            pan.upload_file_stream(str(local_file), resume_info=resume_info)

        mock_put.assert_called_once()

    def test_resume_with_nonzero_server_code_starts_new_upload_session(self, pan, tmp_path):
        local_file = tmp_path / "resume.txt"
        local_file.write_text("demo", encoding="utf-8")
        resume_info = {
            "bucket": "bucket",
            "storage_node": "node",
            "upload_key": "key",
            "upload_id": "upload-id",
            "up_file_id": 123,
            "total_parts": 1,
            "block_size": UPLOAD_PART_SIZE,
            "done_parts": {1},
            "etag": hashlib.md5(b"demo").hexdigest(),
        }

        server_parts_res = _mock_response(200, {"code": 2, "message": "expired"})
        upload_res = _mock_response(200, {"code": 0, "data": {"Reuse": True}})

        with patch.object(
            pan,
            "_api_request",
            side_effect=[server_parts_res, upload_res],
        ) as mock_api:
            result = pan.upload_file_stream(str(local_file), resume_info=resume_info)

        assert result == "复用上传成功"
        urls = [call.args[1] for call in mock_api.call_args_list]
        assert urls == [
            "https://www.123pan.com/b/api/file/s3_list_upload_parts",
            "https://www.123pan.com/b/api/file/upload_request",
        ]

    def test_resume_with_changed_local_file_starts_new_upload(self, pan, tmp_path):
        local_file = tmp_path / "changed.txt"
        local_file.write_text("new-content", encoding="utf-8")
        resume_info = {
            "bucket": "bucket",
            "storage_node": "node",
            "upload_key": "key",
            "upload_id": "upload-id",
            "up_file_id": 123,
            "total_parts": 1,
            "block_size": UPLOAD_PART_SIZE,
            "done_parts": {1},
            "etag": "outdated-hash",
        }
        upload_res = _mock_response(200, {"code": 0, "data": {"Reuse": True}})

        with patch.object(pan, "_api_request", side_effect=[upload_res]) as mock_api:
            result = pan.upload_file_stream(str(local_file), resume_info=resume_info)

        assert result == "复用上传成功"
        payload = json.loads(mock_api.call_args.kwargs["data"])
        assert payload["fileName"] == "changed.txt"
        assert payload["parentFileId"] == 0

    def test_resume_reuses_server_parts_and_completes_with_history(self, pan, tmp_path):
        content = b"abcdefgh"
        local_file = tmp_path / "resume-two-parts.txt"
        local_file.write_bytes(content)
        resume_info = {
            "bucket": "bucket",
            "storage_node": "node",
            "upload_key": "key",
            "upload_id": "upload-id",
            "up_file_id": 123,
            "total_parts": 2,
            "block_size": 4,
            "done_parts": set(),
            "etag": hashlib.md5(content).hexdigest(),
        }
        part1_etag = f'"{hashlib.md5(content[:4]).hexdigest()}"'
        server_parts_res = _mock_response(
            200,
            {"code": 0, "data": {"parts": [{"PartNumber": 1, "ETag": part1_etag}]}},
        )
        init_res = _mock_response(200, {"code": 0, "message": "ok"})
        get_link_res = _mock_response(
            200,
            {"code": 0, "data": {"presignedUrls": {"2": "https://upload.example/2"}}},
        )
        complete_ready_res = _mock_response(
            200,
            {"code": 0, "data": {"parts": [{"PartNumber": 1, "ETag": part1_etag}]}},
        )
        ok_res = _mock_response(200, {"code": 0, "message": "ok"})
        put_res = MagicMock()
        put_res.status_code = 200
        put_res.headers = {"ETag": '"etag-2"'}
        put_res.raise_for_status.return_value = None
        put_res.__enter__.return_value = put_res

        with patch.object(
            pan,
            "_api_request",
            side_effect=[
                server_parts_res,
                init_res,
                get_link_res,
                complete_ready_res,
                ok_res,
                ok_res,
            ],
        ) as mock_api, patch.object(pan.session, "put", return_value=put_res) as mock_put, \
             patch("src.app.common.api.Database.instance") as mock_db:
            mock_db.return_value.get_config.return_value = 1
            result = pan.upload_file_stream(str(local_file), resume_info=resume_info)

        assert result == 123
        mock_put.assert_called_once()
        complete_payload = _find_api_payload(mock_api, "s3_complete_multipart_upload")
        assert complete_payload["parts"] == [
            {"ETag": part1_etag, "PartNumber": 1},
            {"ETag": '"etag-2"', "PartNumber": 2},
        ]

    def test_resume_with_mismatched_server_part_starts_new_upload(self, pan, tmp_path):
        content = b"abcdefgh"
        local_file = tmp_path / "mismatch.txt"
        local_file.write_bytes(content)
        resume_info = {
            "bucket": "bucket",
            "storage_node": "node",
            "upload_key": "key",
            "upload_id": "upload-id",
            "up_file_id": 123,
            "total_parts": 2,
            "block_size": 4,
            "done_parts": {1},
            "etag": hashlib.md5(content).hexdigest(),
        }
        stale_part_etag = f'"{hashlib.md5(b"WXYZ").hexdigest()}"'
        server_parts_res = _mock_response(
            200,
            {"code": 0, "data": {"parts": [{"PartNumber": 1, "ETag": stale_part_etag}]}},
        )
        upload_res = _mock_response(200, {"code": 0, "data": {"Reuse": True}})

        with patch.object(
            pan,
            "_api_request",
            side_effect=[server_parts_res, upload_res],
        ) as mock_api:
            result = pan.upload_file_stream(str(local_file), resume_info=resume_info)

        assert result == "复用上传成功"
        urls = [call.args[1] for call in mock_api.call_args_list]
        assert urls == [
            "https://www.123pan.com/b/api/file/s3_list_upload_parts",
            "https://www.123pan.com/b/api/file/upload_request",
        ]

    def test_rate_limit_error_when_fetching_presigned_url_requeues_part(self, pan, tmp_path):
        local_file = tmp_path / "retry.txt"
        local_file.write_text("demo", encoding="utf-8")
        upload_res = _mock_response(
            200,
            {
                "code": 0,
                "data": {
                    "Reuse": False,
                    "Bucket": "bucket",
                    "StorageNode": "node",
                    "Key": "key",
                    "UploadId": "upload-id",
                    "FileId": 123,
                },
            },
        )
        init_res = _mock_response(200, {"code": 0, "message": "ok"})
        get_link_res = _mock_response(
            200,
            {"code": 0, "data": {"presignedUrls": {"1": "https://upload.example/1"}}},
        )
        ok_res = _mock_response(200, {"code": 0, "message": "ok"})
        put_res = MagicMock()
        put_res.status_code = 200
        put_res.headers = {"ETag": '"etag-1"'}
        put_res.raise_for_status.return_value = None

        with patch.object(
            pan,
            "_api_request",
            side_effect=[
                upload_res,
                init_res,
                RateLimitError("429"),
                get_link_res,
                ok_res,
                ok_res,
                ok_res,
            ],
        ), patch.object(pan.session, "put", return_value=put_res) as mock_put, \
             patch("src.app.common.api.Database.instance") as mock_db, \
             patch("src.app.common.api.time.sleep") as mock_sleep:
            mock_db.return_value.get_config.return_value = 1

            result = pan.upload_file_stream(str(local_file))

        assert result == 123
        mock_put.assert_called_once()
        assert any(call.args[0] == 2 for call in mock_sleep.call_args_list)

    def test_prepare_folder_upload_rolls_back_root_on_stat_failure(self, pan, tmp_path):
        root = tmp_path / "资料"
        root.mkdir()
        target_file = root / "a.txt"
        target_file.write_text("a", encoding="utf-8")
        original_stat = Path.stat

        def fake_stat(path_obj, **kwargs):
            if path_obj == target_file:
                raise OSError("stat failed")
            return original_stat(path_obj)

        with patch.object(pan, "_get_child_directory_map", return_value={}), \
             patch.object(pan, "_create_directory_with_backoff", return_value=101), \
             patch.object(pan, "delete_file") as mock_delete, \
             patch("src.app.common.api.Path.stat", autospec=True, side_effect=fake_stat):
            with pytest.raises(OSError, match="stat failed"):
                pan.prepare_folder_upload(root, 0)

        mock_delete.assert_called_once_with(
            {"FileId": 101, "Type": 1, "FileName": "资料"},
        )


class TestThreadSafety:
    def test_upload_thread_no_parent_file_id_mutation(self):
        task = MagicMock()
        task.local_path = "/tmp/demo.txt"
        task.target_dir_id = 456
        task.speed_tracker = None
        task.db_task_id = None
        task.bucket = ""
        task.upload_key = ""
        task.upload_id_s3 = ""
        task.is_cancelled = False
        task.pause_requested = False
        pan = MagicMock()
        pan.parent_file_id = 123
        pan.upload_file_stream.return_value = "复用上传成功"

        UploadThread(task, pan).run()

        assert pan.parent_file_id == 123
        assert pan.upload_file_stream.call_args.kwargs["parent_id"] == 456


class TestQrGenerate:
    """qr_generate() 单元测试"""

    def test_qr_generate_success(self, pan):
        """Test 1: 成功时返回 dict 包含 uniID 和 url"""
        resp = _mock_response(200, {
            "code": 0,
            "data": {"uniID": "uni-abc-123", "url": "https://login.123pan.com/qr/xxx"},
        })

        with patch.object(pan.session, "get", return_value=resp):
            result = pan.qr_generate()
            assert "uniID" in result
            assert "url" in result
            assert result["uniID"] == "uni-abc-123"
            assert result["url"] == "https://login.123pan.com/qr/xxx"

    def test_qr_generate_network_error(self, pan):
        """Test 2: 网络异常时抛出 requests.RequestException"""
        with patch.object(
            pan.session, "get",
            side_effect=requests.exceptions.ConnectionError("连接失败"),
        ):
            with pytest.raises(requests.exceptions.RequestException):
                pan.qr_generate()

    def test_qr_generate_api_error(self, pan):
        """Test 3: API 返回 code!=0 时抛出 RuntimeError"""
        resp = _mock_response(200, {"code": -1, "message": "服务异常"})

        with patch.object(pan.session, "get", return_value=resp):
            with pytest.raises(RuntimeError, match="获取二维码失败"):
                pan.qr_generate()


class TestQrPoll:
    """qr_poll() 单元测试"""

    def test_qr_poll_waiting(self, pan):
        """Test 4: loginStatus=0 时返回等待状态"""
        resp = _mock_response(200, {
            "code": 0,
            "data": {"loginStatus": 0, "scanPlatform": 0},
        })

        with patch.object(pan.session, "get", return_value=resp):
            result = pan.qr_poll("uni-abc-123")
            assert result["loginStatus"] == 0
            assert result["scanPlatform"] == 0
            assert "token" not in result

    def test_qr_poll_app_confirmed(self, pan):
        """Test 5: code=200 时 App 扫码确认，返回 loginStatus=3 + token"""
        resp = _mock_response(200, {
            "code": 200,
            "data": {"login_type": 7, "token": "eyJhbGciOiJIUzI1NiJ9.test"},
        })

        with patch.object(pan.session, "get", return_value=resp):
            result = pan.qr_poll("uni-abc-123")
            assert result["loginStatus"] == 3
            assert result["scanPlatform"] == 7
            assert result["token"] == "eyJhbGciOiJIUzI1NiJ9.test"

    def test_qr_poll_wechat_confirmed(self, pan):
        """Test 5b: code=200 时微信扫码确认，scanPlatform=4"""
        resp = _mock_response(200, {
            "code": 200,
            "data": {"login_type": 4, "token": ""},
        })

        with patch.object(pan.session, "get", return_value=resp):
            result = pan.qr_poll("uni-abc-123")
            assert result["loginStatus"] == 3
            assert result["scanPlatform"] == 4

    def test_qr_poll_network_error(self, pan):
        """Test 6: 网络异常时抛出 requests.RequestException"""
        with patch.object(
            pan.session, "get",
            side_effect=requests.exceptions.ConnectionError("连接失败"),
        ):
            with pytest.raises(requests.exceptions.RequestException):
                pan.qr_poll("uni-abc-123")


# ── _ProgressFileIO 单元测试 ──


class TestProgressFileIO:
    """_ProgressFileIO：按需从文件读取并上报进度。"""

    def test_read_all_at_once(self, tmp_path):
        """read(-1) 一次性读取全部内容。"""
        data = b"Hello, World! " * 100  # ~1.4 KB
        f = tmp_path / "test.bin"
        f.write_bytes(data)

        reported: list[int] = []
        pio = _ProgressFileIO(str(f), 0, len(data), reported.append)
        assert len(pio) == len(data)

        result = pio.read(-1)
        assert result == data
        # requests 会再调一次 read()，触发剩余 pending 刷新
        assert pio.read(-1) == b""
        assert pio.reported == len(data)
        assert reported == [len(data)]
        pio.close()

    def test_chunked_read_with_threshold(self, tmp_path):
        """分段读取，进度按 _REPORT_SIZE 阈值触发。"""
        data = b"X" * (512 * 1024)  # 512 KB
        f = tmp_path / "test.bin"
        f.write_bytes(data)

        reported: list[int] = []
        pio = _ProgressFileIO(str(f), 0, len(data), reported.append)
        chunk_size = 64 * 1024
        total_read = 0
        while True:
            chunk = pio.read(chunk_size)
            if not chunk:
                break
            total_read += len(chunk)

        assert total_read == len(data)
        assert pio.reported == len(data)
        assert sum(reported) == len(data)
        pio.close()

    def test_read_with_offset(self, tmp_path):
        """从文件中间偏移处开始读取。"""
        data = b"0123456789ABCDEF"
        f = tmp_path / "test.bin"
        f.write_bytes(data)

        pio = _ProgressFileIO(str(f), 5, 6, lambda n: None)  # offset=5, size=6
        result = pio.read(-1)
        assert result == b"56789A"
        pio.read(-1)  # 触发 pending 刷新
        assert pio.reported == 6
        pio.close()

    def test_read_returns_empty_after_exhausted(self, tmp_path):
        """读取完毕后再 read 返回空串。"""
        f = tmp_path / "test.bin"
        f.write_bytes(b"short")

        pio = _ProgressFileIO(str(f), 0, 5, lambda n: None)
        pio.read(-1)
        assert pio.read(-1) == b""
        assert pio.read(100) == b""
        pio.close()

    def test_read_truncates_oversized_request(self, tmp_path):
        """read(size) 超出剩余时自动截断到 _remaining。"""
        f = tmp_path / "test.bin"
        f.write_bytes(b"0123456789")

        pio = _ProgressFileIO(str(f), 0, 5, lambda n: None)
        result = pio.read(999)
        assert result == b"01234"
        pio.read(-1)  # 触发 pending 刷新
        assert pio.reported == 5
        pio.close()

    def test_close_idempotent(self, tmp_path):
        """多次 close 不报错。"""
        f = tmp_path / "test.bin"
        f.write_bytes(b"data")

        pio = _ProgressFileIO(str(f), 0, 4, lambda n: None)
        pio.close()
        pio.close()  # 不应抛异常

    def test_len_returns_size(self, tmp_path):
        """__len__ 返回构造时的 size 而非文件大小。"""
        f = tmp_path / "test.bin"
        f.write_bytes(b"0123456789")

        pio = _ProgressFileIO(str(f), 0, 3, lambda n: None)
        assert len(pio) == 3
        pio.close()


# ── _parse_json_response 单元测试 ──


class TestParseJsonResponse:
    def test_parse_json_returns_dict(self):
        resp = MagicMock()
        resp.json.return_value = {"code": 0, "message": "ok"}
        resp.status_code = 200

        result = _parse_json_response(resp)
        assert result == {"code": 0, "message": "ok"}

    def test_parse_json_raises_on_invalid(self):
        resp = MagicMock()
        resp.json.side_effect = ValueError("bad json")
        resp.status_code = 200
        resp.text = "not json"

        with pytest.raises(RuntimeError, match="JSON 解析失败"):
            _parse_json_response(resp)

    def test_parse_json_closes_response(self):
        resp = MagicMock()
        resp.json.return_value = {"code": 0}

        _parse_json_response(resp)
        resp.close.assert_called()


# ── format_file_size 单元测试 ──


class TestFormatFileSize:
    def test_format_file_size_bytes(self):
        assert format_file_size(500) == "500 B"

    def test_format_file_size_kb(self):
        assert format_file_size(2048) == "2.0 KB"

    def test_format_file_size_mb(self):
        assert format_file_size(2 * 1024 * 1024) == "2.0 MB"

    def test_format_file_size_gb(self):
        assert format_file_size(2 * 1024 ** 3) == "2.0 GB"

    def test_format_file_size_zero(self):
        assert format_file_size(0) == "0 B"


# ── _calculate_file_md5 单元测试 ──


class TestCalculateFileMd5:
    def test_calculate_md5_correct_hash(self, tmp_path):
        f = tmp_path / "test.bin"
        content = b"hello world"
        f.write_bytes(content)
        expected = hashlib.md5(content).hexdigest()

        err, md5_hex = _calculate_file_md5(str(f), len(content))
        assert err is None
        assert md5_hex == expected

    def test_calculate_md5_cancelled(self, tmp_path):
        f = tmp_path / "test.bin"
        f.write_bytes(b"data")
        task = MagicMock()
        task.is_cancelled = True

        err, md5_hex = _calculate_file_md5(str(f), 4, task=task)
        assert err == "已取消"
        assert md5_hex == ""

    def test_calculate_md5_emits_progress(self, tmp_path):
        f = tmp_path / "test.bin"
        content = b"x" * (2 * 1024 * 1024)
        f.write_bytes(content)
        signals = MagicMock()

        _calculate_file_md5(str(f), len(content), signals=signals)
        signals.progress.emit.assert_called()


class TestPrefetchResultSlot:
    def test_publish_drops_stale_prefetch_result(self):
        slot = _PrefetchResultSlot()
        slot.reserve("new-prefetch", 2)

        slot.publish("old-prefetch", 1, url="https://upload.example/old")

        assert slot.consume("new-prefetch", 2) is None

    def test_consume_only_returns_matching_part_result(self):
        slot = _PrefetchResultSlot()
        slot.reserve("prefetch-1", 3)
        slot.publish("prefetch-1", 3, url="https://upload.example/3")

        assert slot.consume("prefetch-1", 2) is None
        result = slot.consume("prefetch-1", 3)

        assert result == {
            "part_number": 3,
            "url": "https://upload.example/3",
            "error": None,
        }


# ── _RWLock 单元测试 ──


class TestRWLock:
    def test_rwlock_multiple_reads_allowed(self):
        lock = _RWLock()
        results = []
        barrier = threading.Barrier(2, timeout=5)

        def reader(idx):
            with lock.rlock():
                barrier.wait()  # 两个读线程同时持有读锁
                results.append(idx)

        t1 = threading.Thread(target=reader, args=(1,))
        t2 = threading.Thread(target=reader, args=(2,))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        assert sorted(results) == [1, 2]

    def test_rwlock_write_exclusive(self):
        lock = _RWLock()
        write_held = threading.Event()
        write_blocked = threading.Event()

        def writer():
            with lock.wlock():
                write_held.set()
                write_blocked.wait(timeout=5)

        t = threading.Thread(target=writer)
        t.start()
        write_held.wait(timeout=5)
        # 写锁持有期间，再次获取写锁应阻塞
        acquired = threading.Event()

        def try_write():
            with lock.wlock():
                acquired.set()

        t2 = threading.Thread(target=try_write)
        t2.start()
        assert not acquired.is_set()
        write_blocked.set()
        t.join(timeout=5)
        t2.join(timeout=5)
        assert acquired.is_set()


# ── _reset_transient_failure_count 单元测试 ──


def test_reset_transient_failure_count_sets_zero():
    counter = [5]
    _reset_transient_failure_count(counter)
    assert counter[0] == 0


# ============================================================================
# ── 批次 2 追加：补齐 src/app/common/api.py 覆盖缺口（目标 ≥98%）──
# ============================================================================

from src.app.common import api as api_module  # noqa: E402
from src.app.common.api import (  # noqa: E402
    RATE_LIMIT_CODES,
    TokenExpiredError,
    _calculate_file_part_md5,
    _merge_uploaded_parts,
    _normalize_etag,
)


def _api_signals(*channels):
    """构造仅含指定通道的 signals 对象（源码以 hasattr 探测通道是否存在）。"""
    return SimpleNamespace(**{name: MagicMock() for name in channels})


def _configure_upload_db(mock_db, max_threads=1, retries=3):
    """配置 Database.instance() mock 的 get_config 返回值。"""

    def _get_config(key, default=None):
        return {
            "maxUploadThreads": max_threads,
            "retryMaxAttempts": retries,
        }.get(key, default)

    mock_db.return_value.get_config.side_effect = _get_config


def _put_response(status_code=200, etag='"etag-1"'):
    """session.put 响应：支持 with 上下文、raise_for_status 与 ETag 头。"""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {"ETag": etag}
    resp.raise_for_status.return_value = None
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def _presigned_response(part_numbers):
    """s3_repare_upload_parts_batch 响应：为指定分块签发预签名 URL。"""
    return _mock_response(200, {
        "code": 0,
        "data": {
            "presignedUrls": {
                str(pn): f"https://upload.example/{pn}" for pn in part_numbers
            },
        },
    })


def _upload_ok_response(file_id=123):
    """upload_request 响应：非复用，返回完整 S3 会话信息。"""
    return _mock_response(200, {
        "code": 0,
        "data": {
            "Reuse": False,
            "Bucket": "bucket",
            "StorageNode": "node",
            "Key": "key",
            "UploadId": "upload-id",
            "FileId": file_id,
        },
    })


def _drain_progress(data):
    """模拟 requests 消费 file-like：读空后触发进度回调（probe 转正依赖此路径）。"""
    while data.read(65536):
        pass


class _CancellableTask:
    """可在任意时机置为已取消的上传任务替身。"""

    def __init__(self):
        self._cancelled = False
        self.pause_requested = False

    def cancel(self):
        self._cancelled = True

    @property
    def is_cancelled(self):
        return self._cancelled


class TestPan123Init:
    """Pan123.__init__ 真实构造路径。"""

    def test_init_with_explicit_credentials(self, tmp_path, monkeypatch):
        _use_temp_db(tmp_path, monkeypatch)
        pan = Pan123(readfile=False, user_name="u1", password="p1")
        try:
            assert isinstance(pan.session, requests.Session)
            assert isinstance(pan._session_lock, _RWLock)
            assert pan.user_name == "u1"
            assert pan.password == "p1"
            assert pan.authorization == ""
            assert pan.header_logined["authorization"] == ""
            assert pan.parent_file_id == 0
            assert pan.parent_file_list == [0]
            assert pan.on_token_expired is None
            assert pan._login_lock is not None
        finally:
            pan.close()

    def test_init_raises_when_credentials_missing(self, tmp_path, monkeypatch):
        _use_temp_db(tmp_path, monkeypatch)
        with pytest.raises(Exception, match="用户名或密码为空"):
            Pan123(readfile=False, user_name="", password="")

    def test_init_readfile_loads_saved_account(self, tmp_path, monkeypatch):
        db = _use_temp_db(tmp_path, monkeypatch)
        db.set_many_config({
            "deviceType": "MI6",
            "osVersion": "Android_13",
            "loginuuid": "uuid-x",
            "userName": "dbuser",
        })

        with patch("src.app.common.credential_store.load_credential") as mock_load:
            mock_load.side_effect = lambda key: {
                "passWord": "dbpwd",
                "authorization": "Bearer dbauth",
            }.get(key, "")
            pan = Pan123()
        try:
            assert pan.user_name == "dbuser"
            assert pan.password == "dbpwd"
            assert pan.authorization == "Bearer dbauth"
            assert pan.devicetype == "MI6"
            assert pan.osversion == "Android_13"
            assert pan.loginuuid == "uuid-x"
            assert pan.header_logined["authorization"] == "Bearer dbauth"
        finally:
            pan.close()

    def test_read_ini_raises_when_credential_store_fails(self, tmp_path, monkeypatch):
        _use_temp_db(tmp_path, monkeypatch)
        with patch.object(Pan123, "__init__", lambda self, **kw: None):
            pan = Pan123()
        with patch(
            "src.app.common.credential_store.load_credential",
            side_effect=RuntimeError("keyring 不可用"),
        ):
            with pytest.raises(RuntimeError, match="无法从配置获取账号信息"):
                pan.read_ini("", "", False)


class TestPan123Close:
    def test_close_closes_session(self, pan):
        pan.session = MagicMock()
        pan.close()
        pan.session.close.assert_called_once()

    def test_close_swallows_session_error(self, pan):
        pan.session = MagicMock()
        pan.session.close.side_effect = RuntimeError("boom")
        pan.close()  # 不应抛出


class TestPrefetchResultSlotReset:
    def test_clear_empties_slot_and_is_idempotent(self):
        slot = _PrefetchResultSlot()
        slot.reserve("pf-1", 2)
        slot.publish("pf-1", 2, url="https://upload.example/2")

        slot.clear()

        assert slot.consume("pf-1", 2) is None
        slot.clear()  # 重复 clear 幂等
        assert slot.consume("pf-1", 2) is None


class TestProgressFileIoEdgeCases:
    def test_context_manager_closes_file(self, tmp_path):
        f = tmp_path / "bin"
        f.write_bytes(b"0123")

        with _ProgressFileIO(str(f), 0, 4, lambda n: None) as pio:
            pio.read(-1)
            pio.read(-1)  # 触发 pending 刷新

        assert pio._f.closed

    def test_read_warns_when_file_ends_early(self, tmp_path):
        f = tmp_path / "bin"
        f.write_bytes(b"01234")  # 实际 5 字节，声明 10

        pio = _ProgressFileIO(str(f), 0, 10, lambda n: None)
        assert pio.read(-1) == b"01234"
        assert pio.read(-1) == b""  # 第二次读取触发"提前结束"告警分支
        pio.close()

    def test_read_warns_when_remaining_going_negative(self):
        pio = object.__new__(_ProgressFileIO)
        fake_file = MagicMock()
        fake_file.read.return_value = b"0123456789"  # 超量返回，模拟异常数据源
        fake_file.tell.return_value = 0
        pio._f = fake_file
        pio._size = 5
        pio._remaining = 5
        pio._cb = lambda n: None
        pio._pending = 0
        pio.reported = 0

        assert pio.read(-1) == b"0123456789"
        assert pio._remaining == -5  # 触发负值告警分支
        assert pio.read(-1) == b""  # 下一次读取冲销 pending
        assert pio.reported == 10


class TestCalculateFileMd5Extra:
    def test_calculate_md5_paused(self, tmp_path):
        f = tmp_path / "bin"
        f.write_bytes(b"data")
        task = MagicMock()
        task.is_cancelled = False
        task.pause_requested = True

        err, digest = _calculate_file_md5(str(f), 4, task=task)

        assert err == "已暂停"
        assert digest == ""

    def test_calculate_md5_records_speed(self, tmp_path):
        f = tmp_path / "bin"
        f.write_bytes(b"x")
        signals = MagicMock()
        tracker = MagicMock()

        err, digest = _calculate_file_md5(
            str(f), 1, signals=signals, speed_tracker=tracker,
        )

        assert err is None
        tracker.record.assert_called_once_with(1)
        signals.progress.emit.assert_called_once_with(100)


class TestCalculateFilePartMd5:
    @pytest.mark.parametrize("attr,expected", [
        ("is_cancelled", "已取消"),
        ("pause_requested", "已暂停"),
    ])
    def test_stop_states(self, tmp_path, attr, expected):
        f = tmp_path / "bin"
        f.write_bytes(b"data")
        task = MagicMock()
        task.is_cancelled = attr == "is_cancelled"
        task.pause_requested = attr == "pause_requested"

        err, digest = _calculate_file_part_md5(str(f), 0, 4, task=task)

        assert err == expected
        assert digest == ""

    def test_short_read_raises(self, tmp_path):
        f = tmp_path / "bin"
        f.write_bytes(b"ab")  # 声明 4 字节实际 2

        with pytest.raises(RuntimeError, match="读取上传分块失败"):
            _calculate_file_part_md5(str(f), 0, 4)


class TestPureHelperTables:
    @pytest.mark.parametrize("raw,expected", [
        ('"ABC123"', "abc123"),
        ("  ETag  ", "etag"),
        ("", ""),
        (None, ""),
    ])
    def test_normalize_etag_table(self, raw, expected):
        assert _normalize_etag(raw) == expected

    def test_merge_uploaded_parts_sorts_and_normalizes(self):
        merged = _merge_uploaded_parts(
            {2: {"ETag": '"b"', "PartNumber": 2}},
            {1: {"ETag": "a", "PartNumber": "1"}},
        )

        assert merged == [
            {"ETag": "a", "PartNumber": 1},
            {"ETag": '"b"', "PartNumber": 2},
        ]

    @pytest.mark.parametrize("size,expected", [
        (0, "0 B"),
        (1024, "1024 B"),
        (1025, "1.0 KB"),
        (1048576, "1024.0 KB"),
        (1048577, "1.0 MB"),
        (1073741824, "1024.0 MB"),
        (1073741825, "1.0 GB"),
    ])
    def test_format_file_size_boundaries(self, size, expected):
        assert format_file_size(size) == expected


class TestLoginEdgeCases:
    def test_login_missing_token_returns_minus_one(self, pan):
        resp = _mock_response(200, {"code": 200, "data": {}})

        with patch.object(pan.session, "post", return_value=resp):
            assert pan.login() == -1

    def test_prepare_kwargs_defaults_to_logined_headers(self, pan):
        kwargs = pan._prepare_request_kwargs({})
        assert kwargs["headers"] is pan.header_logined

    def test_expired_token_without_password_notifies_and_raises(self, pan):
        pan.password = ""
        pan.on_token_expired = MagicMock()

        with pytest.raises(TokenExpiredError, match="无法自动刷新"):
            pan._refresh_token_for_request("Bearer token123")

        pan.on_token_expired.assert_called_once()

    def test_expired_token_without_password_and_callback_still_raises(self, pan):
        pan.password = ""
        pan.on_token_expired = None

        with pytest.raises(TokenExpiredError):
            pan._refresh_token_for_request("Bearer token123")

    @pytest.mark.parametrize("status_code", sorted(RATE_LIMIT_CODES))
    def test_raw_request_raises_on_rate_limit_status(self, pan, status_code):
        resp = _mock_response(status_code, {"code": -1})
        method = MagicMock(return_value=resp)

        with pytest.raises(RateLimitError, match=str(status_code)):
            pan._raw_request(method, "https://example.com/x")

        method.assert_called_once_with("https://example.com/x")

    def test_save_file_swallows_database_error(self, pan):
        with patch(
            "src.app.common.api.Database.instance",
            side_effect=RuntimeError("db down"),
        ):
            pan.save_file()  # 不应抛出


class TestGetDirFailureBranches:
    def test_rate_limited_returns_minus_one(self, pan):
        with patch.object(pan.session, "get", side_effect=RateLimitError("429")):
            assert pan.get_dir_by_id(0) == (-1, [])

    def test_connection_error_returns_minus_one(self, pan):
        with patch.object(
            pan.session, "get",
            side_effect=requests.exceptions.ConnectionError("重置"),
        ):
            assert pan.get_dir_by_id(0) == (-1, [])

    def test_generic_request_exception_returns_minus_one(self, pan):
        with patch.object(
            pan.session, "get",
            side_effect=requests.exceptions.RequestException("其他异常"),
        ):
            assert pan.get_dir_by_id(0) == (-1, [])

    def test_many_pages_triggers_pause_warning(self, pan):
        resp = _mock_response(200, {
            "code": 0,
            "data": {"Total": 5, "InfoList": [{"FileId": 1}]},
        })

        with patch.object(pan.session, "get", return_value=resp), \
             patch("src.app.common.api.time.sleep") as mock_sleep:
            code, items = pan.get_dir_by_id(0, all=True)

        assert code == 0
        assert len(items) == 5
        mock_sleep.assert_called_once_with(3)


class TestDownloadLinkBranches:
    @staticmethod
    def _redirect_response(headers, text):
        resp = MagicMock()
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        resp.headers = headers
        resp.text = text
        return resp

    def test_folder_uses_batch_download_info_url(self, pan):
        api_resp = _mock_response(
            200, {"code": 0, "data": {"DownloadUrl": "https://x/jump"}},
        )
        redirect = self._redirect_response(
            {"Location": "https://dl.example/f.bin"}, "",
        )

        with patch.object(pan, "_api_request", return_value=api_resp) as mock_api, \
             patch.object(pan, "_raw_request", return_value=redirect):
            result = pan.link_by_fileDetail({"Type": 1, "FileId": 9}, showlink=False)

        assert result == "https://dl.example/f.bin"
        assert mock_api.call_args.args[1].endswith("batch_download_info")

    def test_api_error_returns_code(self, pan):
        resp = _mock_response(200, {"code": 5, "message": "受限"})

        with patch.object(pan, "_api_request", return_value=resp):
            assert pan.link_by_fileDetail({"Type": 0, "FileId": 1}) == 5

    def test_missing_download_url_returns_minus_one(self, pan):
        resp = _mock_response(200, {"code": 0, "data": {}})

        with patch.object(pan, "_api_request", return_value=resp):
            assert pan.link_by_fileDetail({"Type": 0, "FileId": 1}) == -1

    def test_showlink_logs_redirect_host(self, pan):
        api_resp = _mock_response(
            200, {"code": 0, "data": {"DownloadUrl": "https://x/jump"}},
        )
        redirect = self._redirect_response(
            {"Location": "https://dl.example/f.bin"}, "",
        )

        with patch.object(pan, "_api_request", return_value=api_resp), \
             patch.object(pan, "_raw_request", return_value=redirect):
            result = pan.link_by_fileDetail({"Type": 0, "FileId": 1}, showlink=True)

        assert result == "https://dl.example/f.bin"


class TestFileOperationErrors:
    def test_delete_failure_raises(self, pan):
        resp = _mock_response(200, {"code": -1, "message": "删除失败"})

        with patch.object(pan.session, "post", return_value=resp):
            with pytest.raises(RuntimeError, match="删除文件失败"):
                pan.delete_file({"FileId": 1})


class TestMoveFile:
    def test_move_success(self, pan):
        resp = _mock_response(200, {"code": 0, "message": "ok"})

        with patch.object(pan.session, "post", return_value=resp) as mock_post:
            assert pan.move_file([1, 2], 5) is True

        payload = json.loads(mock_post.call_args.kwargs["data"])
        assert payload["fileIdList"] == [{"FileId": 1}, {"FileId": 2}]
        assert payload["parentFileId"] == 5

    def test_move_failure(self, pan):
        resp = _mock_response(200, {"code": -1, "message": "移动失败"})

        with patch.object(pan.session, "post", return_value=resp):
            assert pan.move_file([1], 5) is False


class TestUserInfo:
    def test_success_returns_data(self, pan):
        resp = _mock_response(200, {"code": 0, "data": {"VIP": 1}})

        with patch.object(pan.session, "get", return_value=resp):
            assert pan.user_info() == {"VIP": 1}

    def test_failure_returns_none(self, pan):
        resp = _mock_response(200, {"code": -1, "message": "未登录"})

        with patch.object(pan.session, "get", return_value=resp):
            assert pan.user_info() is None


class TestQrPollExtra:
    def test_api_error_raises(self, pan):
        resp = _mock_response(200, {"code": -1, "message": "会话过期"})

        with patch.object(pan.session, "get", return_value=resp):
            with pytest.raises(RuntimeError, match="轮询扫码状态失败"):
                pan.qr_poll("uni")


class TestQrWxCode:
    def test_success_returns_wx_code(self, pan):
        resp = _mock_response(200, {"code": 0, "data": {"wxCode": "wx-1"}})

        with patch.object(pan.session, "post", return_value=resp) as mock_post:
            assert pan.qr_wx_code("uni") == "wx-1"

        assert mock_post.call_args.kwargs["json"] == {"uniID": "uni"}

    def test_api_error_raises(self, pan):
        resp = _mock_response(200, {"code": -1, "message": "失败"})

        with patch.object(pan.session, "post", return_value=resp):
            with pytest.raises(RuntimeError, match="获取 wxCode 失败"):
                pan.qr_wx_code("uni")


class TestFileDetails:
    def test_success_returns_data(self, pan):
        resp = _mock_response(200, {"code": 0, "data": {"paths": ["/a"]}})

        with patch.object(pan.session, "post", return_value=resp):
            assert pan.file_details([1]) == {"paths": ["/a"]}

    def test_failure_returns_none(self, pan):
        resp = _mock_response(200, {"code": -1, "message": "失败"})

        with patch.object(pan.session, "post", return_value=resp):
            assert pan.file_details([1]) is None


class TestShareExtra:
    def test_missing_share_key_raises(self, pan):
        resp = _mock_response(200, {"code": 0, "data": {}})

        with patch.object(pan.session, "post", return_value=resp):
            with pytest.raises(RuntimeError, match="分享响应缺少 ShareKey"):
                pan.share([1])


class TestCreateDirectoryMissingFileId:
    def test_success_without_file_id_raises(self, pan):
        resp = _mock_response(200, {"code": 0, "data": {"Info": {}}})

        with patch.object(pan.session, "post", return_value=resp):
            with pytest.raises(RuntimeError, match="成功但未返回 FileId"):
                pan._create_directory(0, "docs")


class TestDirectoryMapHelpers:
    def test_get_dir_items_by_id_returns_items(self, pan):
        with patch.object(
            pan, "get_dir_by_id", return_value=(0, [{"FileId": 1}]),
        ) as mock_get:
            assert pan._get_dir_items_by_id(3) == [{"FileId": 1}]

        mock_get.assert_called_once_with(3, all=True, limit=100)

    def test_get_dir_items_by_id_raises_on_error(self, pan):
        with patch.object(pan, "get_dir_by_id", return_value=(2, [])):
            with pytest.raises(RuntimeError, match="获取目录失败，返回码: 2"):
                pan._get_dir_items_by_id(3)

    def test_child_directory_map_keeps_directories_only(self, pan):
        items = [
            {"Type": 1, "FileName": "dir", "FileId": 1},
            {"Type": 0, "FileName": "file.txt", "FileId": 2},
            {"Type": "", "FileName": "bad", "FileId": 3},
        ]

        with patch.object(pan, "_get_dir_items_by_id", return_value=items):
            assert pan._get_child_directory_map(0) == {"dir": 1}

    def test_child_directory_map_normalizes_names(self, pan, monkeypatch):
        monkeypatch.setattr("src.app.common.filename_utils.sys.platform", "win32")
        items = [{"Type": 1, "FileName": "dir.", "FileId": 7}]

        with patch.object(pan, "_get_dir_items_by_id", return_value=items):
            assert pan._get_child_directory_map(0, normalize_names=True) == {"dir": 7}


class TestEnsureDirectoryCreation:
    def test_creates_when_missing(self, pan):
        with patch.object(pan, "_get_child_directory_map", return_value={}), \
             patch.object(
                 pan, "_create_directory_with_backoff", return_value=55,
             ) as mock_create:
            assert pan.ensure_directory(0, "docs") == 55

        mock_create.assert_called_once_with(0, "docs")

    def test_raises_when_create_returns_falsy(self, pan):
        with patch.object(pan, "_get_child_directory_map", return_value={}), \
             patch.object(pan, "_create_directory_with_backoff", return_value=None):
            with pytest.raises(RuntimeError, match="创建目录失败: docs"):
                pan.ensure_directory(0, "docs")


class TestPrepareFolderUploadValidation:
    def test_missing_local_dir_raises(self, pan, tmp_path):
        with pytest.raises(FileNotFoundError, match="文件夹不存在"):
            pan.prepare_folder_upload(tmp_path / "nope", 0)

    def test_local_file_raises_not_a_directory(self, pan, tmp_path):
        target = tmp_path / "file.txt"
        target.write_text("x", encoding="utf-8")

        with pytest.raises(NotADirectoryError, match="不是文件夹"):
            pan.prepare_folder_upload(target, 0)

    def test_root_create_failure_raises(self, pan, tmp_path):
        root = tmp_path / "docs"
        root.mkdir()

        with patch.object(pan, "_get_child_directory_map", return_value={}), \
             patch.object(pan, "_create_directory_with_backoff", return_value=None), \
             patch.object(pan, "delete_file") as mock_delete:
            with pytest.raises(RuntimeError, match="创建顶层目录失败: docs"):
                pan.prepare_folder_upload(root, 0)

        mock_delete.assert_not_called()

    def test_child_create_failure_rolls_back(self, pan, tmp_path):
        root = tmp_path / "docs"
        (root / "sub").mkdir(parents=True)

        with patch.object(pan, "_get_child_directory_map", return_value={}), \
             patch.object(
                 pan, "_create_directory_with_backoff", side_effect=[7, None],
             ), patch.object(pan, "delete_file") as mock_delete:
            with pytest.raises(RuntimeError, match="创建目录失败: sub"):
                pan.prepare_folder_upload(root, 0)

        mock_delete.assert_called_once_with(
            {"FileId": 7, "Type": 1, "FileName": "docs"},
        )

    def test_merge_mode_skips_existing_remote_files(self, pan, tmp_path):
        root = tmp_path / "docs"
        root.mkdir()
        (root / "a.txt").write_text("a", encoding="utf-8")
        (root / "b.txt").write_text("b", encoding="utf-8")

        def fake_dir_map(parent_id, *, normalize_names=False):
            if parent_id == 0:
                return {"docs": 7}
            return {}

        def fake_items(parent_id):
            if parent_id == 7:
                return [{"Type": 0, "FileName": "a.txt", "FileId": 9}]
            return []

        with patch.object(pan, "_get_child_directory_map", side_effect=fake_dir_map), \
             patch.object(pan, "_get_dir_items_by_id", side_effect=fake_items), \
             patch.object(pan, "_create_directory_with_backoff") as mock_create:
            plan = pan.prepare_folder_upload(root, 0, merge=True)

        assert plan["root_dir_id"] == 7
        assert plan["created_dir_count"] == 0
        assert [t["file_name"] for t in plan["file_targets"]] == ["b.txt"]
        mock_create.assert_not_called()

    def test_rollback_failure_is_swallowed_and_reraises_original(self, pan, tmp_path):
        root = tmp_path / "docs"
        root.mkdir()
        target_file = root / "a.txt"
        target_file.write_text("a", encoding="utf-8")
        original_stat = Path.stat

        def fake_stat(path_obj, **kwargs):
            if path_obj == target_file:
                raise OSError("stat failed")
            return original_stat(path_obj)

        with patch.object(pan, "_get_child_directory_map", return_value={}), \
             patch.object(pan, "_create_directory_with_backoff", return_value=101), \
             patch.object(
                 pan, "delete_file", side_effect=RuntimeError("回滚失败"),
             ), patch(
                 "src.app.common.api.Path.stat", autospec=True, side_effect=fake_stat,
             ):
            with pytest.raises(OSError, match="stat failed"):
                pan.prepare_folder_upload(root, 0)


class TestListUploadedPartsValidation:
    def test_invalid_part_number_raises(self, pan):
        resp = _mock_response(200, {
            "code": 0,
            "data": {"parts": [{"PartNumber": 0, "ETag": "x"}]},
        })

        with patch.object(pan, "_api_request", return_value=resp):
            with pytest.raises(RuntimeError, match="服务端返回了无效分块编号"):
                pan._list_uploaded_parts({}, "bucket", "key", "uid", "node")

    def test_success_returns_parts_keyed_by_number(self, pan):
        resp = _mock_response(200, {
            "code": 0,
            "data": {"parts": [
                {"PartNumber": "2", "ETag": '"b"'},
                {"PartNumber": 1, "ETag": '"a"'},
            ]},
        })

        with patch.object(pan, "_api_request", return_value=resp):
            parts = pan._list_uploaded_parts({}, "bucket", "key", "uid", "node")

        assert parts == {
            1: {"ETag": '"a"', "PartNumber": 1},
            2: {"ETag": '"b"', "PartNumber": 2},
        }


class TestValidateResumeServerParts:
    def test_part_number_beyond_total_raises(self, pan, tmp_path):
        f = tmp_path / "bin"
        f.write_bytes(b"data")

        with pytest.raises(RuntimeError, match="服务端分块编号越界"):
            pan._validate_resume_server_parts(str(f), 4, 4, 1, {5: {"ETag": "x"}})

    def test_non_positive_part_size_raises(self, pan, tmp_path):
        f = tmp_path / "bin"
        f.write_bytes(b"data")

        with pytest.raises(RuntimeError, match="服务端分块大小无效"):
            pan._validate_resume_server_parts(str(f), 0, 4, 1, {1: {"ETag": "x"}})

    def test_task_stop_state_propagates(self, pan, tmp_path):
        f = tmp_path / "bin"
        f.write_bytes(b"data")
        task = MagicMock()
        task.is_cancelled = True
        task.pause_requested = False

        stop_state, matched = pan._validate_resume_server_parts(
            str(f), 4, 4, 1, {1: {"ETag": "whatever"}}, task=task,
        )

        assert stop_state == "已取消"
        assert matched is False


class TestUploadFileStreamGuards:
    def test_windows_style_path_separator_normalized(self, pan, tmp_path, monkeypatch):
        local_file = tmp_path / "win.txt"
        local_file.write_text("demo", encoding="utf-8")
        # os.name 是模块级全局属性；Path 按运行时 os.name 分派，
        # 需同时固定为 PosixPath 保证文件 stat 正常（monkeypatch 结束后还原）
        monkeypatch.setattr(api_module.os, "name", "nt")
        monkeypatch.setattr(api_module, "Path", PosixPath)
        resp = _mock_response(200, {"code": 0, "data": {"Reuse": True}})

        with patch.object(pan, "_api_request", side_effect=[resp]):
            assert pan.upload_file_stream(str(local_file)) == "复用上传成功"

    def test_missing_file_raises(self, pan, tmp_path):
        with pytest.raises(FileNotFoundError, match="文件不存在"):
            pan.upload_file_stream(str(tmp_path / "ghost.txt"))

    def test_directory_raises(self, pan, tmp_path):
        d = tmp_path / "dir"
        d.mkdir()

        with pytest.raises(IsADirectoryError, match="不支持文件夹上传"):
            pan.upload_file_stream(str(d))


def _write_resume_file(tmp_path, content):
    local_file = tmp_path / "resume.bin"
    local_file.write_bytes(content)
    return local_file


def _base_resume_info(content, total_parts=1, block_size=4, **overrides):
    info = {
        "bucket": "bucket",
        "storage_node": "node",
        "upload_key": "key",
        "upload_id": "upload-id",
        "up_file_id": 123,
        "total_parts": total_parts,
        "block_size": block_size,
        "etag": hashlib.md5(content).hexdigest(),
    }
    info.update(overrides)
    return info


class TestUploadResumeGuards:
    def test_resume_without_etag_falls_back_to_new_upload(self, pan, tmp_path):
        local_file = _write_resume_file(tmp_path, b"demo")
        upload_res = _mock_response(200, {"code": 0, "data": {"Reuse": True}})

        with patch.object(pan, "_api_request", side_effect=[upload_res]) as mock_api:
            result = pan.upload_file_stream(
                str(local_file), resume_info={"upload_id": "uid"},
            )

        assert result == "复用上传成功"
        assert mock_api.call_args.args[1].endswith("upload_request")

    def test_resume_mtime_unchanged_skips_md5(self, pan, tmp_path):
        content = b"demo"
        local_file = _write_resume_file(tmp_path, content)
        resume_info = _base_resume_info(
            content,
            file_size=len(content),
            file_mtime=local_file.stat().st_mtime,
        )
        signals = _api_signals("status", "progress")
        server_parts_res = _mock_response(200, {"code": 0, "data": {"parts": []}})
        init_res = _mock_response(200, {"code": 0})
        list_final = _mock_response(200, {"code": 0, "data": {"parts": []}})

        with patch.object(api_module, "_calculate_file_md5") as mock_md5, \
             patch.object(pan, "_api_request", side_effect=[
                 server_parts_res, init_res, _presigned_response([1]),
                 list_final, _mock_response(200, {"code": 0}),
                 _mock_response(200, {"code": 0}),
             ]), patch.object(pan.session, "put", return_value=_put_response()), \
             patch("src.app.common.api.Database.instance") as mock_db:
            _configure_upload_db(mock_db)
            result = pan.upload_file_stream(
                str(local_file), resume_info=resume_info,
                signals=signals, speed_tracker=MagicMock(),
            )

        assert result == 123
        mock_md5.assert_not_called()  # 大小/mtime 未变，跳过 MD5 校验
        signals.status.emit.assert_any_call("上传中")

    def test_resume_mtime_changed_recomputes_md5_and_matches(self, pan, tmp_path):
        content = b"demo"
        local_file = _write_resume_file(tmp_path, content)
        resume_info = _base_resume_info(
            content,
            file_size=len(content),
            file_mtime=local_file.stat().st_mtime - 100,
        )
        signals = _api_signals("status", "progress")
        server_parts_res = _mock_response(200, {"code": 0, "data": {"parts": []}})
        init_res = _mock_response(200, {"code": 0})
        list_final = _mock_response(200, {"code": 0, "data": {"parts": []}})

        with patch.object(pan, "_api_request", side_effect=[
            server_parts_res, init_res, _presigned_response([1]),
            list_final, _mock_response(200, {"code": 0}),
            _mock_response(200, {"code": 0}),
        ]), patch.object(pan.session, "put", return_value=_put_response()), \
             patch("src.app.common.api.Database.instance") as mock_db:
            _configure_upload_db(mock_db)
            result = pan.upload_file_stream(
                str(local_file), resume_info=resume_info, signals=signals,
            )

        assert result == 123
        signals.status.emit.assert_any_call("校验中")

    def test_resume_md5_cancelled_returns(self, pan, tmp_path):
        content = b"demo"
        local_file = _write_resume_file(tmp_path, content)
        resume_info = _base_resume_info(
            content, file_mtime=local_file.stat().st_mtime - 100,
        )
        task = MagicMock()
        task.is_cancelled = True
        task.pause_requested = False

        with patch.object(pan, "_api_request") as mock_api:
            result = pan.upload_file_stream(
                str(local_file), resume_info=resume_info, task=task,
            )

        assert result == "已取消"
        mock_api.assert_not_called()

    def test_resume_validation_stop_returns(self, pan, tmp_path):
        content = b"demo"
        local_file = _write_resume_file(tmp_path, content)
        resume_info = _base_resume_info(content)
        part_etag = f'"{hashlib.md5(content).hexdigest()}"'
        server_parts_res = _mock_response(200, {
            "code": 0, "data": {"parts": [{"PartNumber": 1, "ETag": part_etag}]},
        })
        task = MagicMock()
        task.is_cancelled = True
        task.pause_requested = False

        # 绕过整文件 MD5（否则任务在校验前就会取消），
        # 让取消发生在服务端分块校验阶段
        with patch.object(
            api_module, "_calculate_file_md5",
            return_value=(None, hashlib.md5(content).hexdigest()),
        ), patch.object(pan, "_api_request", side_effect=[server_parts_res]):
            result = pan.upload_file_stream(
                str(local_file), resume_info=resume_info, task=task,
            )

        assert result == "已取消"

    def test_resume_md5_mismatch_resets_and_reuploads(self, pan, tmp_path):
        content = b"demo"
        local_file = _write_resume_file(tmp_path, content)
        resume_info = _base_resume_info(
            content, etag="stale-hash",
            file_mtime=local_file.stat().st_mtime - 100,
        )
        signals = _api_signals("progress")
        tracker = MagicMock()
        upload_res = _mock_response(200, {"code": 0, "data": {"Reuse": True}})

        with patch.object(pan, "_api_request", side_effect=[upload_res]):
            result = pan.upload_file_stream(
                str(local_file), resume_info=resume_info,
                signals=signals, speed_tracker=tracker,
            )

        assert result == "复用上传成功"
        signals.progress.emit.assert_called_once_with(0)
        tracker.reset.assert_called_once()

    def test_resume_parts_mismatch_resets_and_reuploads(self, pan, tmp_path):
        content = b"demo"
        local_file = _write_resume_file(tmp_path, content)
        resume_info = _base_resume_info(content)
        server_parts_res = _mock_response(200, {
            "code": 0,
            "data": {"parts": [{"PartNumber": 1, "ETag": '"deadbeef"'}]},
        })
        signals = _api_signals("progress")
        tracker = MagicMock()
        upload_res = _mock_response(200, {"code": 0, "data": {"Reuse": True}})

        with patch.object(pan, "_api_request", side_effect=[server_parts_res, upload_res]):
            result = pan.upload_file_stream(
                str(local_file), resume_info=resume_info,
                signals=signals, speed_tracker=tracker,
            )

        assert result == "复用上传成功"
        signals.progress.emit.assert_called_with(0)
        tracker.reset.assert_called_once()

    def test_resume_validation_error_resets_and_reuploads(self, pan, tmp_path):
        content = b"demo"
        local_file = _write_resume_file(tmp_path, content)
        resume_info = _base_resume_info(content)
        signals = _api_signals("progress")
        tracker = MagicMock()
        upload_res = _mock_response(200, {"code": 0, "data": {"Reuse": True}})

        with patch.object(
            pan, "_api_request",
            side_effect=[RuntimeError("网络炸了"), upload_res],
        ):
            result = pan.upload_file_stream(
                str(local_file), resume_info=resume_info,
                signals=signals, speed_tracker=tracker,
            )

        assert result == "复用上传成功"
        signals.progress.emit.assert_called_with(0)
        tracker.reset.assert_called_once()

    def test_new_upload_md5_cancelled_returns(self, pan, tmp_path):
        local_file = _write_resume_file(tmp_path, b"demo")
        task = MagicMock()
        task.is_cancelled = True
        task.pause_requested = False

        with patch.object(pan, "_api_request") as mock_api:
            result = pan.upload_file_stream(str(local_file), task=task)

        assert result == "已取消"
        mock_api.assert_not_called()


class TestUploadRequestCodes:
    def test_duplicate_code_5060_retries_with_choice(self, pan, tmp_path):
        local_file = _write_resume_file(tmp_path, b"demo")
        first = _mock_response(200, {"code": 5060, "message": "duplicate"})
        second = _mock_response(200, {"code": 0, "data": {"Reuse": True}})

        with patch.object(pan, "_api_request", side_effect=[first, second]) as mock_api:
            result = pan.upload_file_stream(str(local_file), dup_choice=2)

        assert result == "复用上传成功"
        second_payload = json.loads(mock_api.call_args_list[1].kwargs["data"])
        assert second_payload["duplicate"] == 2

    def test_duplicate_5060_still_failing_raises(self, pan, tmp_path):
        local_file = _write_resume_file(tmp_path, b"demo")
        first = _mock_response(200, {"code": 5060, "message": "duplicate"})
        second = _mock_response(200, {"code": -1, "message": "nope"})

        with patch.object(pan, "_api_request", side_effect=[first, second]):
            with pytest.raises(RuntimeError, match="上传请求失败"):
                pan.upload_file_stream(str(local_file))

    def test_new_upload_emits_checking_status(self, pan, tmp_path):
        local_file = _write_resume_file(tmp_path, b"demo")
        upload_res = _mock_response(200, {"code": 0, "data": {"Reuse": True}})
        signals = _api_signals("status", "progress", "session_info")

        with patch.object(pan, "_api_request", side_effect=[upload_res]):
            result = pan.upload_file_stream(str(local_file), signals=signals)

        assert result == "复用上传成功"
        emitted = [c.args[0] for c in signals.status.emit.call_args_list]
        assert emitted == ["校验中"]

    def test_init_session_failure_raises(self, pan, tmp_path):
        local_file = _write_resume_file(tmp_path, b"demo")
        init_fail = _mock_response(200, {"code": -1, "message": "boom"})

        with patch.object(
            pan, "_api_request", side_effect=[_upload_ok_response(), init_fail],
        ):
            with pytest.raises(RuntimeError, match="初始化上传会话失败"):
                pan.upload_file_stream(str(local_file))


class TestUploadZeroByte:
    def test_zero_byte_file_completes_directly(self, pan, tmp_path):
        local_file = _write_resume_file(tmp_path, b"")
        signals = _api_signals("status", "progress", "session_info")

        with patch.object(pan, "_api_request", side_effect=[
            _upload_ok_response(file_id=77),
            _mock_response(200, {"code": 0}),
            _mock_response(200, {"code": 0}),
        ]) as mock_api:
            result = pan.upload_file_stream(str(local_file), signals=signals)

        assert result == 77
        signals.progress.emit.assert_any_call(100)
        emitted = [c.args[0] for c in signals.status.emit.call_args_list]
        assert emitted == ["校验中", "上传中"]
        urls = [c.args[1] for c in mock_api.call_args_list]
        assert urls == [
            "https://www.123pan.com/b/api/file/upload_request",
            "https://www.123pan.com/b/api/file/s3_complete_multipart_upload",
            "https://www.123pan.com/b/api/file/upload_complete",
        ]

    def test_zero_byte_complete_retries_exhausted(self, pan, tmp_path):
        local_file = _write_resume_file(tmp_path, b"")
        fail = _mock_response(200, {"code": -1, "message": "not ready"})

        with patch.object(pan, "_api_request", side_effect=[
            _upload_ok_response(file_id=77),
            _mock_response(200, {"code": 0}),
            fail, fail, fail, fail, fail,
        ]), patch("src.app.common.api.time.sleep") as mock_sleep:
            with pytest.raises(RuntimeError, match="0字节文件 upload_complete 超时"):
                pan.upload_file_stream(str(local_file))

        assert mock_sleep.call_count == 4  # 第 2~5 次尝试前退避


class TestUploadResumeProgressSignals:
    def test_resume_with_done_parts_emits_initial_progress(self, pan, tmp_path):
        content = b"abcdefgh"
        local_file = _write_resume_file(tmp_path, content)
        resume_info = _base_resume_info(content, total_parts=2)
        part1_etag = f'"{hashlib.md5(content[:4]).hexdigest()}"'
        server_parts_res = _mock_response(200, {
            "code": 0,
            "data": {"parts": [{"PartNumber": 1, "ETag": part1_etag}]},
        })
        init_res = _mock_response(200, {"code": 0})
        list_final = _mock_response(200, {
            "code": 0,
            "data": {"parts": [{"PartNumber": 1, "ETag": part1_etag}]},
        })
        signals = _api_signals("status", "progress")
        tracker = MagicMock()

        with patch.object(pan, "_api_request", side_effect=[
            server_parts_res, init_res, _presigned_response([2]),
            list_final, _mock_response(200, {"code": 0}),
            _mock_response(200, {"code": 0}),
        ]), patch.object(
            pan.session, "put", return_value=_put_response(etag='"etag-2"'),
        ), patch("src.app.common.api.Database.instance") as mock_db:
            _configure_upload_db(mock_db)
            result = pan.upload_file_stream(
                str(local_file), resume_info=resume_info,
                signals=signals, speed_tracker=tracker,
            )

        assert result == 123
        signals.progress.emit.assert_any_call(50)  # 已完成 4/8 字节
        tracker.record.assert_any_call(4)


class TestUploadPause:
    def test_paused_before_workers_returns_paused(self, pan, tmp_path):
        local_file = _write_resume_file(tmp_path, b"demo")
        task = MagicMock()
        task.is_cancelled = False
        task.pause_requested = False
        upload_res = _upload_ok_response()
        init_res = _mock_response(200, {"code": 0})

        def fake_api(method, url, **kwargs):
            if url.endswith("upload_request"):
                task.pause_requested = True  # 请求发出后立即暂停
                return upload_res
            return init_res

        with patch.object(pan, "_api_request", side_effect=fake_api), \
             patch.object(pan.session, "put") as mock_put, \
             patch("src.app.common.api.Database.instance") as mock_db:
            _configure_upload_db(mock_db)
            result = pan.upload_file_stream(str(local_file), task=task)

        assert result == "已暂停"
        mock_put.assert_not_called()  # 未发起任何分片上传


class TestUploadWorkerFailures:
    def test_presigned_error_code_fails_upload(self, pan, tmp_path):
        local_file = _write_resume_file(tmp_path, b"demo")
        bad_link = _mock_response(200, {"code": -1, "message": "no url"})

        with patch.object(pan, "_api_request", side_effect=[
            _upload_ok_response(), _mock_response(200, {"code": 0}), bad_link,
        ]), patch.object(pan.session, "put") as mock_put, \
             patch("src.app.common.api.Database.instance") as mock_db:
            _configure_upload_db(mock_db, retries=0)
            with pytest.raises(RuntimeError, match="分块上传失败"):
                pan.upload_file_stream(str(local_file))

        mock_put.assert_not_called()

    def test_missing_presigned_url_fails_upload(self, pan, tmp_path):
        local_file = _write_resume_file(tmp_path, b"demo")
        empty_link = _mock_response(200, {"code": 0, "data": {"presignedUrls": {}}})

        with patch.object(pan, "_api_request", side_effect=[
            _upload_ok_response(), _mock_response(200, {"code": 0}), empty_link,
        ]), patch.object(pan.session, "put") as mock_put, \
             patch("src.app.common.api.Database.instance") as mock_db:
            _configure_upload_db(mock_db, retries=0)
            with pytest.raises(RuntimeError, match="分块上传失败"):
                pan.upload_file_stream(str(local_file))

        mock_put.assert_not_called()

    def test_connection_error_exhaustion_triggers_failed(self, pan, tmp_path):
        local_file = _write_resume_file(tmp_path, b"demo")
        side_effects = [_upload_ok_response(), _mock_response(200, {"code": 0})]
        side_effects += [_presigned_response([1])] * 80

        with patch.object(pan, "_api_request", side_effect=side_effects), \
             patch.object(
                 pan.session, "put",
                 side_effect=requests.exceptions.ConnectionError("reset"),
             ), patch("src.app.common.api.time.sleep"), \
             patch("src.app.common.api.Database.instance") as mock_db:
            _configure_upload_db(mock_db, retries=0)
            with pytest.raises(RuntimeError, match="分块上传失败"):
                pan.upload_file_stream(str(local_file))

    def test_too_many_rate_limit_errors_abort_upload(self, pan, tmp_path):
        local_file = _write_resume_file(tmp_path, b"demo")
        side_effects = [_upload_ok_response(), _mock_response(200, {"code": 0})]
        side_effects += [RateLimitError("429")] * 60

        with patch.object(pan, "_api_request", side_effect=side_effects), \
             patch("src.app.common.api.time.sleep"), \
             patch("src.app.common.api.Database.instance") as mock_db:
            _configure_upload_db(mock_db)
            with pytest.raises(RuntimeError, match="分块上传失败"):
                pan.upload_file_stream(str(local_file))

    def test_zero_byte_complete_merge_failure_raises(self, pan, tmp_path):
        local_file = _write_resume_file(tmp_path, b"")
        complete_fail = _mock_response(200, {"code": -1, "message": "merge fail"})

        with patch.object(pan, "_api_request", side_effect=[
            _upload_ok_response(file_id=77), complete_fail,
        ]):
            with pytest.raises(RuntimeError, match="0字节文件合并失败"):
                pan.upload_file_stream(str(local_file))

    def test_prefetch_failure_recomputes_url(self, pan, tmp_path):
        content = b"abcdefgh"  # 2 分片
        local_file = _write_resume_file(tmp_path, content)
        upload_res = _upload_ok_response()
        init_res = _mock_response(200, {"code": 0})
        link_res = _presigned_response([1, 2])
        list_final = _mock_response(200, {"code": 0, "data": {"parts": []}})
        p2_fetches = []

        def fake_api(method, url, **kwargs):
            if url.endswith("upload_request"):
                return upload_res
            if url.endswith("s3_list_upload_parts"):
                return init_res
            if url.endswith("s3_complete_multipart_upload") or url.endswith(
                "upload_complete",
            ):
                return _mock_response(200, {"code": 0})
            start = json.loads(kwargs["data"])["partNumberStart"]
            if start == 1:
                return link_res  # 主流程：分片 1 正常
            p2_fetches.append(1)
            if len(p2_fetches) == 1:
                raise RateLimitError("429")  # 预取线程首次取分片 2 链接失败
            return link_res  # 主流程重新取链接

        with patch.object(pan, "_api_request", side_effect=fake_api), \
             patch.object(
                 pan.session, "put", return_value=_put_response(etag='"e"'),
             ), patch(
                 "src.app.common.api.get_upload_part_size", return_value=4,
             ), patch("src.app.common.api.Database.instance") as mock_db:
            _configure_upload_db(mock_db)
            result = pan.upload_file_stream(str(local_file))

        assert result == 123
        assert len(p2_fetches) == 2  # 预取失败后重新获取

    def test_put_rate_limit_clears_probe_name(self, pan, tmp_path):
        local_file = _write_resume_file(tmp_path, b"demo")
        link_res = _presigned_response([1])
        list_final = _mock_response(200, {"code": 0, "data": {"parts": []}})

        with patch.object(pan, "_api_request", side_effect=[
            _upload_ok_response(),
            _mock_response(200, {"code": 0}),
            link_res, link_res,
            list_final,
            _mock_response(200, {"code": 0}),
            _mock_response(200, {"code": 0}),
        ]), patch.object(pan.session, "put", side_effect=[
            _put_response(status_code=429),  # probe 尚未转正时命中 429
            _put_response(),
        ]), patch("src.app.common.api.time.sleep"), \
             patch("src.app.common.api.Database.instance") as mock_db:
            _configure_upload_db(mock_db)
            result = pan.upload_file_stream(str(local_file))

        assert result == 123

    def test_put_rate_limit_exhaustion_aborts(self, pan, tmp_path):
        local_file = _write_resume_file(tmp_path, b"demo")
        api_effects = [_upload_ok_response(), _mock_response(200, {"code": 0})]
        api_effects += [_presigned_response([1])] * 60

        with patch.object(pan, "_api_request", side_effect=api_effects), \
             patch.object(pan.session, "put", side_effect=[
                 _put_response(status_code=429),
             ] * 60), patch("src.app.common.api.time.sleep"), \
             patch("src.app.common.api.Database.instance") as mock_db:
            _configure_upload_db(mock_db)
            with pytest.raises(RuntimeError, match="分块上传失败"):
                pan.upload_file_stream(str(local_file))

    def test_cancel_abort_failure_is_swallowed(self, pan, tmp_path):
        content = b"abcdefgh"  # 2 分片
        local_file = _write_resume_file(tmp_path, content)
        task = _CancellableTask()
        link_res = _presigned_response([1, 2])

        def fake_put(url, data=None, timeout=None, **kwargs):
            task.cancel()
            return _put_response()

        with patch.object(pan, "_api_request", side_effect=[
            _upload_ok_response(),
            _mock_response(200, {"code": 0}),
            link_res, link_res,
            RuntimeError("abort 接口炸了"),  # abort 失败也应返回"已取消"
        ]), patch.object(pan.session, "put", side_effect=fake_put), \
             patch("src.app.common.api.get_upload_part_size", return_value=4), \
             patch("src.app.common.api.Database.instance") as mock_db:
            _configure_upload_db(mock_db)
            result = pan.upload_file_stream(str(local_file), task=task)

        assert result == "已取消"

    def test_merge_part_count_mismatch_raises(self, pan, tmp_path):
        content = b"abcdefgh"  # 2 分片
        local_file = _write_resume_file(tmp_path, content)
        link_res = _presigned_response([1, 2])
        # 服务端返回了越界分块 3 → 合并数量与预期不符
        list_final = _mock_response(200, {
            "code": 0,
            "data": {"parts": [
                {"PartNumber": 1, "ETag": '"e1"'},
                {"PartNumber": 3, "ETag": '"e3"'},
            ]},
        })

        with patch.object(pan, "_api_request", side_effect=[
            _upload_ok_response(),
            _mock_response(200, {"code": 0}),
            link_res, link_res,
            list_final,
        ]), patch.object(
            pan.session, "put", return_value=_put_response(etag='"e"'),
        ), patch("src.app.common.api.get_upload_part_size", return_value=4), \
             patch("src.app.common.api.Database.instance") as mock_db:
            _configure_upload_db(mock_db, max_threads=2)
            with pytest.raises(RuntimeError, match="上传分块不完整"):
                pan.upload_file_stream(str(local_file))

    def test_complete_multipart_failure_raises(self, pan, tmp_path):
        local_file = _write_resume_file(tmp_path, b"demo")
        link_res = _presigned_response([1])
        list_final = _mock_response(200, {"code": 0, "data": {"parts": []}})
        complete_fail = _mock_response(200, {"code": -1, "message": "merge fail"})

        with patch.object(pan, "_api_request", side_effect=[
            _upload_ok_response(),
            _mock_response(200, {"code": 0}),
            link_res,
            list_final,
            complete_fail,
        ]), patch.object(
            pan.session, "put", return_value=_put_response(etag='"e"'),
        ), patch("src.app.common.api.Database.instance") as mock_db:
            _configure_upload_db(mock_db)
            with pytest.raises(RuntimeError, match="合并上传分块失败"):
                pan.upload_file_stream(str(local_file))

    def test_upload_complete_retry_then_success(self, pan, tmp_path):
        local_file = _write_resume_file(tmp_path, b"demo")
        link_res = _presigned_response([1])
        list_final = _mock_response(200, {"code": 0, "data": {"parts": []}})
        up_not_ready = _mock_response(200, {"code": -1, "message": "not ready"})

        with patch.object(pan, "_api_request", side_effect=[
            _upload_ok_response(),
            _mock_response(200, {"code": 0}),
            link_res,
            list_final,
            _mock_response(200, {"code": 0}),
            up_not_ready,
            _mock_response(200, {"code": 0}),
        ]), patch.object(
            pan.session, "put", return_value=_put_response(etag='"e"'),
        ), patch("src.app.common.api.time.sleep") as mock_sleep, \
             patch("src.app.common.api.Database.instance") as mock_db:
            _configure_upload_db(mock_db)
            result = pan.upload_file_stream(str(local_file))

        assert result == 123
        mock_sleep.assert_called_once_with(2)  # 2 ** attempt, 上限 8

    def test_upload_complete_exhausted_raises(self, pan, tmp_path):
        local_file = _write_resume_file(tmp_path, b"demo")
        link_res = _presigned_response([1])
        list_final = _mock_response(200, {"code": 0, "data": {"parts": []}})
        up_not_ready = _mock_response(200, {"code": -1, "message": "not ready"})

        with patch.object(pan, "_api_request", side_effect=[
            _upload_ok_response(),
            _mock_response(200, {"code": 0}),
            link_res,
            list_final,
            _mock_response(200, {"code": 0}),
            up_not_ready, up_not_ready, up_not_ready, up_not_ready, up_not_ready,
        ]), patch.object(
            pan.session, "put", return_value=_put_response(etag='"e"'),
        ), patch("src.app.common.api.time.sleep"), \
             patch("src.app.common.api.Database.instance") as mock_db:
            _configure_upload_db(mock_db)
            with pytest.raises(RuntimeError, match="上传完成确认失败"):
                pan.upload_file_stream(str(local_file))


class TestUploadWorkerConcurrency:
    """多分片并发路径：预取、probe 转正、限流回队、取消回队。"""

    def test_multi_part_upload_with_prefetch_and_rate_limit_recovery(
        self, pan, tmp_path,
    ):
        content = b"0123456789AB"  # 12 字节 → 3 分片（block_size=4）
        local_file = _write_resume_file(tmp_path, content)
        signals = _api_signals("status", "progress", "session_info", "conn_info", "part_done")
        w0_calls = []

        def fake_put(url, data=None, timeout=None, **kwargs):
            if threading.current_thread().name == "upload_worker_0":
                w0_calls.append(1)
                _drain_progress(data)
                if len(w0_calls) == 2:  # 第二个分片首次上传命中 429
                    return _put_response(status_code=429)
                return _put_response()
            _drain_progress(data)
            return _put_response()

        link_res = _presigned_response([1, 2, 3])
        with patch.object(pan, "_api_request", side_effect=[
            _upload_ok_response(),
            _mock_response(200, {"code": 0}),
            link_res, link_res, link_res, link_res,
            _mock_response(200, {"code": 0, "data": {"parts": []}}),
            _mock_response(200, {"code": 0}),
            _mock_response(200, {"code": 0}),
        ]), patch.object(pan.session, "put", side_effect=fake_put), \
             patch("src.app.common.api.time.sleep"), \
             patch("src.app.common.api.get_upload_part_size", return_value=4), \
             patch("src.app.common.api.Database.instance") as mock_db:
            _configure_upload_db(mock_db, max_threads=2)
            result = pan.upload_file_stream(str(local_file), signals=signals)

        assert result == 123
        assert signals.part_done.emit.call_count == 3
        assert signals.conn_info.emit.called

    def test_allowed_workers_increment_after_successful_part(self, pan, tmp_path):
        content = b"abcdefgh"  # 8 字节 → 2 分片
        local_file = _write_resume_file(tmp_path, content)
        signals = _api_signals("progress", "part_done")
        link_res = _presigned_response([1, 2])

        with patch.object(pan, "_api_request", side_effect=[
            _upload_ok_response(),
            _mock_response(200, {"code": 0}),
            link_res, link_res,
            _mock_response(200, {"code": 0, "data": {"parts": []}}),
            _mock_response(200, {"code": 0}),
            _mock_response(200, {"code": 0}),
        ]), patch.object(
            pan.session, "put", return_value=_put_response(etag='"e"'),
        ), patch("src.app.common.api.get_upload_part_size", return_value=4), \
             patch("src.app.common.api.Database.instance") as mock_db:
            _configure_upload_db(mock_db, max_threads=2)
            result = pan.upload_file_stream(str(local_file), signals=signals)

        assert result == 123
        assert signals.part_done.emit.call_count == 2

    def test_cancel_at_loop_top_requeues_prefetched_part(self, pan, tmp_path):
        content = b"abcdefgh"  # 2 分片
        local_file = _write_resume_file(tmp_path, content)
        task = _CancellableTask()
        link_res = _presigned_response([1, 2])

        def fake_put(url, data=None, timeout=None, **kwargs):
            task.cancel()  # 首个分片上传后置为已取消
            return _put_response()

        with patch.object(pan, "_api_request", side_effect=[
            _upload_ok_response(),
            _mock_response(200, {"code": 0}),
            link_res, link_res,
            _mock_response(200, {"code": 0}),  # abort
        ]) as mock_api, patch.object(pan.session, "put", side_effect=fake_put), \
             patch("src.app.common.api.get_upload_part_size", return_value=4), \
             patch("src.app.common.api.Database.instance") as mock_db:
            _configure_upload_db(mock_db)
            result = pan.upload_file_stream(str(local_file), task=task)

        assert result == "已取消"
        assert any(
            c.args[1].endswith("s3_abort_multipart_upload")
            for c in mock_api.call_args_list
        )

    def test_stop_before_part_upload_requeues_current_part(self, pan, tmp_path):
        local_file = _write_resume_file(tmp_path, b"demo")
        task = _CancellableTask()

        def fake_put(url, data=None, timeout=None, **kwargs):
            task.cancel()
            raise requests.exceptions.ConnectionError("reset")

        with patch.object(pan, "_api_request", side_effect=[
            _upload_ok_response(),
            _mock_response(200, {"code": 0}),
            _presigned_response([1]),
            _mock_response(200, {"code": 0}),  # abort
        ]), patch.object(pan.session, "put", side_effect=fake_put), \
             patch("src.app.common.api.time.sleep"), \
             patch("src.app.common.api.Database.instance") as mock_db:
            _configure_upload_db(mock_db)
            result = pan.upload_file_stream(str(local_file), task=task)

        assert result == "已取消"

    def test_rate_limit_after_connection_error_lowers_allowed_workers(
        self, pan, tmp_path,
    ):
        content = b"abcdefgh"  # 2 分片
        local_file = _write_resume_file(tmp_path, content)
        put_calls = []

        def fake_put(url, data=None, timeout=None, **kwargs):
            put_calls.append(1)
            _drain_progress(data)
            if len(put_calls) == 2:  # 分片 2 首传：先报进度再连接失败
                raise requests.exceptions.ConnectionError("reset")
            return _put_response()

        link_res = _presigned_response([1, 2])
        with patch.object(pan, "_api_request", side_effect=[
            _upload_ok_response(),
            _mock_response(200, {"code": 0}),
            link_res, link_res,
            RateLimitError("429"),  # 重试取链接时命中限流
            link_res,
            _mock_response(200, {"code": 0, "data": {"parts": []}}),
            _mock_response(200, {"code": 0}),
            _mock_response(200, {"code": 0}),
        ]), patch.object(pan.session, "put", side_effect=fake_put), \
             patch("src.app.common.api.time.sleep"), \
             patch("src.app.common.api.get_upload_part_size", return_value=4), \
             patch("src.app.common.api.Database.instance") as mock_db:
            _configure_upload_db(mock_db, max_threads=2)
            result = pan.upload_file_stream(str(local_file))

        assert result == 123
        assert len(put_calls) == 3

    def test_worker_above_allowed_limit_exits(self, pan, tmp_path):
        content = b"0123456789AB"  # 3 分片
        local_file = _write_resume_file(tmp_path, content)
        worker1_started = threading.Event()
        rate_limit_hit = threading.Event()
        w0_calls = []

        def fake_put(url, data=None, timeout=None, **kwargs):
            name = threading.current_thread().name
            if name == "upload_worker_0":
                w0_calls.append(1)
                _drain_progress(data)  # 触发 probe 转正（allowed 1→2）
                if len(w0_calls) == 1:
                    assert worker1_started.wait(timeout=10)
                    return _put_response()
                rate_limit_hit.set()  # 分片 2 首传 429，同时压低 allowed
                return _put_response(status_code=429)
            worker1_started.set()
            assert rate_limit_hit.wait(timeout=10)
            _drain_progress(data)
            return _put_response()

        link_res = _presigned_response([1, 2, 3])
        with patch.object(pan, "_api_request", side_effect=[
            _upload_ok_response(),
            _mock_response(200, {"code": 0}),
            link_res, link_res, link_res, link_res,
            _mock_response(200, {"code": 0, "data": {"parts": []}}),
            _mock_response(200, {"code": 0}),
            _mock_response(200, {"code": 0}),
        ]), patch.object(pan.session, "put", side_effect=fake_put), \
             patch("src.app.common.api.time.sleep"), \
             patch("src.app.common.api.get_upload_part_size", return_value=4), \
             patch("src.app.common.api.Database.instance") as mock_db:
            _configure_upload_db(mock_db, max_threads=2)
            result = pan.upload_file_stream(str(local_file))

        assert result == 123
