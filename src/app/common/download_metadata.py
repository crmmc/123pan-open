DOWNLOAD_METADATA_VERSION = 2
LEGACY_RESUME_TASK_ERROR = "下载任务元数据已失效，请删除后重新创建下载任务"
MISSING_DOWNLOAD_METADATA_ERROR = "无法获取文件的原始元数据，请删除后重新创建下载任务"

_BASE_REQUIRED_FIELDS = ("FileId", "FileName", "Type")
_FILE_REQUIRED_FIELDS = ("Size", "Etag", "S3KeyFlag")


class DownloadMetadataError(RuntimeError):
    """下载请求元数据异常。"""


def is_resume_metadata_compatible(metadata):
    return metadata.get("metadata_version") == DOWNLOAD_METADATA_VERSION


def _ensure_required_fields(file_detail):
    missing_fields = [
        key for key in _BASE_REQUIRED_FIELDS if key not in file_detail
    ]
    if int(file_detail.get("Type", 0) or 0) != 1:
        missing_fields.extend(
            key for key in _FILE_REQUIRED_FIELDS if key not in file_detail
        )
    if missing_fields:
        missing = ", ".join(sorted(set(missing_fields)))
        raise DownloadMetadataError(f"文件元数据缺少必要字段: {missing}")


def _match_file_detail(items, file_id):
    target_id = str(file_id)
    for item in items or []:
        if str(item.get("FileId")) != target_id:
            continue
        _ensure_required_fields(item)
        return dict(item)
    return None


def _snapshot_pan_state(pan):
    return {
        "file_page": getattr(pan, "file_page", None),
        "all_file": getattr(pan, "all_file", None),
        "total": getattr(pan, "total", None),
    }


def _restore_pan_state(pan, state):
    for key, value in state.items():
        if value is not None:
            setattr(pan, key, value)


def _load_directory_items(pan, directory_id):
    state = _snapshot_pan_state(pan)
    try:
        code, items = pan.get_dir_by_id(
            directory_id,
            save=False,
            all=True,
            limit=1000,
        )
    finally:
        _restore_pan_state(pan, state)
    if code != 0:
        raise DownloadMetadataError(
            f"获取目录 {directory_id} 的文件元数据失败，返回码: {code}"
        )
    return items


def _candidate_directory_ids(pan, current_dir_id):
    directory_ids = []
    for value in (current_dir_id, getattr(pan, "parent_file_id", None), 0):
        if value in (None, "") or value in directory_ids:
            continue
        directory_ids.append(value)
    return directory_ids


def resolve_download_file_detail(pan, file_id, current_dir_id=0):
    file_detail = _match_file_detail(getattr(pan, "list", []), file_id)
    if file_detail:
        return file_detail

    for directory_id in _candidate_directory_ids(pan, current_dir_id):
        items = _load_directory_items(pan, directory_id)
        file_detail = _match_file_detail(items, file_id)
        if file_detail:
            return file_detail

    raise DownloadMetadataError(MISSING_DOWNLOAD_METADATA_ERROR)
