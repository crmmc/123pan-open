import pytest

from src.app.common import filename_utils


def test_sanitize_filename_rejects_dot_names(monkeypatch):
    monkeypatch.setattr(filename_utils.sys, "platform", "darwin")

    assert filename_utils.sanitize_filename("") == "_unnamed"
    assert filename_utils.sanitize_filename(".") == "_unnamed"
    assert filename_utils.sanitize_filename("..") == "_unnamed"


def test_sanitize_filename_trims_overlong_extension_when_needed(monkeypatch):
    monkeypatch.setattr(filename_utils.sys, "platform", "darwin")
    name = "a." + ("后" * 200)

    sanitized = filename_utils.sanitize_filename(name)

    assert len(sanitized.encode("utf-8")) <= 254
    assert sanitized.startswith("a.")


def test_sanitize_filename_trims_long_name_without_extension(monkeypatch):
    """无扩展名超长名：逐字裁剪 stem 分支（28-30 行）。"""
    monkeypatch.setattr(filename_utils.sys, "platform", "darwin")

    sanitized = filename_utils.sanitize_filename("后" * 200)

    assert len(sanitized.encode("utf-8")) <= 254
    assert sanitized


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("CON", "_CON"),
        ("con", "_con"),
        ("NUL.txt", "_NUL.txt"),
        ("com1", "_com1"),
        ("lpt9.log", "_lpt9.log"),
        ("aux.gz", "_aux.gz"),
        ("normal.txt", "normal.txt"),
        ("notcon", "notcon"),
    ],
)
def test_sanitize_filename_prefixes_windows_reserved_names(monkeypatch, name, expected):
    """Windows 保留名（含大小写与带扩展名变体）加下划线前缀（48 行）。"""
    monkeypatch.setattr(filename_utils.sys, "platform", "win32")

    assert filename_utils.sanitize_filename(name) == expected
