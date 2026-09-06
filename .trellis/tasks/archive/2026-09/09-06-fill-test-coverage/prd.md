# 补齐测试用例：主路径100%覆盖，总体≥95%

## Goal

为 `src/app` 全部 25 个模块补齐 pytest 测试，使整体行覆盖率从基线 53% 提升到 ≥95%，且每个模块的主路径（核心业务方法的正常流 + 主错误流）达到 100% 覆盖。测试统一采用表测试、mock、依赖注入友好模式，不依赖真实网络与真实外部服务。用户价值：为后续重构与功能迭代建立安全网，缺陷在 CI 阶段暴露。

## 背景与已确认事实

- 123pan-open 是 PySide6 桌面端 123 云盘客户端；`src/app/common/`（业务层 13 模块）+ `src/app/view/`（界面层 12 模块），共 6617 语句。
- 基线（2026-09-06）：312 个测试全部通过；行覆盖率 53%（3113 行未覆盖）；mypy 0 错误；pylint 9.99/10。缺口集中在视图层大文件（file_interface.py 20%、move_window.py 14%、transfer_interface.py 51%）。分文件数据见 `research/coverage-baseline.md`。
- 存量测试已确立的风格（monkeypatch 模块接缝、`patch.object(Pan123, "__init__")` 跳网络、Qt 测试模块级 QApplication、shiboken patch 等）见 `research/test-conventions.md`，新测试必须沿用。
- 工具链事实：本机覆盖率命令必须用 `uv run python -m pytest`（`uv run pytest` 解析到 Homebrew pytest，加载不到 venv 的 pytest-cov）；CI（.github/workflows/test.yaml）= `script/mypy.sh` + `uv run pytest tests/ -v`，`QT_QPA_PLATFORM=offscreen`。
- `.trellis/spec/backend/` 各规范文件当前为空模板，本任务约定以 research/ 两份文档为准。

## Requirements

- **R1 覆盖率目标**：整体行覆盖率 ≥95%（`--cov=src/app` 口径）；每个模块主路径 100%，残留未覆盖行必须逐条加 `# pragma: no cover` 并在同行或相邻行注明理由（限：平台守卫、Qt 样板、进程退出兜底、防御性不可达分支），禁止为凑数滥用。
- **R2 测试模式**：分支密集的纯函数用 `@pytest.mark.parametrize` 表测试；外部依赖（requests 网络、sqlite、keyring、QApplication、QMessageBox/QFileDialog 等弹窗、剪贴板、时钟/sleep）一律 mock 或经接缝注入；禁止真实网络。
- **R3 依赖注入**：对难以 mock 的模块做最小侵入式可测试性重构——优先模块级接缝（现有惯例），必要时加可选构造参数（如 `Pan123(session=None)`）；不得改变行为与公开 API。
- **R4 测试基础设施**：新增 `tests/conftest.py`（session 级 qapp、临时数据库 fixture、mock response 工厂等共享 fixture）；pyproject 增加 `[tool.coverage.*]` 配置；Makefile 增加 test/coverage/lint/mypy 统一入口。
- **R5 兼容性**：现有 312 个测试保持全部通过、不修改其断言语义；mypy 与 pylint 基线不得劣化。

## Acceptance Criteria

- [ ] A1 `uv run python -m pytest tests/ -q --cov=src/app --cov-report=term-missing`：0 failed，TOTAL 覆盖率 ≥95%。
- [ ] A2 各模块残留未覆盖行为 0，或全部带 `# pragma: no cover` 且有理由注释；主路径无裸露未覆盖行。
- [ ] A3 `bash script/mypy.sh` 通过；`bash script/lint.sh` 评分不低于 9.99 基线。
- [ ] A4 测试套件无真实网络/socket 访问（全部经 mock/注入）。
- [ ] A5 现有 312 个测试未修改语义且全部通过。

## Out of Scope

- 不改动任何业务行为、不新增功能、不做 UI 改版。
- 不追求纯 UI 样板（布局构造、样式表字符串、Qt 绘制）的覆盖，按 R1 规则 pragma 标注。
- 不引入 pytest-qt 等新测试框架依赖（沿用现有 QApplication 模式）。
- 不做 E2E/真机联调、不测真实 123 云盘服务端。

## Open Questions

（无。覆盖率口径=行覆盖、pragma 允许范围、允许最小 DI 重构三项已在 R1/R3 定义，待最终规划摘要一并确认。）
