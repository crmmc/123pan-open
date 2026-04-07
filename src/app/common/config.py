import os
import platform
import sys
from pathlib import Path


def isWin11():
    return sys.platform == "win32" and sys.getwindowsversion().build >= 22000


# 配置文件路径
if platform.system() == "Windows":
    CONFIG_DIR = Path(os.environ.get("APPDATA", "") or (Path.home() / "AppData" / "Roaming")) / "Qxyz17" / "123pan"
elif platform.system() == "Darwin":
    CONFIG_DIR = Path.home() / "Library" / "Application Support" / "Qxyz17" / "123pan"
else:
    CONFIG_DIR = Path.home() / ".config" / "Qxyz17" / "123pan"
