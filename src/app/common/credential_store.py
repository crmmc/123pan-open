"""凭证存储：优先使用系统 keyring，不可用时回退到 SQLite 明文存储。"""

import logging

logger = logging.getLogger(__name__)

_SERVICE_NAME = "123pan-open"
_use_keyring = False

try:
    import keyring  # noqa: E402
    # 验证 keyring 后端是否真正可用（某些 Linux 环境导入成功但后端不可用）
    keyring.get_password(_SERVICE_NAME, "__probe__")
    _use_keyring = True
except Exception:
    _use_keyring = False
    logger.info("keyring 不可用，凭证将存储在 SQLite 中")


def _db_save(key: str, value: str) -> None:
    from .database import Database
    Database.instance().set_config(f"_cred_{key}", value)


def _db_load(key: str) -> str:
    from .database import Database
    return Database.instance().get_config(f"_cred_{key}", "") or ""


def _db_delete(key: str) -> None:
    from .database import Database
    Database.instance().set_config(f"_cred_{key}", "")


def save_credential(key: str, value: str) -> None:
    if not value:
        delete_credential(key)
        return
    if _use_keyring:
        try:
            keyring.set_password(_SERVICE_NAME, key, value)
            return
        except Exception:
            logger.warning("keyring 写入 %s 失败，回退到 SQLite", key)
    _db_save(key, value)


def load_credential(key: str) -> str:
    if _use_keyring:
        try:
            val = keyring.get_password(_SERVICE_NAME, key) or ""
            if val:
                return val
        except Exception:
            logger.warning("keyring 读取 %s 失败，回退到 SQLite", key)
    return _db_load(key)


def delete_credential(key: str) -> None:
    if _use_keyring:
        try:
            keyring.delete_password(_SERVICE_NAME, key)
        except Exception:
            pass
    _db_delete(key)
