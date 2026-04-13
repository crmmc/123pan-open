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
