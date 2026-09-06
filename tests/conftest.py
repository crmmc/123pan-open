"""共享 pytest fixtures，供新增测试使用。

约定（见 .trellis/tasks/09-06-fill-test-coverage/research/test-conventions.md）：
- 不回改存量测试文件；存量文件均为模块级自建 QApplication，与本文件兼容。
- 在导入 PySide6 之前设置 offscreen 平台兜底（CI 已显式设置时 setdefault 为 no-op）。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from typing import cast
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QApplication

from src.app.common import database as database_module
from src.app.common.database import Database


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    """Session 级 QApplication；已存在实例时直接复用（与存量模块级写法兼容）。"""
    # PySide6 stubs 中 instance() 返回 Optional[QCoreApplication]，实际必为 QApplication
    return cast(QApplication, QApplication.instance() or QApplication([]))


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """函数级临时数据库：_get_db_path 指向 tmp_path 后重建单例。

    提炼自存量 _use_temp_db 辅助函数（tests/test_config.py），行为一致；
    额外在 teardown 时 reset，避免临时库连接泄漏到后续测试。
    """
    db_path = tmp_path / "123pan-open.db"
    monkeypatch.setattr(database_module, "_get_db_path", lambda: db_path)
    Database.reset()
    yield Database.instance()
    Database.reset()


@pytest.fixture
def fake_response():
    """mock response 工厂：fake_response(status_code, json_data, headers)。

    提炼自存量 _mock_response（tests/test_pan_api.py），返回配好
    status_code / .json() / .headers 的 MagicMock。
    """

    def _factory(status_code=200, json_data=None, headers=None):
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = json_data or {"code": 0, "message": "success"}
        resp.headers = headers or {}
        return resp

    return _factory
