# Coding Conventions

**Analysis Date:** 2026-04-05

## Naming Patterns

**Files:**
- `snake_case.py` for all Python modules
- Test files: `test_*.py` in `tests/` directory
- `__init__.py` marks packages (empty in `tests/`)

**Functions:**
- `snake_case` for all functions and methods
- Private methods: prefixed with underscore (e.g., `_create_directory`, `_get_db_path`)
- Magic methods: dunder names (e.g., `__init__`, `__loadPanAndData`)

**Variables:**
- `snake_case` for local variables and parameters
- `CONSTANT_CASE` for module-level constants (e.g., `CONFIG_DIR`, `LOG_FILE`)
- Private instance variables: prefixed with underscore (e.g., `_db_path`, `_write_lock`)

**Classes:**
- `PascalCase` for all classes (e.g., `Pan123`, `Database`, `TransferTaskManager`)
- Private classes: prefixed with underscore in tests (e.g., `_MockResponse`, `_PauseTask`)

**Types:**
- Type hints used extensively in newer code (`database.py`, `config.py`)
- Format: `param: type`, `-> return_type`
- Examples:
  ```python
  def get_config(self, key: str, default=None) -> dict | None:
  def save_download_task(self, task: dict) -> None:
  ```

## Code Style

**Formatting:**
- No explicit formatter configured in `pyproject.toml`
- Standard Python PEP 8 style followed
- 4-space indentation
- Line length appears to follow standard ~88-100 characters (evident in long parameter lists)

**Linting:**
- `pylint>=4.0.5` and `mypy>=1.19.1` listed as optional dev dependencies
- No explicit lint configuration files present
- Type checking available but not enforced

**Import Organization:**
Standard order observed:
1. Standard library imports (`os`, `json`, `pathlib`, `threading`)
2. Third-party imports (`requests`, `PyQt6`, `pytest`, `qfluentwidgets`)
3. Local imports (relative imports from `src.app.*`)

**Path Aliases:**
- No path aliases configured
- Uses relative imports: `from .database import Database`, `from ..common import resource`
- Test imports use absolute paths from `src`: `from src.app.common.api import Pan123`

## Error Handling

**Patterns:**
- Exceptions caught with specific exception types (`FileNotFoundError`, `json.JSONDecodeError`)
- Error logging via `logger.error()` before raising or returning
- Network errors handled with `requests.exceptions.*` specific exceptions
- SQLite errors handled implicitly through SQLite3 exceptions

**Database operations:**
- Use `INSERT OR IGNORE` and `INSERT OR REPLACE` to avoid constraint errors
- No explicit transaction error handling in `database.py`
- Write operations protected by `threading.Lock`

**API errors:**
- Check response codes: `if res_code != 0:`, `if code != 200:`
- Return error codes from API methods (e.g., `return res_code_login`)
- Raise `RuntimeError` for critical failures: `raise RuntimeError("上传请求失败: ...")`
- Retry logic handled by `urllib3.Retry` on HTTPAdapter for network errors only

**Validation:**
- Input validation in API methods: `if file_path_obj.is_dir(): raise IsADirectoryError`
- Type checking in some places: `if not str(file).isdigit(): raise ValueError`

## Logging

**Framework:** Standard Python `logging` module via custom `get_logger()`

**Logger creation:**
```python
from .log import get_logger
logger = get_logger(__name__)
```

**Log levels:**
- `logger.debug()` - Detailed operation info (worker threads, HTTP details)
- `logger.info()` - Important operations (login success, upload progress)
- `logger.warning()` - Recoverable issues (token expiry, rate limiting)
- `logger.error()` - Failures (upload failed, connection errors)

**Configuration:**
- Log file: `CONFIG_DIR / "123pan.log"` (platform-specific location)
- Format: `%(asctime)s - %(name)s - %(levelname)s - %(message)s`
- Handlers: Both file and console
- Level: `DEBUG` by default (comment shows `INFO` alternative)
- Encoding: UTF-8

**Patterns:**
- Module-level logger instantiated once
- Chinese log messages accepted
- Error context included: `logger.error(f"上传请求失败: {json.dumps(res_json)}")`

## Comments

**When to Comment:**
- Docstrings for all public methods and classes
- Inline comments for complex logic (e.g., threading primitives, retry strategies)
- Section markers: `# ---- Download tasks ----`, `# ---- Upload tasks ----`

**JSDoc/TSDoc:**
- Python docstrings (triple quotes) used for class/method documentation
- Format: Google-style docstrings
```python
def login(self):
    """登录123云盘账户并获取授权令牌"""
```

**Parameter documentation:**
```python
def ensure_directory(self, parent_id, dirname):
    """确保指定父目录下存在目标子目录。"""
```

## Function Design

**Size:**
- Functions typically 10-50 lines
- Longer functions (100+ lines) contain complex control flow (e.g., `upload_file_stream`)
- Maximum observed: ~400 lines for `upload_file_stream` (handles multipart upload, threading, retries)

**Parameters:**
- Use keyword arguments for options: `upload_file_stream(file_path, dup_choice=1, task_id=None, signals=None)`
- Required parameters come first, optional parameters last
- Boolean flags use descriptive names: `save=True`, `all=False`, `remakedir=False`

**Return Values:**
- API methods: Return status codes (0, 200) or data
- Database methods: Return `None` for success, data for queries, `bool` for operations
- Error signaling: Either return error code OR raise exception (not both)
- Special return strings for UI state: `"已取消"`, `"已暂停"`, `"复用上传成功"`

## Module Design

**Exports:**
- No explicit `__all__` declarations
- Public API implicit (non-prefixed names)
- Tests import specific classes/functions

**Barrel Files:**
- `__init__.py` in packages (empty in `tests/`, likely exports in `src/app/common/`)
- `tests/__init__.py` is empty (tests not imported as package)

**Module organization:**
- `database.py` - Database singleton, config, download/upload tasks
- `api.py` - API client (`Pan123`), utility functions, task management
- `config.py` - Config file loading/saving (merged into database in newer code)
- `log.py` - Logging configuration
- `view/*.py` - PyQt6 UI components
- `common/*.py` - Shared utilities (resource loading, styles, constants)

---

*Convention analysis: 2026-04-05*
