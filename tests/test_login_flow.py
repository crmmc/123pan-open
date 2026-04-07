from unittest.mock import MagicMock, patch

from src.app.common import database as database_module
from src.app.common.database import Database
from src.app.view.login_window import (
    has_saved_credentials,
    try_token_probe,
)


def _use_temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "123pan.db"
    monkeypatch.setattr(database_module, "_get_db_path", lambda: db_path)
    Database.reset()
    return Database.instance()


class TestHasSavedCredentials:
    def test_returns_true_when_both_present(self, tmp_path, monkeypatch):
        db = _use_temp_db(tmp_path, monkeypatch)
        db.set_many_config({"userName": "alice", "passWord": "secret"})
        assert has_saved_credentials(db) is True

    def test_returns_false_when_no_password(self, tmp_path, monkeypatch):
        db = _use_temp_db(tmp_path, monkeypatch)
        db.set_many_config({"userName": "alice", "passWord": ""})
        assert has_saved_credentials(db) is False

    def test_returns_false_when_no_username(self, tmp_path, monkeypatch):
        db = _use_temp_db(tmp_path, monkeypatch)
        db.set_many_config({"userName": "", "passWord": "secret"})
        assert has_saved_credentials(db) is False


class TestTryTokenProbe:
    def test_returns_none_when_no_token(self, tmp_path, monkeypatch):
        db = _use_temp_db(tmp_path, monkeypatch)
        db.set_config("authorization", "")
        assert try_token_probe(db) is None

    def test_returns_pan_when_token_valid(self, tmp_path, monkeypatch):
        db = _use_temp_db(tmp_path, monkeypatch)
        db.set_config("authorization", "valid-token")
        mock_pan = MagicMock()
        mock_pan.user_info.return_value = {"user": "alice"}
        with patch("src.app.view.login_window.Pan123", return_value=mock_pan):
            result = try_token_probe(db)
        assert result is mock_pan

    def test_clears_token_when_invalid(self, tmp_path, monkeypatch):
        db = _use_temp_db(tmp_path, monkeypatch)
        db.set_config("authorization", "expired-token")
        mock_pan = MagicMock()
        mock_pan.user_info.return_value = None
        with patch("src.app.view.login_window.Pan123", return_value=mock_pan):
            result = try_token_probe(db)
        assert result is None
        assert db.get_config("authorization", "") == ""

    def test_clears_token_on_exception(self, tmp_path, monkeypatch):
        db = _use_temp_db(tmp_path, monkeypatch)
        db.set_config("authorization", "bad-token")
        with patch("src.app.view.login_window.Pan123", side_effect=Exception("connection error")):
            result = try_token_probe(db)
        assert result is None
        assert db.get_config("authorization", "") == ""
