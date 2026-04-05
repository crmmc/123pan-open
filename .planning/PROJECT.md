# 123pan 第三方客户端

## What This Is

一个基于 PyQt6 + qfluentwidgets 的 123pan 云盘第三方桌面客户端，目标是替代官方客户端的基本功能（文件浏览、上传、下载），提供更简洁实用的体验。

## Core Value

文件管理的核心三件事——浏览、上传、下载——必须稳定可靠、操作直观。

## Requirements

### Validated

<!-- 已实现的功能 -->

- ✓ 用户登录/自动登录 — existing
- ✓ 文件列表浏览与目录导航 — existing
- ✓ 单文件上传（含断点续传） — existing
- ✓ 多线程分片下载（含断点续传） — existing
- ✓ 传输任务管理（进度、暂停、重试） — existing
- ✓ 设置页面（下载路径、并发数等） — existing

### Active

<!-- 当前需要实现的功能 -->

- [ ] 文件夹拖拽上传（拖入文件列表区域上传整个文件夹）
- [ ] 文件夹选择上传（通过按钮选择本地文件夹上传）

### Out of Scope

<!-- 明确不做 -->

- 离线下载 — 用户不需要
- 文件预览（图片/视频/文档） — 非核心
- 分享管理 — 非核心
- 回收站 — 非核心
- 同步盘 — 复杂度过高
- 多账号支持 — 非核心

## Context

- 这是一个 brownfield 项目，代码库已有完整的文件浏览、上传、下载基础
- 技术栈：Python 3.12+ / PyQt6 / qfluentwidgets / SQLite / requests
- API 层封装在 `src/app/common/api.py` 的 Pan123 类中
- 上传流程：计算 MD5 → 请求上传 → 获取 S3 presigned URL → 分片上传 → 合并
- 当前上传仅支持单文件，需要扩展支持文件夹（递归创建目录 + 逐文件上传）

## Constraints

- **Tech Stack**: Python 3.12+ / PyQt6 / qfluentwidgets — 已确定，不更换
- **API**: 依赖 123pan REST API，需处理 token 过期和 429 限流
- **Platform**: 主要在 macOS 开发，构建目标 Windows/Linux

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| PyQt6 + qfluentwidgets | 现代 Fluent Design 风格，组件丰富 | ✓ Good |
| SQLite 本地存储 | 轻量、无需服务端 | ✓ Good |
| 多线程分片传输 | 大文件传输性能 | ✓ Good |

---
*Last updated: 2026-04-05 after initialization*
