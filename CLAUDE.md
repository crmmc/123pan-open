# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

123pan-open 是高性能 123 云盘第三方桌面客户端，基于 PySide6 + Fluent Design，支持多线程上传/下载、断点续传。使用安卓客户端 API 端点。

## 常用命令

```bash
# 运行
uv sync
uv run src/123pan-open.py

# 测试（CI 中使用 QT_QPA_PLATFORM=offscreen）
uv run pytest tests/ -v              # 全部测试
uv run pytest tests/test_xxx.py -v   # 单个测试文件
uv run pytest tests/test_xxx.py::test_func -v  # 单个用例

# 类型检查
bash script/mypy.sh                  # 全量 mypy
bash script/mypy.sh src/app/common/api.py  # 单文件

# Lint
bash script/lint.sh                  # 全量 pylint
bash script/lint.sh src/app/common/  # 指定目录

# 构建（Nuitka 编译为单文件可执行）
uv sync --extra build
bash script/build.sh
```

## 架构

### 目录结构

```
src/
├── 123pan-open.py              # 入口：QApplication 初始化、登录流程
└── app/
    ├── common/                 # 纯逻辑层（禁止导入 PySide6）
    │   ├── api.py              # 123 云盘 API 封装（Pan123 类）：登录、文件列表、上传、下载 URL 获取
    │   ├── concurrency.py      # 上传/下载共用的 probe-first 慢启动并发调度器
    │   ├── database.py         # SQLite 单例（WAL 模式）：配置 KV + 任务持久化
    │   ├── download_resume.py  # 下载执行层：分片下载、合并、断点续传
    │   ├── download_metadata.py # 下载元数据版本校验
    │   ├── speed_tracker.py    # 滑动窗口速度计算（worker 写 → UI flush）
    │   ├── config.py           # CONFIG_DIR 路径定义（跨平台）
    │   ├── log.py              # 日志配置（文件 + 控制台）
    │   └── const.py            # 版本号、设备指纹列表
    └── view/                   # UI 层（PySide6 + qfluentwidgets）
        ├── main_window.py      # FluentWindow 主窗口，导航栏管理
        ├── file_interface.py   # 文件管理页：双栏布局、面包屑、拖拽上传、右键菜单
        ├── transfer_interface.py # 传输页：下载/上传任务表、QThread worker 管理
        ├── setting_interface.py # 设置页：配置项 UI 控件
        ├── cloud_interface.py  # 账户页：用户信息、退出登录
        ├── login_window.py     # 登录弹窗（密码 + 扫码）
        └── ...                 # 重命名、新建文件夹、移动、搜索等弹窗
```

### 核心数据流

```
file_interface.py (用户选文件)
    → transfer_interface.py (创建任务、启动 QThread)
        → api.py / download_resume.py (纯 Python 网络 I/O)
            → concurrency.py (slow_start_scheduler 管理并发)
                → _SignalsAdapter → Qt Signal 回调 UI 更新
```

### 关键设计

**并发模型 — probe-first 慢启动**：`concurrency.py:slow_start_scheduler` 先启动 1 个 probe worker，收到首字节后转正 (allowed += 1)，再启 probe，逐个验证到 max_workers。上传和下载共用同一调度器。

**api.py 无 Qt 依赖**：`api.py` 是纯 Python 模块，通过 `_SignalsAdapter` 代理模式与 Qt 信号解耦。`transfer_interface.py` 中构造适配器注入回调。

**Database 单例**：`Database.instance()` 返回线程安全的 SQLite 单例（WAL 模式）。配置以 KV 存储，任务以 JSON 序列化存储。Schema 版本迁移在 `_migrate()` 中。

**速度追踪**：`SpeedTracker` 采用无锁 deque，worker 线程 `record()`，UI 线程 `flush()` 消费后计算滑动窗口速度。

## PySide6 编码约定

### 信号槽模式

| 模式 | 适用场景 | 示例文件 |
|------|---------|---------|
| `QThread` + 类级 `Signal` | 长时间传输任务（上传/下载） | `transfer_interface.py` |
| `QRunnable` + 内嵌 `QObject` Signal | 短时网络请求（列表加载、文件操作） | `file_interface.py` |
| Widget 级 `Signal` | 页面间通信（退出登录、登录成功） | `cloud_interface.py` |
| `_SignalsAdapter` 代理 | 解耦 api.py 与 Qt 依赖 | `transfer_interface.py` → `api.py` |

关键规则：
- 循环中连接信号用 `lambda t=task:` 默认参数捕获，避免闭包陷阱
- `QRunnable` 的 signals 对象必须手动持有引用，防止 GC 回收
- 设置页面信号连接集中在 `__connectSignalToSlot` 方法
- `api.py` 是纯 Python 模块，**禁止**导入 PySide6

### Qt 枚举风格

使用完全限定枚举路径：`Qt.AlignmentFlag.AlignCenter`（不用旧式 `Qt.AlignCenter`）

### Widget 继承

- 主窗口：`FluentWindow`（qfluentwidgets）
- 功能页面：`QWidget` / `ScrollArea`
- 弹窗：`QDialog`
- qfluentwidgets 组件作为成员使用，不继承

## 设置页面原则

所有 `database.py` `_init_defaults` 中的用户可调配置项，都必须在设置页面（`setting_interface.py`）提供对应的 UI 控件。新增配置项时同步添加设置界面入口。

当前需要在设置页面暴露的配置项：

| 配置 key | 说明 | 类型 | 范围 |
|----------|------|------|------|
| defaultDownloadPath | 默认下载路径 | 路径选择 | - |
| askDownloadLocation | 每次询问下载位置 | 开关 | bool |
| rememberPassword | 记住密码 | 开关 | bool |
| stayLoggedIn | 保持登录 | 开关 | bool |
| maxDownloadThreads | 单任务下载并发数 | SpinBox | 1-16 |
| maxUploadThreads | 单任务上传并发数 | SpinBox | 1-16 |
| maxConcurrentDownloads | 同时下载任务数 | SpinBox | 1-5 |
| maxConcurrentUploads | 同时上传任务数 | SpinBox | 1-5 |
| retryMaxAttempts | 分块重试次数 | ComboBox | 0-5 |
| uploadPartSizeMB | 上传分片大小 | SpinBox | 5-16 MB |
| downloadPartSizeMB | 下载分片大小 | SpinBox | 4-32 MB |
