import re as _re
import sys
from pathlib import Path

# Windows 保留名
_WINDOWS_RESERVED = frozenset(
    "CON PRN AUX NUL COM1 COM2 COM3 COM4 COM5 COM6 COM7 COM8 COM9 "
    "LPT1 LPT2 LPT3 LPT4 LPT5 LPT6 LPT7 LPT8 LPT9".split()
)

_MAX_NAME_BYTES = 254  # 文件系统通常限制 255 字节，留 1 字节余量

# P2-22: 零宽字符、RTL/LTR 标记、C1 控制字符
_INVISIBLE_CHARS_RE = _re.compile(
    '[\u200b-\u200d\ufeff'       # 零宽字符
    '\u202a-\u202e\u2066-\u2069'  # RTL/LTR 标记
    '\u0080-\u009f]'              # C1 控制字符
)


def _trim_utf8_name(name: str, max_bytes: int) -> str:
    ext = "".join(Path(name).suffixes)
    stem = name[:name.rfind(ext)] if ext else name
    while len((stem + ext).encode("utf-8")) > max_bytes:
        if stem and ext:
            ext = ext[:-1]
            continue
        if stem:
            stem = stem[:-1]
            continue
        if ext:
            ext = ext[:-1]
            continue
        break
    trimmed = stem + ext
    return trimmed or "_unnamed"


def sanitize_filename(name: str) -> str:
    """过滤非法字符和保留名，防止路径注入。跨平台适配。"""
    # P2-22: 先移除零宽字符、RTL/LTR 标记、C1 控制字符
    name = _INVISIBLE_CHARS_RE.sub('', name)
    if sys.platform == "win32":
        name = _re.sub(r'[<>:"|/\\?*\x00-\x1f]', '_', name)
        name = name.rstrip('. ')
        stem = Path(name).stem.upper() if name else ""
        if stem in _WINDOWS_RESERVED and name:
            name = "_" + name
    else:
        name = _re.sub(r'[/\x00]', '_', name)
    if name in {"", ".", ".."}:
        name = "_unnamed"
    if len(name.encode("utf-8")) > _MAX_NAME_BYTES:
        name = _trim_utf8_name(name, _MAX_NAME_BYTES)
    return name
