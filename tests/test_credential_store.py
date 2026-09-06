"""credential_store 测试：keyring 全 mock，禁止真实 keyring 读写。"""
import importlib
from unittest.mock import MagicMock, patch

import keyring

from src.app.common import credential_store

# ---- SQLite 后备存取（_db_save / _db_load / _db_delete） ----


def test_db_save_load_delete_roundtrip(temp_db):
    credential_store._db_save("password", "secret")
    assert credential_store._db_load("password") == "secret"

    credential_store._db_delete("password")
    assert credential_store._db_load("password") == ""


def test_db_load_missing_key_returns_empty_string(temp_db):
    assert credential_store._db_load("missing") == ""


# ---- keyring 分支（显式置 _use_keyring=True，平台无关） ----


def test_save_credential_writes_keyring(monkeypatch):
    kr = MagicMock()
    monkeypatch.setattr(credential_store, "_use_keyring", True)
    monkeypatch.setattr(credential_store, "keyring", kr)

    credential_store.save_credential("token", "v")

    kr.set_password.assert_called_once_with(credential_store._SERVICE_NAME, "token", "v")


def test_save_credential_empty_value_deletes(temp_db, monkeypatch):
    """43 行：空值等价于删除（keyring + SQLite 双清）。"""
    kr = MagicMock()
    monkeypatch.setattr(credential_store, "_use_keyring", True)
    monkeypatch.setattr(credential_store, "keyring", kr)
    credential_store._db_save("token", "old")

    credential_store.save_credential("token", "")

    kr.delete_password.assert_called_once_with(credential_store._SERVICE_NAME, "token")
    assert credential_store._db_load("token") == ""


def test_save_credential_falls_back_to_db_on_keyring_error(temp_db, monkeypatch):
    """39-41 行：keyring 写入失败时回退 SQLite。"""
    kr = MagicMock()
    kr.set_password.side_effect = RuntimeError("keyring broken")
    monkeypatch.setattr(credential_store, "_use_keyring", True)
    monkeypatch.setattr(credential_store, "keyring", kr)

    credential_store.save_credential("token", "v")

    assert credential_store._db_load("token") == "v"


def test_load_credential_returns_keyring_value(monkeypatch):
    kr = MagicMock()
    kr.get_password.return_value = "from-keyring"
    monkeypatch.setattr(credential_store, "_use_keyring", True)
    monkeypatch.setattr(credential_store, "keyring", kr)

    assert credential_store.load_credential("token") == "from-keyring"


def test_load_credential_falls_back_to_db_on_keyring_error(temp_db, monkeypatch):
    """50-52 行：keyring 读取抛异常时回退 SQLite。"""
    kr = MagicMock()
    kr.get_password.side_effect = RuntimeError("keyring broken")
    monkeypatch.setattr(credential_store, "_use_keyring", True)
    monkeypatch.setattr(credential_store, "keyring", kr)
    credential_store._db_save("token", "from-db")

    assert credential_store.load_credential("token") == "from-db"


def test_load_credential_falls_back_to_db_when_keyring_empty(temp_db, monkeypatch):
    """48-49 行：keyring 返回空时继续查 SQLite。"""
    kr = MagicMock()
    kr.get_password.return_value = None
    monkeypatch.setattr(credential_store, "_use_keyring", True)
    monkeypatch.setattr(credential_store, "keyring", kr)
    credential_store._db_save("token", "from-db")

    assert credential_store.load_credential("token") == "from-db"


def test_delete_credential_swallows_keyring_error(temp_db, monkeypatch):
    """57-58 行：keyring 删除失败不影响 SQLite 清理。"""
    kr = MagicMock()
    kr.delete_password.side_effect = RuntimeError("keyring broken")
    monkeypatch.setattr(credential_store, "_use_keyring", True)
    monkeypatch.setattr(credential_store, "keyring", kr)
    credential_store._db_save("token", "v")

    credential_store.delete_credential("token")

    assert credential_store._db_load("token") == ""


# ---- SQLite 回退分支（61-73 行：keyring 探测失败时重载模块） ----


def test_sqlite_fallback_branch_when_keyring_probe_fails(temp_db):
    """keyring 探测抛异常 → 重载后走 else 分支定义的 SQLite 函数。"""
    with patch.object(keyring, "get_password", side_effect=RuntimeError("probe failed")):
        module = importlib.reload(credential_store)
        assert module._use_keyring is False

        module.save_credential("token", "v")
        assert module.load_credential("token") == "v"

        module.save_credential("token", "")  # 空值 → 删除
        assert module.load_credential("token") == ""

        module.save_credential("token2", "x")
        module.delete_credential("token2")
        assert module.load_credential("token2") == ""

    # 恢复 keyring 分支（probe 用 mock 返回 None，避免真实 keyring 访问）
    with patch.object(keyring, "get_password", return_value=None):
        importlib.reload(credential_store)
    assert credential_store._use_keyring is True
