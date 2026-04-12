import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import CONFIG_DIR

# 配置文件路径
LOG_FILE = CONFIG_DIR / "123pan-open.log"


def get_logger(name: str = "123pan-open"):
    try:
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)

        # 防止重复添加 handler
        if not logger.handlers:
            formatter = logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            )

            if not CONFIG_DIR.exists():
                CONFIG_DIR.mkdir(parents=True, exist_ok=True)

            file_handler = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=5, encoding="utf-8")
            file_handler.setFormatter(formatter)

            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)

            logger.addHandler(file_handler)
            logger.addHandler(console_handler)

        return logger
    except Exception:
        return logging.getLogger(name)


def set_log_level(level_name: str) -> None:
    """动态修改日志级别（同时更新所有 handler）。"""
    level = getattr(logging, level_name.upper(), logging.INFO)
    logger = logging.getLogger("123pan-open")
    logger.setLevel(level)
    for handler in logger.handlers:
        handler.setLevel(level)
