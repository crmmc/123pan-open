# Roadmap: 123pan 第三方客户端

## Overview

v1.0 完成了文件夹上传功能。v1.1 聚焦登录体验重构：将"自动登录"拆分为"记住密码"和"保持登录"两个独立功能，并新增二维码登录方式。两个阶段——先重构现有登录流程，再添加二维码登录。

## Milestones

- ✅ **v1.0 文件夹上传** - Phases 1-2 (shipped 2026-04-06)
- 🚧 **v1.1 登录体验重构** - Phases 3-4 (in progress)

## Phases

**Phase Numbering:**
- Integer phases (1, 2): v1.0 milestone work (complete)
- Integer phases (3, 4): v1.1 milestone work
- Decimal phases (3.1, 3.2): Urgent insertions (marked with INSERTED)

<details>
<summary>✅ v1.0 文件夹上传 (Phases 1-2) - SHIPPED 2026-04-06</summary>

### Phase 1: Folder Upload UI
**Goal**: Users can upload a folder to the cloud via both a button and drag-drop, with clear visual feedback during the drag operation
**Depends on**: Nothing (backend pipeline already exists)
**Requirements**: UPLD-01, UPLD-02, UPLD-03
**Success Criteria** (what must be TRUE):
  1. User can click an "Upload Folder" button, select a local folder via system dialog, and see all files from that folder appear as upload tasks in the transfer list
  2. User can drag a folder from the system file manager onto the file list area and see all files from that folder appear as upload tasks
  3. File list area shows a visible highlight border when a folder is dragged over it, and the border disappears when the drag leaves or completes
**Plans**: 01-PLAN.md

### Phase 2: Upload Robustness
**Goal**: Folder uploads complete reliably even with large directories, API rate limits, partial failures, or expired tokens
**Depends on**: Phase 1
**Requirements**: ROBUST-01, ROBUST-02, ROBUST-03
**Success Criteria** (what must be TRUE):
  1. A folder upload with 50+ subdirectories completes without triggering 429 rate-limit errors from the 123pan API
  2. When one file in a folder batch fails preparation (e.g., permission denied), the remaining files still upload successfully
  3. A folder upload that takes long enough for the auth token to expire completes without manual re-login
**Plans**: 2 plans
Plans:
- [x] 02-01-PLAN.md -- 消除共享可变状态 + 统一 token 刷新拦截器
- [x] 02-02-PLAN.md -- 目录创建 429 退避 + 文件级上传容错

</details>

### 🚧 v1.1 登录体验重构 (In Progress)

**Milestone Goal:** 将"自动登录"拆分为"记住密码"和"保持登录"两个独立功能，并新增二维码登录方式

- [ ] **Phase 3: 登录状态重构** - 拆分"自动登录"为"记住密码"+"保持登录"，token 探测启动
- [ ] **Phase 4: 二维码登录** - 新增二维码登录方式，支持"保持登录"

## Phase Details

### Phase 3: 登录状态重构
**Goal**: 用户对密码持久化和 token 持久化有独立控制，启动时通过 token 探测决定是否直接进入主页面
**Depends on**: Phase 2 (v1.0 完成)
**Requirements**: UI-01, AUTH-01, AUTH-02, AUTH-03
**Success Criteria** (what must be TRUE):
  1. 登录界面显示"记住密码"和"保持登录"两个独立复选框，不再有"自动登录"
  2. 勾选"记住密码"后登录成功，下次打开登录界面时密码输入框自动填充；取消勾选后密码从数据库清空，输入框保留当前值
  3. 勾选"保持登录"（默认开启）后登录成功，关闭并重启应用时跳过登录界面直接进入主页面
  4. 取消"保持登录"后，重启应用总是显示登录界面，需要手动登录
**Plans**: 2 plans
Plans:
- [x] 03-01-PLAN.md -- DB 迁移 + 登录界面 UI 重构 + 细粒度保存逻辑
- [x] 03-02-PLAN.md -- 启动流程 token 探测 + 测试更新
**UI hint**: yes

### Phase 4: 二维码登录
**Goal**: 用户可以通过手机扫码登录，无需手动输入密码
**Depends on**: Phase 3
**Requirements**: QRC-01, QRC-02, QRC-03, QRC-04, QRC-05, QRC-06, UI-02
**Success Criteria** (what must be TRUE):
  1. 用户在登录界面点击二维码入口后，界面切换为二维码展示，能看到清晰的二维码图片
  2. 用户用手机 123pan App 扫码并确认后，桌面端自动获取 token 并进入主页面
  3. 扫码过程中界面实时显示状态变化（等待扫码、已扫码待确认、已确认）
  4. 二维码登录成功后，"保持登录"设置生效——勾选则持久化 token，未勾选则仅内存持有
  5. 用户可以从二维码界面返回密码登录界面
**Plans**: 2 plans
Plans:
- [ ] 04-01-PLAN.md -- Pan123 QR API 方法 + 依赖 + 测试
- [ ] 04-02-PLAN.md -- LoginDialog Tab 改造 + QRLoginPage widget + QR 登录测试 + 人工验证
**UI hint**: yes

## Progress

**Execution Order:**
Phases execute in numeric order: 1 -> 2 -> 3 -> 4

| Phase | Milestone | Plans Complete | Status | Completed |
|-------|-----------|----------------|--------|-----------|
| 1. Folder Upload UI | v1.0 | 1/1 | Complete | 2026-04-06 |
| 2. Upload Robustness | v1.0 | 2/2 | Complete | 2026-04-06 |
| 3. 登录状态重构 | v1.1 | 0/2 | Not started | - |
| 4. 二维码登录 | v1.1 | 0/2 | Not started | - |
