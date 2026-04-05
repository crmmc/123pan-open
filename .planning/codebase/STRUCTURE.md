# Codebase Structure

**Analysis Date:** 2026-04-05

## Directory Layout

```
123pan/
├── dist/                  # Build output (Nuitka compiled binaries)
├── doc/                   # Documentation (API specs, etc.)
├── script/                # Build/deployment scripts
├── src/                   # Application source code
│   ├── 123pan.py         # Application entry point
│   └── app/
│       ├── common/       # Business logic layer
│       │   ├── api.py                    # 123pan REST API client
│       │   ├── config.py                # Configuration directory paths
│       │   ├── const.py                 # Application constants
│       │   ├── database.py              # SQLite persistence layer
│       │   ├── download_metadata.py     # Download metadata validation
│       │   ├── download_resume.py       # Multi-threaded download engine
│       │   ├── log.py                   # Logging configuration
│       │   ├── resource.py              # Resource loading utilities
│       │   ├── speed_tracker.py         # Transfer speed calculation
│       │   └── style_sheet.py           # QSS stylesheet management
│       ├── resource/     # Static assets
│       │   └── qss/                      # Stylesheets
│       │       ├── light/                # Light theme QSS files
│       │       └── dark/                 # Dark theme QSS files
│       └── view/         # Presentation layer
│           ├── cloud_interface.py        # Account/settings interface
│           ├── file_interface.py         # File browser interface
│           ├── login_window.py           # Login dialog
│           ├── main_window.py            # Main application window
│           ├── newfolder_window.py       # New folder dialog
│           ├── rename_window.py          # Rename dialog
│           ├── setting_interface.py      # Settings interface
│           └── transfer_interface.py     # Transfer manager interface
├── tests/                # Test suite
├── .github/              # GitHub workflows/issue templates
├── .venv/                # Python virtual environment
├── pyproject.toml        # Project dependencies and metadata
└── uv.lock              # Dependency lock file
```

## Directory Purposes

**`src/app/common/`:**
- Purpose: Shared business logic, API client, data persistence
- Contains: Core application logic independent of UI framework
- Key files: `api.py` (Pan123 client), `database.py` (persistence), `download_resume.py` (transfer engine)

**`src/app/view/`:**
- Purpose: PyQt6-based UI components
- Contains: Main window, sub-interfaces, dialog windows
- Key files: `main_window.py` (app container), `file_interface.py` (file browser), `transfer_interface.py` (transfer manager)

**`src/app/resource/`:**
- Purpose: Static UI assets and styling
- Contains: QSS stylesheets for theming
- Key files: `qss/light/*.qss`, `qss/dark/*.qss`

**`tests/`:**
- Purpose: Unit and integration tests
- Contains: Test files for API, config, download, login flow, etc.
- Key files: `test_pan_api.py`, `test_download_resume.py`, `test_transfer_interface.py`

## Key File Locations

**Entry Points:**
- `src/123pan.py`: Application bootstrap and main event loop

**Configuration:**
- `src/app/common/config.py`: Config directory paths (platform-specific)
- `src/app/common/const.py`: Application constants (version, device types, OS versions)
- `pyproject.toml`: Project metadata and dependencies

**Core Logic:**
- `src/app/common/api.py`: Pan123 API client (1017 lines, handles all 123pan REST operations)
- `src/app/common/database.py`: Database singleton with config/task tables
- `src/app/common/download_resume.py`: Multi-threaded download engine with resume support
- `src/app/common/download_metadata.py`: Download metadata validation and resolution
- `src/app/common/speed_tracker.py`: Transfer speed calculation utilities

**UI Components:**
- `src/app/view/main_window.py`: FluentWindow container with navigation
- `src/app/view/file_interface.py`: File browser with tree/breadcrumb navigation
- `src/app/view/transfer_interface.py`: Download/upload queue management
- `src/app/view/setting_interface.py`: User preferences configuration
- `src/app/view/cloud_interface.py`: Account info and logout
- `src/app/view/login_window.py`: Authentication dialog

**Testing:**
- `tests/`: Root test directory
- `tests/test_pan_api.py`: API client unit tests
- `tests/test_download_resume.py`: Download engine tests
- `tests/test_transfer_interface.py`: Transfer UI tests
- `tests/test_login_flow.py`: Authentication flow tests

## Naming Conventions

**Files:**
- Modules: `lowercase_with_underscores.py` (e.g., `download_resume.py`, `file_interface.py`)
- Classes: `PascalCase` (e.g., `Pan123`, `MainWindow`, `FileInterface`)
- Functions/Methods: `lowercase_with_underscores` (e.g., `get_dir_by_id`, `upload_file_stream`)
- Constants: `UPPER_CASE` (e.g., `MAX_STORAGE_CAPACITY`, `PART_SIZE`)

**Directories:**
- All lowercase: `common/`, `view/`, `resource/`
- Theme-specific: `light/`, `dark/`

**Private Members:**
- Private methods: `_method_name` (single underscore prefix)
- Private class attributes: `__attribute_name` (double underscore for name mangling)

## Where to Add New Code

**New Feature:**
- Primary code: `src/app/common/` (business logic) or `src/app/view/` (UI components)
- Tests: `tests/test_<feature_name>.py`

**New Component/Module:**
- Implementation: `src/app/view/<component_name>_interface.py` for UI, `src/app/common/<module_name>.py` for logic
- Styles: `src/app/resource/qss/light/<component_name>.qss` and `src/app/resource/qss/dark/<component_name>.qss`

**Utilities:**
- Shared helpers: `src/app/common/<utility_name>.py`
- Test fixtures: `tests/conftest.py` (if needed)

**Configuration:**
- User preferences: Add to `database.py` `_init_defaults()` and `setting_interface.py`
- Constants: Add to `const.py`
- Platform-specific paths: Add to `config.py`

## Special Directories

**`dist/`:**
- Purpose: Nuitka compiled application binaries
- Generated: Yes
- Committed: No (in .gitignore)

**`.venv/`:**
- Purpose: Python virtual environment
- Generated: Yes
- Committed: No

**`src/app/resource/qss/`:**
- Purpose: Qt stylesheets for UI theming
- Generated: No
- Committed: Yes

**`tests/__pycache__/`:**
- Purpose: Python bytecode cache
- Generated: Yes
- Committed: No

**`CONFIG_DIR` (runtime):**
- Purpose: User configuration and data storage
- Location: Platform-specific (Windows: `%APPDATA%\Qxyz17\123pan`, Unix: `~/.config/Qxyz17/123pan`)
- Contains: `123pan.db` (SQLite database), `tmp/` (download temporary files)

---

*Structure analysis: 2026-04-05*
