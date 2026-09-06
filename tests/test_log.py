"""log 模块测试：CONFIG_DIR/LOG_FILE 全部指向 tmp_path，不写真实日志文件。"""
import logging

import pytest

from src.app.common import log as log_module


@pytest.fixture
def logger_factory(tmp_path, monkeypatch):
    """把 CONFIG_DIR/LOG_FILE 指向 tmp_path，返回创建独立命名 logger 的工厂。

    teardown 统一移除并关闭测试创建的 handler，避免污染 logging 注册表。
    """
    log_dir = tmp_path / "cfg"
    monkeypatch.setattr(log_module, "CONFIG_DIR", log_dir)
    monkeypatch.setattr(log_module, "LOG_FILE", log_dir / "123pan-open.log")
    created = []

    def _make(name):
        logger = log_module.get_logger(name)
        created.append(logger)
        return logger

    yield _make

    for logger in created:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
        logger.setLevel(logging.INFO)


def test_get_logger_creates_config_dir_and_handlers(logger_factory):
    """23 行：CONFIG_DIR 不存在时自动创建，并挂载 file + console 两个 handler。"""
    logger = logger_factory("ut-log-basic")

    assert log_module.CONFIG_DIR.exists()
    handler_types = {type(handler).__name__ for handler in logger.handlers}
    assert handler_types == {"RotatingFileHandler", "StreamHandler"}


def test_get_logger_does_not_duplicate_handlers(logger_factory):
    first = logger_factory("ut-log-reuse")

    again = log_module.get_logger("ut-log-reuse")

    assert again.handlers == first.handlers
    assert len(again.handlers) == 2


def test_get_logger_falls_back_when_file_handler_fails(logger_factory, tmp_path, monkeypatch):
    """35-36 行：RotatingFileHandler 创建失败（路径是目录）时返回裸 logger。"""
    monkeypatch.setattr(log_module, "LOG_FILE", tmp_path)  # 目录路径 → IsADirectoryError

    logger = logger_factory("ut-log-fallback")

    assert logger.handlers == []


def test_set_log_level_updates_logger_and_handlers(logger_factory):
    """45 行：set_log_level 同步更新所有 handler 的级别。"""
    logger = logger_factory("123pan-open")  # set_log_level 固定操作默认名 logger
    assert logger.handlers

    log_module.set_log_level("DEBUG")

    assert logger.level == logging.DEBUG
    assert all(handler.level == logging.DEBUG for handler in logger.handlers)


def test_set_log_level_invalid_name_falls_back_to_info(logger_factory):
    """41 行：非法级别名回退 INFO。"""
    logger = logger_factory("123pan-open")

    log_module.set_log_level("NotALevel")

    assert logger.level == logging.INFO
