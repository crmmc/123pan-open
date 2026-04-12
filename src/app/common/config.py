import os
import platform
import sys
from pathlib import Path


def isWin11():
    if sys.platform != "win32":
        return False
    try:
        return sys.getwindowsversion().build >= 22000
    except AttributeError:
        return False


# 配置文件路径
if platform.system() == "Windows":
    CONFIG_DIR = (
        Path(os.environ.get("APPDATA", "") or (Path.home() / "AppData" / "Roaming"))
        / "123pan-open"
    )
elif platform.system() == "Darwin":
    CONFIG_DIR = Path.home() / "Library" / "Application Support" / "123pan-open"
else:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    CONFIG_DIR = Path(xdg) / "123pan-open" if xdg else Path.home() / ".config" / "123pan-open"
