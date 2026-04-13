"""测试 Pan123 API 业务方法（login, get_dir, delete, rename, share, mkdir 等）"""
import hashlib
import json
import threading
import time
from pathlib import Path
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

        reported = []
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

        reported = []
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
