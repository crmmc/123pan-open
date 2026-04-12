import pytest

from src.app.common.download_metadata import (
    DOWNLOAD_METADATA_VERSION,
    LEGACY_RESUME_TASK_ERROR,
    DownloadMetadataError,
    _candidate_directory_ids,
    _ensure_required_fields,
    _match_file_detail,
    _restore_pan_state,
    _snapshot_pan_state,
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


# ---- 2a. _ensure_required_fields ----


def test_ensure_required_fields_passes_complete_detail():
    _ensure_required_fields({
        "FileId": 1, "FileName": "a.txt", "Type": 0,
        "Size": 100, "Etag": "e", "S3KeyFlag": True,
    })


def test_ensure_required_fields_raises_on_missing_base():
    with pytest.raises(DownloadMetadataError, match="缺少必要字段"):
        _ensure_required_fields({"FileName": "a.txt", "Type": 0})


def test_ensure_required_fields_raises_on_missing_file_fields():
    with pytest.raises(DownloadMetadataError, match="缺少必要字段"):
        _ensure_required_fields({"FileId": 1, "FileName": "a.txt", "Type": 0})


def test_ensure_required_fields_skips_file_fields_for_folder():
    _ensure_required_fields({"FileId": 1, "FileName": "dir", "Type": 1})


# ---- 2b. _match_file_detail ----


def test_match_file_detail_returns_matching_item():
    items = [{"FileId": 10, "FileName": "a.txt", "Type": 0, "Size": 5, "Etag": "e", "S3KeyFlag": False}]
    result = _match_file_detail(items, 10)
    assert result is not None
    assert result["FileId"] == 10


def test_match_file_detail_returns_none_for_empty_list():
    assert _match_file_detail([], 1) is None


def test_match_file_detail_returns_none_when_no_match():
    items = [{"FileId": 10, "FileName": "a.txt", "Type": 0, "Size": 5, "Etag": "e", "S3KeyFlag": False}]
    assert _match_file_detail(items, 99) is None


def test_match_file_detail_validates_fields():
    with pytest.raises(DownloadMetadataError, match="缺少必要字段"):
        _match_file_detail([{"FileId": 10, "FileName": "a.txt", "Type": 0}], 10)


# ---- 2c. _candidate_directory_ids ----


def test_candidate_directory_ids_returns_unique():
    pan = _FakePan()
    pan.parent_file_id = 5
    result = _candidate_directory_ids(pan, 3)
    assert result == [3, 5, 0]


def test_candidate_directory_ids_deduplicates():
    pan = _FakePan()
    pan.parent_file_id = 5
    result = _candidate_directory_ids(pan, 5)
    assert result == [5, 0]


def test_candidate_directory_ids_skips_none_and_empty():
    pan = _FakePan()
    pan.parent_file_id = None
    result = _candidate_directory_ids(pan, None)
    assert result == [0]


# ---- 2d. _snapshot_pan_state / _restore_pan_state ----


def test_snapshot_restore_roundtrip():
    pan = _FakePan()
    state = _snapshot_pan_state(pan)
    assert state == {"file_page": 7, "all_file": True, "total": 99}

    pan.file_page = 100
    pan.all_file = False
    pan.total = 0

    _restore_pan_state(pan, state)
    assert pan.file_page == 7
    assert pan.all_file is True
    assert pan.total == 99
