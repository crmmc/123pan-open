"""const.py 的版本探测逻辑（17/23/24-25 行）。"""
from unittest.mock import patch

from src.app.common import const as const_module


def test_detect_version_returns_injected_build_commit(monkeypatch):
    """CI 注入构建号时直接返回，不再调用 git。"""
    monkeypatch.setattr(const_module, "_BUILD_COMMIT", "abc1234")

    assert const_module._detect_version() == "abc1234"


def test_detect_version_falls_back_to_dev_when_git_fails(monkeypatch):
    """git 命令失败（24-25 行）回退 'dev'。"""
    monkeypatch.setattr(const_module, "_BUILD_COMMIT", "dev")

    with patch("subprocess.check_output", side_effect=OSError("git not found")):
        assert const_module._detect_version() == "dev"


def test_detect_version_empty_git_output_falls_back_to_dev(monkeypatch):
    """git 输出为空时（23 行 or 兜底）回退 'dev'。"""
    monkeypatch.setattr(const_module, "_BUILD_COMMIT", "dev")

    with patch("subprocess.check_output", return_value=b""):
        assert const_module._detect_version() == "dev"
