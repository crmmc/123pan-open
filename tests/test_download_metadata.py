import pytest

from src.app.common.download_metadata import (
    DOWNLOAD_METADATA_VERSION,
    LEGACY_RESUME_TASK_ERROR,
    DownloadMetadataError,
    is_resume_metadata_compatible,
    resolve_download_file_detail,
)


class _FakePan:
    def __init__(self, items=None, directory_items=None):
        self.list = items or []
        self.directory_items = directory_items or {}
        self.file_page = 7
        self.all_file = True
        self.total = 99
        self.parent_file_id = 9
        self.calls = []

    def get_dir_by_id(self, file_id, save=True, all=False, limit=100):
        self.calls.append((file_id, save, all, limit))
        return 0, self.directory_items.get(file_id, [])


def test_resolve_download_file_detail_prefers_current_pan_list():
    file_detail = {
        "FileId": 101,
        "FileName": "demo.bin",
        "Type": 0,
        "Size": 2048,
        "Etag": "etag-1",
        "S3KeyFlag": True,
    }
    pan = _FakePan(items=[file_detail])

    result = resolve_download_file_detail(pan, 101, current_dir_id=3)

    assert result == file_detail
    assert pan.calls == []


def test_resolve_download_file_detail_restores_pan_state_after_lookup():
    file_detail = {
        "FileId": 202,
        "FileName": "video.mp4",
        "Type": 0,
        "Size": 4096,
        "Etag": "etag-2",
        "S3KeyFlag": False,
    }
    pan = _FakePan(directory_items={5: [file_detail]})

    result = resolve_download_file_detail(pan, 202, current_dir_id=5)

    assert result == file_detail
    assert pan.calls == [(5, False, True, 1000)]
    assert pan.file_page == 7
    assert pan.all_file is True
    assert pan.total == 99


def test_resolve_download_file_detail_raises_when_matched_metadata_is_incomplete():
    pan = _FakePan(
        directory_items={
            8: [
                {
                    "FileId": 303,
                    "FileName": "broken.bin",
                    "Type": 0,
                }
            ]
        }
    )

    with pytest.raises(DownloadMetadataError, match="缺少必要字段"):
        resolve_download_file_detail(pan, 303, current_dir_id=8)


def test_resume_metadata_requires_current_version():
    assert is_resume_metadata_compatible(
        {"metadata_version": DOWNLOAD_METADATA_VERSION}
    )
    assert not is_resume_metadata_compatible({})
    assert LEGACY_RESUME_TASK_ERROR
