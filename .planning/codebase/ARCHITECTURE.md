# Architecture

**Analysis Date:** 2026-04-05

## Pattern Overview

**Overall:** Model-View-Controller (MVC) with Qt Signals/Slots for event-driven UI updates

**Key Characteristics:**
- Desktop GUI application built on PyQt6 with qfluentwidgets component library
- Thin API client layer over 123pan cloud storage REST APIs
- SQLite-based persistence for user config and transfer task state
- Multi-threaded download/upload with concurrent worker pools
- Signal-based progress reporting and UI updates

## Layers

**Presentation Layer (UI):**
- Purpose: User interface rendering and interaction handling
- Location: `src/app/view/`
- Contains: MainWindow, FileInterface, TransferInterface, SettingInterface, CloudInterface, and dialog windows
- Depends on: PyQt6, qfluentwidgets, common business logic layer
- Used by: Application entry point

**Business Logic Layer:**
- Purpose: API client, file operations, transfer orchestration
- Location: `src/app/common/`
- Contains: Pan123 API client, download/upload managers, metadata handlers, retry logic
- Depends on: requests library, Database layer, threading primitives
- Used by: All UI components

**Data Persistence Layer:**
- Purpose: Configuration storage and task state management
- Location: `src/app/common/database.py`
- Contains: Database singleton with SQLite backend
- Depends on: sqlite3, json for config serialization
- Used by: All layers requiring persistent state

**Resource Layer:**
- Purpose: Static assets and styling
- Location: `src/app/resource/`
- Contains: QSS stylesheets for light/dark themes, Qt resource files
- Depends on: Qt resource system
- Used by: UI components for styling

## Data Flow

**Application Startup:**

1. Entry point (`src/123pan.py`) initializes QApplication with high-DPI support
2. MainWindow created and initializes login flow
3. Auto-login attempted if credentials saved; otherwise show LoginDialog
4. On successful login, Pan123 API client instantiated and passed to all sub-interfaces
5. FileInterface loads initial directory listing from cloud storage

**File Browser Flow:**

1. User navigates directories via FileInterface tree/breadcrumb controls
2. FileInterface calls Pan123.get_dir_by_id() to fetch file listings
3. API client makes HTTP requests to 123pan REST endpoints
4. Response data parsed and displayed in TreeWidget/TableWidget
5. User actions (download, upload, delete, rename) trigger corresponding API calls

**Download Flow:**

1. User selects files and initiates download via FileInterface
2. TransferInterface creates DownloadTask with file metadata
3. download_resume.stream_download_from_url() manages multi-threaded download
4. Worker threads fetch file parts concurrently with rate limit handling
5. Progress updates emitted via Qt signals to update UI
6. Download state persisted to Database for resume capability

**Upload Flow:**

1. User selects local files and initiates upload via FileInterface
2. Pan123.upload_file_stream() handles multi-part upload
3. File MD5 calculated and upload request sent to 123pan API
4. S3 presigned URLs obtained for each part
5. Worker pool uploads parts concurrently with 429 rate limit handling
6. Parts merged and upload completed via API calls
7. Upload state persisted for resume capability

**State Management:**
- User config: JSON-serialized key-value pairs in SQLite config table
- Task state: Download/upload tasks with part-level progress tracking
- Thread-safe writes using threading.Lock() for database operations
- Qt signals for cross-thread communication (worker threads → UI thread)

## Key Abstractions

**Pan123 API Client:**
- Purpose: Encapsulates 123pan cloud storage REST API interactions
- Examples: `src/app/common/api.py` (Pan123 class)
- Pattern: Session-based client with automatic token refresh and retry logic

**TransferTask:**
- Purpose: Base abstraction for upload/download tasks with progress tracking
- Examples: UploadTask, DownloadTask in `src/app/view/transfer_interface.py`
- Pattern: State machine with status transitions (等待中→下载中→已完成/失败)

**Database Singleton:**
- Purpose: Centralized persistence with thread-safe access
- Examples: `src/app/common/database.py` (Database class)
- Pattern: Single SQLite connection with WAL mode for concurrent reads

**Signal Emitter Pattern:**
- Purpose: Progress reporting from worker threads to UI
- Examples: Progress signals in download/upload operations
- Pattern: PyQt6 pyqtSignal for type-safe cross-thread communication

## Entry Points

**main():**
- Location: `src/123pan.py`
- Triggers: Application launch
- Responsibilities: Qt application initialization, window creation, event loop start

**MainWindow.__init__():**
- Location: `src/app/view/main_window.py`
- Triggers: After Qt app initialization
- Responsibilities: Sub-interface creation, login flow orchestration, navigation setup

**LoginDialog:**
- Location: `src/app/view/login_window.py`
- Triggers: Manual login or failed auto-login
- Responsibilities: Credential collection, Pan123 client instantiation, credential persistence

## Error Handling

**Strategy:** Multi-layered with graceful degradation

**Patterns:**
- Network errors: Automatic retry with exponential backoff (via urllib3.Retry)
- API errors: Token refresh on 401/2, error message display via InfoBar
- Transfer errors: Part-level retry with task pause/resume capability
- Database errors: Thread-safe writes with exception logging

**Cross-Cutting Concerns:**

**Logging:** Structured logging via `src/app/common/log.py` with module-level loggers

**Validation:** Input validation in UI layer, metadata validation in business layer

**Authentication:** Bearer token storage in Database, automatic refresh on expiry

**Concurrency:** ThreadPoolExecutor for async operations, threading.Lock for shared state

---

*Architecture analysis: 2026-04-05*
