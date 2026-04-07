# 123pan 网页端 API 文档

> 通过 Chrome DevTools 抓包获取，2026-04-07

## 通用信息

- 基础域名：`https://www.123pan.com`（主要）、`https://api.123pan.cn`（用户信息等跨域接口）
- 认证方式：`authorization: Bearer <JWT token>`
- 请求签名：查询字符串中附带 `<random>=<timestamp>-<nonce>-<hash>` 防重放参数
- 公共 Headers：

```
platform: web
app-version: 3
loginuuid: <uuid>
content-type: application/json;charset=UTF-8
```

---

## 1. 用户相关

### 1.1 登录

```
POST /b/api/user/sign_in
```

> 项目中已实现，与网页端一致。

### 1.2 获取用户信息

```
GET https://api.123pan.cn/b/api/user/info
```

**注意**：使用独立域名 `api.123pan.cn`，跨域请求。

响应示例（部分）：

```json
{
  "code": 0,
  "data": {
    "UID": 1853154171,
    "Nickname": "13234937019",
    "SpaceUsed": 1590993588586,
    "SpacePermanent": 2199023255552,
    "Vip": false,
    "VipLevel": 0
  }
}
```

---

## 2. 文件列表

### 2.1 获取文件列表

```
GET /b/api/file/list/new
```

参数：

| 参数 | 类型 | 说明 |
|------|------|------|
| driveId | int | 固定 0 |
| limit | int | 每页数量，默认 100 |
| next | int | 分页游标，-1 表示无更多 |
| orderBy | string | 排序字段：`update_time` |
| orderDirection | string | `desc` / `asc` |
| parentFileId | int | 父文件夹 ID，0 为根目录 |
| trashed | bool | 是否回收站 |
| SearchData | string | 搜索关键词 |
| Page | int | 页码 |
| OnlyLookAbnormalFile | int | 是否只看异常文件 |
| event | string | `homeListFile` |
| operateType | int | 操作类型，列表=1，选择目标文件夹=7 |
| inDirectSpace | bool | 是否直链空间 |
| fileCategory | int | 文件分类，0=全部 |
| isSearchOrder | bool | 是否搜索排序 |

响应 InfoList 字段（完整）：

| 字段 | 类型 | 说明 | 项目是否依赖 |
|------|------|------|:---:|
| FileId | int | 文件/文件夹 ID | ✅ 核心 |
| FileName | string | 文件名 | ✅ |
| Type | int | 0=文件, 1=文件夹 | ✅ |
| Size | int | 字节大小 | ✅ |
| Etag | string | 文件哈希，下载必需 | ✅ |
| S3KeyFlag | string | 下载链接请求必需 | ✅ |
| ContentType | string | 内容类型 | ❌ |
| CreateAt | string | 创建时间 (ISO 8601) | ❌ |
| UpdateAt | string | 修改时间 (ISO 8601) | ❌ |
| ParentFileId | int | 父文件夹 ID | ❌ |
| Hidden | bool | 是否隐藏 | ❌ |
| Status | int | 文件状态 | ❌ |
| Category | int | 文件分类 | ❌ |
| PunishFlag | int | 违规标记 | ❌ |
| Trashed | bool | 是否在回收站 | ❌ |
| TrashedExpire | string | 回收站过期时间 | ❌ |
| TrashedAt | string | 删除时间 | ❌ |
| StorageNode | string | 存储节点 | ❌ |
| DirectLink | int | 直链状态 | ❌ |
| AbsPath | string | 绝对路径 | ❌ |
| PinYin | string | 拼音首字母 | ❌ |
| BusinessType | int | 业务类型 | ❌ |
| Thumbnail | string | 缩略图 URL | ❌ |
| StarredStatus | int | 收藏状态 | ❌ |
| HighLight | string | 搜索高亮 | ❌ |
| NewParentName | string | 父文件夹显示名 | ❌ |
| LiveSize | int | 实时大小 | ❌ |
| BaseSize | int | 基础大小 | ❌ |
| AbnormalAlert | int | 异常提醒 | ❌ |
| EnableAppeal | int | 是否可申诉 | ❌ |
| PreviewType | int | 预览类型 | ❌ |
| IsLock | bool | 是否锁定 | ❌ |
| DirectTranscodeStatus | int | 转码状态 | ❌ |
| Operable | bool | 是否可操作 | ❌ |
| RefuseReason | int | 拒绝原因 | ❌ |

> 项目核心依赖的 6 个字段（FileId、FileName、Type、Size、Etag、S3KeyFlag）网页端 API **全部返回**，完全兼容。

**与项目差异**：

- 路径前缀：网页端用 `/b/api/`，项目用 `/api/`（无前缀）
- 网页端多了 `event`、`operateType`、`inDirectSpace`、`fileCategory`、`isSearchOrder` 参数

### 2.2 选择目标文件夹时的文件列表

```
GET /b/api/file/list/new
```

移动/复制文件弹窗中浏览目标文件夹时使用，额外参数：

| 参数 | 值 | 说明 |
|------|-----|------|
| operateType | 7 | 选择目标文件夹模式 |
| FileType | 1 | 只显示文件夹 |
| parentFileName | string | 父文件夹名称（URL 编码） |

---

## 3. 文件操作

