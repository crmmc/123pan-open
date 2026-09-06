# 现有测试约定（从 312 个存量测试中提炼）

> 供 trellis-implement / trellis-check 子代理使用。新测试必须与这些约定保持一致。

## 运行方式

- 测试从仓库根目录运行，直接导入 `src.app.*`（无安装包、无 src 布局 hack）。
- **覆盖率命令必须用 `uv run python -m pytest`**：本机 `uv run pytest` 会解析到 Homebrew 的 pytest（Python 3.14），加载不到 venv 里的 pytest-cov（报 `unrecognized arguments: --cov`）。CI 中 `uv run pytest` 正常，但本地一律用 `python -m pytest`。
- headless：CI 设 `QT_QPA_PLATFORM=offscreen`；本地 macOS 默认可跑。

```bash
uv run python -m pytest tests/ -q                                   # 全量
uv run python -m pytest tests/ -q --cov=src/app --cov-report=term-missing
bash script/mypy.sh        # uv run mypy .
bash script/lint.sh        # uv run pylint src tests
```

## 风格约定（照抄现有测试）

1. **mock**：`unittest.mock.patch` / `MagicMock` + pytest `monkeypatch` / `tmp_path`，二者混用皆可，以就近文件风格为准。
2. **模块级接缝注入**：优先 monkeypatch 模块级函数/对象，而不是深挖私有属性。典型：`monkeypatch.setattr(database_module, "_get_db_path", lambda: db_path)` + `Database.reset()`（见 tests/test_config.py 的 `_use_temp_db`）。
3. **跳过网络构造**：`patch.object(Pan123, "__init__", lambda self, **kw: None)` 后手动补属性（见 tests/test_pan_api.py 的 `pan` fixture）。
4. **mock response 工厂**：`_mock_response(status_code, json_data, headers)` 返回配好 `status_code/.json()/.headers` 的 MagicMock（tests/test_pan_api.py）。
5. **Qt 视图测试**：
   - 文件顶部 `app = QApplication.instance() or QApplication([])`（模块级）。
   - 无法正常构造的 widget 用 `object.__new__(Cls)` 或 MagicMock 代替 self 调用私有方法（`FileInterface._FileInterface__uploadFolder(fi)`）。
   - `shiboken6.isValid` 会误判 → autouse fixture patch 成 `lambda _obj: True`（tests/test_file_interface.py）。
   - Qt 弹窗/对话框 patch 到其模块路径：`@patch("src.app.view.file_interface.QFileDialog.getExistingDirectory")`。
6. **表测试**：分支密集的纯函数用 `@pytest.mark.parametrize`（目标模式，存量中已有零散使用）。
7. **类分组**：同一主题的测试用 `class TestXxx:` 分组，方法不加 self 断言外的东西；docstring 可用中文。
8. **命名**：`test_<subject>_<behavior>`，如 `test_upload_progress_db_updates_are_throttled`。
9. **测试私有函数**：可直接导入下划线函数（如 `_safe_float`、`_parse_json_response`），项目惯例允许。

## 硬约束

- 禁止真实网络：requests 全部 mock（session、response 均不落网）。
- 禁止改动现有 312 个测试的行为；新 fixture 放 `tests/conftest.py` 供新测试使用，不回改旧文件。
- mypy（41 文件 0 错误）与 pylint（9.99/10）必须保持绿；pyproject 已有 `[tool.mypy]` overrides，tests 模块豁免 `attr-defined`。
- `tests/__init__.py` 存在，测试目录是包；新增文件同名前缀 `test_`。
