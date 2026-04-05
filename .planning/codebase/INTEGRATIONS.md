# External Integrations

**Analysis Date:** 2026-04-05

## APIs & External Services

**123云盘 (123pan) API:**
- https://www.123pan.com - File storage and management service
  - SDK/Client: Custom Python client (`src/app/common/api.py`)
  - Auth: Bearer token stored in SQLite database
  - Base URLs:
    - `https://www.123pan.com/b/api/user/sign_in` - Authentication
    - `https://www.123pan.com/api/file/list/new` - File listing
    - `https://www.123pan.com/a/api/file/download_info` - Download links
    - `https://www.123pan.com/a/api/file/batch_download_info` - Batch downloads
    - `https://www.123pan.com/a/api/file/upload_request` - Upload initiation
    - `https://www.123pan.com/b/api/file/s3_*` - S3 multipart upload operations
  - Client emulation: Android app protocol (user-agent, device info)
  - Features: Login, file listing, download/upload, folder operations, sharing

## Data Storage

**Databases:**
- SQLite (local file-based)
  - Connection: `~/.config/123pan/123pan.db`
  - Client: Python `sqlite3` stdlib
  - Purpose: Configuration storage, task persistence, download/upload metadata
  - Mode: WAL (Write-Ahead Logging) for concurrent access
  - Tables: `config`, `download_tasks`, `download_parts`, `upload_tasks`, `upload_parts`

**File Storage:**
- Local filesystem - User's Downloads directory (configurable)
- 123云盘 S3-compatible storage - Cloud storage backend

**Caching:**
- None (no caching layer)

## Authentication & Identity

**Auth Provider:**
- 123云盘 custom authentication
  - Implementation: Username/password → Bearer token flow
  - Token storage: SQLite database (`authorization` key in config table)
  - Device info: Randomly generated device type, OS version, login UUID
  - Auto-login: Supported via stored credentials

## Monitoring & Observability

**Error Tracking:**
- None (no external error tracking service)

**Logs:**
- Approach: Python logging module
- Storage: Local file logging (see `src/app/common/log.py`)
- Level: Configurable (default: INFO)

## CI/CD & Deployment

**Hosting:**
- GitHub Releases - Binary distribution platform
- Website: https://www.123panng.top (CloudFlare CDN)

**CI Pipeline:**
- GitHub Actions
  - Triggers: Version tags (v*), manual workflow dispatch
  - Platforms: Windows (windows-latest), Linux (ubuntu-latest)
  - Build tool: Nuitka (Python → standalone executable)
  - Artifacts: `123pan.exe` (Windows), `123pan` (Linux)
  - Type checking: mypy integrated in CI

## Environment Configuration

**Required env vars:**
- None (all configuration stored in SQLite)

**Secrets location:**
- SQLite database (`~/.config/123pan/123pan.db`)
- Sensitive keys: `userName`, `passWord`, `authorization`
  - Note: Passwords stored in plain text in SQLite (security concern)

## Webhooks & Callbacks

**Incoming:**
- None (no webhook receivers)

**Outgoing:**
- None (no webhook callbacks to external services)

---

*Integration audit: 2026-04-05*
