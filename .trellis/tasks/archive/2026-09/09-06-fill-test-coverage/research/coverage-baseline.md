# 覆盖率基线（2026-09-06，312 tests 全绿）

总体：**53%**（6617 语句，3113 未覆盖）。达标线：总体 ≥95%（允许残留 ≈330 行）。

| 文件 | 语句 | 未覆盖 | 覆盖率 | 派发批次 |
|---|---:|---:|---:|---|
| src/app/view/file_interface.py | 1383 | 1106 | 20% | 4 |
| src/app/view/transfer_interface.py | 1283 | 625 | 51% | 5 |
| src/app/common/api.py | 1073 | 310 | 71% | 2 |
| src/app/common/download_resume.py | 707 | 190 | 73% | 3 |
| src/app/view/search_window.py | 217 | 151 | 30% | 8 |
| src/app/view/qr_login_page.py | 266 | 138 | 48% | 7 |
| src/app/view/move_window.py | 152 | 130 | 14% | 6 |
| src/app/view/main_window.py | 243 | 117 | 52% | 7 |
| src/app/view/login_window.py | 205 | 75 | 63% | 7 |
| src/app/view/upload_conflict_dialog.py | 60 | 44 | 27% | 6 |
| src/app/view/cloud_interface.py | 38 | 29 | 24% | 6 |
| src/app/view/newfolder_window.py | 38 | 31 | 18% | 6 |
| src/app/view/rename_window.py | 38 | 31 | 18% | 6 |
| src/app/common/credential_store.py | 51 | 26 | 49% | 1 |
| src/app/common/concurrency.py | 124 | 24 | 81% | 1 |
| src/app/common/database.py | 281 | 21 | 93% | 1 |
| src/app/view/setting_interface.py | 229 | 37 | 84% | 8 |
| src/app/common/api.py 之外 common 小文件 | ~160 | ~28 | — | 1 |
| 合计 | 6617 | 3113 | 53% | |

common 小文件明细（批次 1）：config.py 59%(7)、const.py 82%(3)、filename_utils.py 78%(8)、log.py 86%(4)、resource.py 89%(1)、speed_tracker.py 95%(3)、download_metadata.py 96%(2)。

## 代码结构事实

- `src/app/common/`：业务层。api.py（Pan123 网络封装，session 在 `__init__` 创建，`self.session = requests.Session()` @ api.py:288）、database.py（sqlite 单例 Database）、download_resume.py（断点续传，707 语句）、concurrency.py、credential_store.py（keyring）等 13 模块。
- `src/app/view/`：PySide6 界面层 12 模块 + resource/。file_interface.py 与 transfer_interface.py 是两个千行级大文件。
- 关键类：Pan123（api.py:257）、UploadThread（view/transfer_interface.py，被 test_pan_api.py 导入）。

## 工具链版本

- Python 3.12（.venv；CI 用 3.13）、pytest 9.0.2、pytest-cov 7.1.0、coverage 7.13.5
- PySide6 ≥6.8、qfluentwidgets（mypy ignore_missing_imports）
- mypy 基线 0 错误、pylint 基线 9.99/10 —— 任何新代码不得劣化
