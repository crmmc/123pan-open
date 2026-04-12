"""使用系统 keyring 安全存储敏感凭证（密码、Token）。"""

import logging

logger = logging.getLogger(__name__)

_SERVICE_NAME = "123pan-open"

# keyring 可能在某些 Linux 环境不可用，降级为空实现
try:
    import keyring  # noqa: E402

    def save_credential(key: str, value: str) -> None:
        if value:
            try:
                keyring.set_password(_SERVICE_NAME, key, value)
            except Exception:
                logger.warning("keyring 写入 %s 失败，凭证将不会持久化", key)
        else:
            delete_credential(key)

    def load_credential(key: str) -> str:
        try:
            return keyring.get_password(_SERVICE_NAME, key) or ""
        except Exception:
            logger.warning("keyring 读取 %s 失败", key)
            return ""

    def delete_credential(key: str) -> None:
        try:
            keyring.delete_password(_SERVICE_NAME, key)
        except Exception:
            pass

except ImportError:

    def save_credential(key: str, value: str) -> None:
        pass

    def load_credential(key: str) -> str:
        return ""

    def delete_credential(key: str) -> None:
        pass