### 3.1 移动文件 — `mod_pid`

```
POST /b/api/file/mod_pid
```

请求体：

```json
{
  "fileIdList": [{"FileId": 39452244}],
  "parentFileId": 39532332,
  "event": "fileMove",
  "operatePlace": "bottom",
  "RequestSource": null
}
```

响应：

```json
{
  "code": 0,
  "data": {
    "Info": [{
      "FileId": 39452244,
      "FileName": "2.5次元的诱惑",
      "Type": 1,
      "ParentFileId": 39532332
    }]
  }
}
```

> 项目中**未实现**。

### 3.2 查看文件/文件夹详情

```
POST /b/api/restful/goapi/v1/file/details
```

请求体：

```json
{"file_ids": [28486993]}
```

响应：

```json
{
  "code": 0,
  "data": {
    "fileNum": 185,
    "dirNum": 31,
    "totalSize": 667321199610,
    "totalFileNum": 185,
    "totalDirNum": 31,
    "paths": [{"fileId": 0, "fileName": "我的文件"}]
  }
}
```

> 项目中**未实现**。新版 `restful/goapi/v1` 接口。

### 3.3 复制文件（异步）

**第一步：提交复制任务**

```
POST /b/api/restful/goapi/v1/file/copy/async
```

请求体：

```json
{
  "fileList": [
    {
      "fileId": 39452244,
      "size": 27450884957,
      "etag": "",
      "type": 1,
      "parentFileId": 39532332,
      "fileName": "2.5次元的诱惑",
      "driveId": 0
    }
  ],
  "targetFileId": 39534358
}
```

响应：

```json
{"code": 0, "data": {"taskId": 205310, "mode": 1}}
```

**第二步：轮询任务状态**

```
GET /b/api/restful/goapi/v1/file/copy/task?taskId=205310
```

响应：

```json
{
  "code": 0,
  "data": {
    "taskId": 205310,
    "status": 2,
    "errorCode": 0,
    "currentCount": 0,
    "createAt": "2026-04-07 14:40:39",
    "reason": ""
  }
}
```

> 项目中**未实现**。新版 `restful/goapi/v1` 异步接口。

### 3.4 重命名

```
POST /a/api/file/rename
```

> 项目中已实现。

### 3.5 删除/回收站

```
POST /a/api/file/trash
```

> 项目中已实现。

---

## 4. 文件信息

### 4.1 批量获取文件信息

```
POST /b/api/file/info
```

> 网页端在多处自动调用（进入文件夹、操作后刷新等）。

### 4.2 异常文件计数

```
GET /b/api/file/abnormal/count
```

---

## 5. 上传相关

### 5.1 上传请求（网页端）

```
POST /b/api/file/upload_request
```

> 网页端复制文件夹时自动调用（用于创建目标文件夹）。项目中已实现。

---

## 6. 系统/辅助接口

| 接口 | 方法 | 说明 |
|------|------|------|
| `/b/api/notice/announcement` | GET | 公告 |
| `/b/api/notice/new` | GET | 新消息（每 60s 轮询） |
| `/b/api/get/server/time` | GET | 服务器时间 |
| `/b/api/config/get` | POST | 获取配置（如离线下载限制） |
| `/b/api/advert/advertconfig` | GET | 广告配置 |
| `/b/api/v2/file/batch/rename_grayscale` | GET | 批量重命名灰度 |
| `/b/api/v3/3rd/app-id` | GET | 第三方应用 ID |
| `/b/api/video/play/conf` | GET | 视频播放配置 |
| `/b/api/file/video/play/list` | GET | 视频播放列表 |
| `/b/api/restful/goapi/v1/am_config/get?key=web_ads` | GET | 广告配置（新版） |
| `/b/api/restful/goapi/v1/user/report/info` | GET | 用户举报信息 |
| `/b/api/transfer/metrics/whether/report` | GET | 传输指标上报开关 |
| `/b/api/video/metrics/whether/report` | GET | 视频指标上报开关 |
| `/api/dydomain` | GET | 动态域名 |

---

## 7. 第三方服务

| 域名 | 用途 |
|------|------|
| `umini.shujupie.com/web_logs` | 数据派埋点上报（友盟） |
| `cloudauth-device-dualstack.cn-shanghai.aliyuncs.com` | 阿里云设备认证 |
| `1gkapk.captcha-open.aliyuncs.com` | 阿里云验证码 |
| `cross-frontend.123pan.com` | 微前端入口 |
| `cross-frontend.123957.com` | 微前端静态资源 |
| `at.alicdn.com` | 阿里 iconfont 图标 |

---

## 项目 vs 网页端 API 差异总结

| 维度 | 项目 (api.py) | 网页端 |
|------|--------------|--------|
| 伪装身份 | Android 客户端 (`platform: android`) | 浏览器 (`platform: web`) |
| API 路径前缀 | 混用 `/api/`、`/a/api/`、`/b/api/` | 统一 `/b/api/` |
| 新版 API | 未使用 | `/b/api/restful/goapi/v1/` |
| 请求签名 | 无 | 有（查询字符串防重放） |
| 移动文件 | 未实现 | `file/mod_pid` |
| 复制文件 | 未实现 | `file/copy/async` + `file/copy/task` |
| 文件详情 | 未实现 | `file/details` |
