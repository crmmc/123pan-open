# 技术设计：补齐测试用例至主路径100% / 总体≥95%

## 覆盖率口径与配置

- 口径：**行覆盖**（line coverage），`coverage` 默认模式，不开 branch。理由：存量 312 测试与 CI 均按行覆盖口径运行；"主路径 100%" 落到可执行验收 = 模块残留未覆盖行仅剩带理由的 pragma。
- pyproject 新增：

```toml
[tool.coverage.run]
source = ["src/app"]

[tool.coverage.report]
show_missing = true
precision = 1
exclude_lines = [
  "pragma: no cover",
  "if TYPE_CHECKING:",
  "raise NotImplementedError",
]
```

- 验证命令（唯一权威入口，本地不用 `uv run pytest`）：

```bash
uv run python -m pytest tests/ -q --cov=src/app --cov-report=term-missing
```

## 测试基础设施（批次 0 产出）

1. `tests/conftest.py`（新增；不回改存量测试文件）：
   - `qapp`：session 级 `QApplication` fixture（`QApplication.instance() or QApplication([])`），供新 Qt 测试注入使用；存量文件模块级写法与之兼容（instance() 复用同一实例）。
   - `temp_db`：`monkeypatch.setattr(database_module, "_get_db_path", lambda: tmp_path/"123pan-open.db")` + `Database.reset()`，返回单例（提炼自 tests/test_config.py `_use_temp_db`，属共享化而非行为变更）。
   - `fake_response`：`_mock_response` 工厂共享版（提炼自 tests/test_pan_api.py）。
2. Makefile 新目标：`test`（全量 pytest）、`coverage`（pytest --cov + term-missing）、`lint`、`mypy`（转调 script/*.sh）。

## 依赖注入与可测试性重构原则

优先级从高到低，重构量最小化：

1. **monkeypatch 模块级接缝**（现有惯例，零重构）：`database._get_db_path`、`file_interface.QFileDialog`、`api.requests` 等。
2. **替换实例属性**：构造后覆盖 `pan.session` 为 MagicMock（现有测试已用）。
3. **可选构造参数注入**（仅在 1/2 不可行时）：`Pan123.__init__(..., session: requests.Session | None = None)`，None 时保持原行为 `requests.Session()`；默认值保证所有现有调用点不变。
4. **时钟/sleep 注入**：download_resume.py 若存在轮询 sleep，优先 monkeypatch `time.sleep`/`time.monotonic` 模块引用，不改签名。

红线：不改行为、不改公开 API、不动 `__slots__`/序列化格式；每批次结束全量 pytest + mypy + pylint 必须绿。重构与新增测试同批次提交，出问题按批次整体回滚。

## 批次划分与串行派发

原则：一次只派发一个 trellis-implement 子代理（用户 LLM API 并发低，禁止并行）；小→大排序，让基础设施在处理大文件前先稳定；每批次内聚（同层/同域文件）。

| 批次 | 范围 | 存量 miss | 新增/扩展测试文件 |
|---|---|---:|---|
| 0 | 基础设施：conftest.py、coverage 配置、Makefile | — | — |
| 1 | common 小模块组：config、const、credential_store、filename_utils、log、resource、speed_tracker、concurrency、database、download_metadata | 99 | test_config.py 等 10 文件扩展 |
| 2 | common/api.py | 310 | test_pan_api.py 扩展 |
| 3 | common/download_resume.py | 190 | test_download_resume.py 扩展 |
| 4 | view/file_interface.py（千行大文件） | 1106 | test_file_interface.py 扩展 |
| 5 | view/transfer_interface.py（千行大文件） | 625 | test_transfer_interface.py 扩展 |
| 6 | view 小窗口组：cloud_interface、move_window、newfolder_window、rename_window、upload_conflict_dialog | 265 | 5 个新测试文件 |
| 7 | view 登录组：login_window、qr_login_page、main_window | 330 | 3 文件扩展 |
| 8 | 收尾：search_window、setting_interface | 188 | 2 文件扩展 |

合计未覆盖 3113 行；批次目标为各自文件残留 ≤2% 或仅剩带理由 pragma，为总体 ≥95% 留出余量。

## 子代理工作循环（每批次）

1. 读注入上下文（implement.jsonl 清单 + prd/design/implement + research/）。
2. 为本批次文件写/扩展测试：表测试优先、mock 外部依赖、沿用 research/test-conventions.md 惯例。
3. 自验：`uv run python -m pytest tests/<本批次文件> -q` 绿 → 触及模块覆盖率 ≥98%（纯 UI 样板 pragma 除外）→ `bash script/mypy.sh` + `bash script/lint.sh` 绿。
4. 汇报：新增测试数、触及文件覆盖率、pragma 清单（文件:行 + 理由）、发现的重构点（若有）。

主会话在每批次后复跑覆盖率核对，不达标则就同一文件追加派发一轮（大文件允许 2 轮）。

## 风险与对策

- **千行大文件单轮不达标**（file_interface 1106 miss / transfer_interface 625 miss）：批次允许追加第二轮派发；implement.md 中把这两个文件标注为高风险点。
- **Qt headless 行为差异**：conftest 强制 `os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")`；弹窗一律 patch。
- **DI 重构引入回归**：默认参数向后兼容 + 每批次全量测试 + mypy/pylint 门禁；必要时按批次回滚。
- **pragma 滥用冲指标**：trellis-check 阶段逐条审查 pragma 理由，砍不掉的行必须真实不可测。
- **`uv run pytest` 入口混淆**：所有文档/Makefile/汇报口径统一 `uv run python -m pytest`。

## 回滚

工作区按批次推进但不强制逐批提交（Phase 3.4 统一批量提交）。任一批次失败：`git checkout -- <该批次文件>` 回滚后重派发；conftest/配置属低风险，最后统一提交。
