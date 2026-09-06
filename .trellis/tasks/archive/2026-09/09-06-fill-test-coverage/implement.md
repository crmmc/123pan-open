# 执行计划：补齐测试用例

> 执行模式：主会话**严格串行**派发 trellis-implement 子代理（一次一个，禁止并行）；每批次完成后主会话核对再派发下一批。

## 前置状态

- [x] 基线记录：53% / 312 tests / mypy 0 错 / pylint 9.99（见 research/coverage-baseline.md）
- [x] 规划 artifacts：prd.md + design.md + implement.md + jsonl 清单
- [ ] `task.py start`（待用户批准最终规划摘要）

## 批次 0：测试基础设施（trellis-implement）

- [ ] `tests/conftest.py`：`qapp`（session 级 QApplication + offscreen 兜底）、`temp_db`、`fake_response` 共享 fixture（提炼自存量模式，见 design.md）
- [ ] pyproject `[tool.coverage.run]/[tool.coverage.report]` 配置
- [ ] Makefile：`test` / `coverage` / `lint` / `mypy` 目标
- [ ] 自验：全量 pytest 绿（312+）、mypy/pylint 绿、`uv run python -m pytest tests/test_config.py -q --cov=src/app/common/config.py` 能出报告

## 批次 1：common 小模块组（trellis-implement）

- [ ] config.py(7)、const.py(3)、credential_store.py(26)、filename_utils.py(8)、log.py(4)、resource.py(1)、speed_tracker.py(3)、concurrency.py(24)、database.py(21)、download_metadata.py(2)
- [ ] 纯函数分支用 `@pytest.mark.parametrize` 表测试；credential_store mock keyring；database 用 `temp_db`
- [ ] 自验 + 汇报（覆盖率、pragma 清单）

## 批次 2：common/api.py（trellis-implement）

- [ ] 310 miss：Pan123 业务方法、限流/重试、token 过期、下载流、_RWLock、_ProgressFileIO、_PrefetchResultSlot、format_file_size 等
- [ ] 全部 mock session/response；必要时按 design 加 `session` 可选注入参数（默认行为不变）
- [ ] 自验 + 汇报

## 批次 3：common/download_resume.py（trellis-implement）

- [ ] 190 miss：断点续传状态机、分片合并、进度恢复
- [ ] 文件系统用 tmp_path、时钟/sleep monkeypatch、网络 mock
- [ ] 自验 + 汇报

## 批次 4：view/file_interface.py（trellis-implement）【高风险·千行文件，允许追加第二轮】

- [ ] 1106 miss：文件浏览、右键菜单动作、拖拽上传、删除/重命名/移动流转
- [ ] 沿用 shiboken patch、QFileDialog/QMessageBox patch 惯例；widget 交互经 mock self 调用私有方法模式
- [ ] 自验 + 汇报

## 批次 5：view/transfer_interface.py（trellis-implement）【高风险·千行文件，允许追加第二轮】

- [ ] 625 miss：上传/下载任务线程、进度节流、任务持久化、重试与取消清理
- [ ] 线程类逻辑优先同步路径测试；sleep/timer monkeypatch；DB 用 temp_db
- [ ] 自验 + 汇报

## 批次 6：view 小窗口组（trellis-implement）

- [ ] cloud_interface.py(29)、move_window.py(130)、newfolder_window.py(31)、rename_window.py(31)、upload_conflict_dialog.py(44)
- [ ] 新建 5 个 test 文件；弹窗与父窗口交互全 mock
- [ ] 自验 + 汇报

## 批次 7：view 登录组（trellis-implement）

- [ ] login_window.py(75)、qr_login_page.py(138)、main_window.py(117)
- [ ] 二维码渲染 mock qrcode/Pillow；登录轮询 mock 网络；窗口切换断言信号
- [ ] 自验 + 汇报

## 批次 8：收尾组（trellis-implement）

- [ ] search_window.py(151)、setting_interface.py(37)
- [ ] 自验 + 汇报

## 批次间主会话核对（每批次后）

- [ ] `uv run python -m pytest tests/ -q` 全绿
- [ ] 触及模块覆盖率达标（≥98% 或仅剩带理由 pragma）
- [ ] mypy / pylint 绿
- [ ] 不达标 → 同文件追加派发一轮；连续两轮不达标 → 回到规划修 design

## 最终检查（trellis-check，最后一轮 2.2 全范围）

- [ ] 全量验证四件套（pytest + coverage + mypy + pylint）
- [ ] A1–A5 验收逐条核对（prd.md）
- [ ] pragma 逐条审查：文件、行、理由是否成立
- [ ] 全局覆盖率 ≥95% 终审

## 验证命令汇总

```bash
uv run python -m pytest tests/ -q
uv run python -m pytest tests/ -q --cov=src/app --cov-report=term-missing
bash script/mypy.sh
bash script/lint.sh
```

## 回滚点

- 每批次独立可回滚：`git checkout -- <批次触及文件>`
- conftest/Makefile/pyproject 基础设施单独一批，问题可独立撤销
- Phase 3.4 统一批量提交前工作区不 commit
