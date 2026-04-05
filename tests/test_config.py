import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.app.common import config as config_module
from src.app.common.config import ConfigManager


@pytest.fixture
def tmp_config_dir(tmp_path):
    """将配置目录重定向到临时目录"""
    config_file = tmp_path / "config.json"
    with patch.object(config_module, "CONFIG_DIR", tmp_path), \
         patch.object(config_module, "CONFIG_FILE", config_file):
        yield tmp_path


class TestLoadConfig:
    def test_default_config_when_no_file(self, tmp_config_dir):
        config = ConfigManager.load_config()
        assert config["userName"] == ""
        assert config["settings"]["defaultDownloadPath"] != ""
        assert config["settings"]["retryMaxAttempts"] == 3
        assert config["settings"]["retryBackoffFactor"] == 0.5

    def test_load_existing_config(self, tmp_config_dir):
        saved = {"userName": "testuser", "passWord": "123", "authorization": "Bearer xxx",
                 "deviceType": "", "osVersion": "", "loginuuid": "", "settings": {"defaultDownloadPath": "/tmp"}}
        config_module.CONFIG_FILE.write_text(json.dumps(saved), encoding="utf-8")
        config = ConfigManager.load_config()
        assert config["userName"] == "testuser"
        assert config["authorization"] == "Bearer xxx"

    def test_missing_settings_key_gets_defaults(self, tmp_config_dir):
        saved = {"userName": "u", "passWord": "p", "authorization": "",
                 "deviceType": "", "osVersion": "", "loginuuid": ""}
        config_module.CONFIG_FILE.write_text(json.dumps(saved), encoding="utf-8")
        config = ConfigManager.load_config()
        assert "settings" in config
        assert config["settings"]["retryMaxAttempts"] == 3

    def test_missing_top_level_keys_get_defaults(self, tmp_config_dir):
        saved = {"settings": {"defaultDownloadPath": "/tmp"}}
        config_module.CONFIG_FILE.write_text(json.dumps(saved), encoding="utf-8")
        config = ConfigManager.load_config()
        assert config["userName"] == ""
        assert config["passWord"] == ""

    def test_corrupted_file_returns_default(self, tmp_config_dir):
        config_module.CONFIG_FILE.write_text("not valid json{{{", encoding="utf-8")
        config = ConfigManager.load_config()
        assert config["userName"] == ""
        assert "settings" in config

    def test_empty_file_returns_default(self, tmp_config_dir):
        config_module.CONFIG_FILE.write_text("", encoding="utf-8")
        config = ConfigManager.load_config()
        assert config["userName"] == ""


class TestSaveConfig:
    def test_save_and_reload(self, tmp_config_dir):
        config = {"userName": "saved_user", "settings": {"retryMaxAttempts": 5}}
        assert ConfigManager.save_config(config) is True
        reloaded = ConfigManager.load_config()
        assert reloaded["userName"] == "saved_user"
        assert reloaded["settings"]["retryMaxAttempts"] == 5

    def test_save_creates_directory(self, tmp_path):
        new_dir = tmp_path / "sub" / "dir"
        with patch("src.app.common.config.CONFIG_DIR", new_dir), \
             patch("src.app.common.config.CONFIG_FILE", new_dir / "config.json"):
            assert ConfigManager.save_config({"userName": "x"}) is True
            assert (new_dir / "config.json").exists()


class TestGetSetting:
    def test_get_existing_setting(self, tmp_config_dir):
        ConfigManager.save_config({"settings": {"retryMaxAttempts": 7}})
        assert ConfigManager.get_setting("retryMaxAttempts") == 7

    def test_get_missing_setting_returns_default(self, tmp_config_dir):
        assert ConfigManager.get_setting("nonexistent", "fallback") == "fallback"

    def test_get_setting_no_settings_key(self, tmp_config_dir):
        ConfigManager.save_config({"userName": "u"})
        assert ConfigManager.get_setting("anything", 42) == 42
