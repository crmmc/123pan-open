import re as _re
import sys
from pathlib import Path

# Windows 保留名
_WINDOWS_RESERVED = frozenset(
    "CON PRN AUX NUL COM1 COM2 COM3 COM4 COM5 COM6 COM7 COM8 COM9 "
    "LPT1 LPT2 LPT3 LPT4 LPT5 LPT6 LPT7 LPT8 LPT9".split()
)


def sanitize_filename(name: str) -> str:
    """过滤非法字符和保留名，防止路径注入。跨平台适配。"""
    if sys.platform == "win32":
        name = _re.sub(r'[<>:"|/\\?*\x00-\x1f]', '_', name)
        name = name.rstrip('. ')
        if not name:
            name = "_unnamed"
        stem = Path(name).stem.upper()
        if stem in _WINDOWS_RESERVED:
            name = "_" + name
    else:
        name = _re.sub(r'[/\x00]', '_', name)
        if not name:
            name = "_unnamed"
    _MAX_NAME_BYTES = 254  # 文件系统通常限制 255 字节，留 1 字节余量
    if len(name.encode("utf-8")) > _MAX_NAME_BYTES:
        ext = "".join(Path(name).suffixes)
        ext_bytes = len(ext.encode("utf-8"))
        stem = name[:name.rfind(ext)] if ext else name
        while len(stem.encode("utf-8")) + ext_bytes > _MAX_NAME_BYTES and stem:
            stem = stem[:-1]
        name = stem + ext
    return name
