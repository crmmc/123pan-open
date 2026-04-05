# Testing Patterns

**Analysis Date:** 2026-04-05

## Test Framework

**Runner:**
- `pytest>=8.0.0` (version 8.0.0+)
- Config: No explicit `pytest.ini` or `pyproject.toml` config section
- Uses standard pytest discovery

**Assertion Library:**
- Built-in `assert` statements (pytest's assertion rewriting)
- No external assertion library (no `hamcrest`, `assertpy`)

**Run Commands:**
```bash
pytest                 # Run all tests (inferred)
pytest -v              # Verbose mode (inferred)
pytest tests/          # Run tests directory (inferred)
```

**Note:** No explicit test scripts in `Makefile` or project docs.

## Test File Organization

**Location:**
- Separate `tests/` directory at project root
- Co-located with `src/` (sibling directories)

**Naming:**
- Pattern: `test_<module_name>.py`
- Examples: `test_config.py`, `test_api.py`, `test_download_resume.py`
- Mirrors source structure: `test_pan_api.py` tests `src/app/common/api.py`

**Structure:**
```
tests/
├── __init__.py              # Empty file, marks package
├── test_config.py           # ConfigManager tests
├── test_pan_api.py          # Pan123 API client tests
├── test_retry.py            # Session retry mechanism tests
├── test_utils.py            # Utility function tests
├── test_task_manager.py     # TransferTaskManager tests
├── test_download_resume.py  # Download resume logic tests
└── ...
```

## Test Structure

**Suite Organization:**
```python
class TestFeatureName:
    def test_specific_behavior(self):
        # Arrange
        # Act
        # Assert
```

**Class-based organization:**
- Group related tests in classes (e.g., `TestLoadConfig`, `TestSaveConfig`)
- Class name describes what's being tested
- Test methods named `test_<action>_<expected_outcome>` or `test_<scenario>`

**Examples from codebase:**
```python
class TestFormatFileSize:
    def test_bytes(self):
        assert format_file_size(0) == "0 B"

class TestLoadConfig:
    def test_default_config_when_no_file(self, tmp_config_dir):
        config = ConfigManager.load_config()
        assert config["userName"] == ""

class TestThreadSafety:
    def test_concurrent_creates(self, manager):
        # Multi-threaded test pattern
```

**Setup/Teardown:**
- pytest fixtures used extensively: `@pytest.fixture`
- Common fixtures: `tmp_path` (built-in), `tmp_config_dir`, `manager` (custom)
- No explicit `setup_method`/`teardown_method` usage
- Fixture-based setup preferred

**Patterns:**
- **Arrange-Act-Assert**: Clear separation in test methods
- **Given-When-Then**: Not explicitly used, but test structure follows this
- **AAA pattern** evident in most tests

## Mocking

**Framework:** `unittest.mock` (standard library)

**Patterns:**
```python
from unittest.mock import patch, MagicMock

# Patch module-level constants
def test_with_patch(tmp_path, monkeypatch):
    monkeypatch.setattr(download_resume, "CONFIG_DIR", tmp_path)

# Patch class methods
with patch("src.app.common.api.ConfigManager") as MockConfig:
    MockConfig.load_config.return_value = {...}

# Mock HTTP responses
def fake_get(url, headers=None, stream=True, timeout=30):
    return _MockResponse(body=content, status_code=206)

monkeypatch.setattr(download_resume.requests, "get", fake_get)
```

**Mock objects:**
- `_make_mock_response()` helper creates mock HTTP responses
- `MagicMock` used for flexible mocking
- `SimpleNamespace` used for test data objects

**What to Mock:**
- File I/O: `monkeypatch.setattr()` for CONFIG_DIR, file paths
- HTTP requests: `requests.get`, `requests.post`, `requests.head`
- Database: `ConfigManager` methods (avoid actual SQLite)
- Time: `time.sleep` mocked to avoid delays
- External dependencies: All external APIs mocked

**What NOT to Mock:**
- Business logic under test
- Data structures (dictionaries, lists)
- Utility functions that are fast and deterministic

**Fixture mocking:**
```python
@pytest.fixture
def tmp_config_dir(tmp_path):
    """将配置目录重定向到临时目录"""
    config_file = tmp_path / "config.json"
    with patch.object(config_module, "CONFIG_DIR", tmp_path), \
         patch.object(config_module, "CONFIG_FILE", config_file):
        yield tmp_path
```

## Fixtures and Factories

**Test Data:**
```python
def _make_resume_task(out_path, etag):
    return SimpleNamespace(
        resume_id=build_resume_id("alice", 100, str(out_path)),
        account_name="alice",
        file_name=out_path.name,
        # ... more fields
    )

def _make_mock_response(status_code=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {"code": 0}
    return resp
```

**Helper functions:**
- Private functions (`_make_*`) for creating test data
- Reusable across tests in same file
- Use descriptive names indicating what's being created

**Location:**
- Defined at module level in test files
- Not shared across files (no `tests/conftest.py` observed)
- Each test file has its own helpers

**Factory pattern:**
- No external factory libraries
- Simple function-based factories
- `tmp_path` fixture used for file-based test data

## Coverage

**Requirements:** Not explicitly enforced
- No coverage target in `pyproject.toml`
- No coverage reporting configured

**View Coverage:**
```bash
pytest --cov=src tests/        # Not configured (inferred)
pytest --cov-report=html       # Not configured (inferred)
```

**Note:** Coverage tools not configured. Could be added via `pytest-cov`.

## Test Types

**Unit Tests:**
- **Scope:** Individual functions, methods, classes
- **Approach:** Isolated with mocks, no external dependencies
- **Examples:**
  - `format_file_size()` - pure function tests
  - `ConfigManager.load_config()` - file I/O mocked
  - `TransferTaskManager` - in-memory state tested

**Integration Tests:**
- **Scope:** Multiple components working together
- **Approach:** Some real dependencies (SQLite in-memory), others mocked
- **Examples:**
  - `test_resume_store_isolated_by_account` - uses real file I/O
  - `test_stream_download_*` - complex integration tests with mocked HTTP

**E2E Tests:**
- **Framework:** Not used
- PyQt6 GUI tests not present (no automated UI testing)
- Manual testing likely for UI components

## Common Patterns

**Async Testing:**
- Not applicable (codebase is synchronous, uses threading)
- Threading tested via `threading.Thread` in tests

**Error Testing:**
```python
def test_update_status_nonexistent(self, manager):
    manager.update_task_status(999, "下载中")  # 不应抛异常

def test_corrupted_file_returns_default(self, tmp_config_dir):
    config_module.CONFIG_FILE.write_text("not valid json{{{", encoding="utf-8")
    config = ConfigManager.load_config()
    assert config["userName"] == ""

def test_stream_download_raises_on_final_hash_mismatch(...):
    with pytest.raises(RuntimeError, match="整文件校验失败"):
        stream_download_from_url(...)
```

**Multi-threaded testing:**
```python
class TestThreadSafety:
    def test_concurrent_creates(self, manager):
        ids = []
        errors = []
        def create_many():
            try:
                for _ in range(100):
                    ids.append(manager.create_task("下载", "f.txt", 0))
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=create_many) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert len(set(ids)) == 500  # 所有 ID 唯一
```

**Parameterized tests:**
- Not using `@pytest.mark.parametrize`
- Individual test methods for each case (e.g., `test_bytes`, `test_kilobytes`)
- Could be refactored to use parameterization

**Test isolation:**
- `tmp_path` fixture ensures file system isolation
- `monkeypatch` for runtime modification
- `tmp_config_dir` fixture for config isolation
- Each test is independent (no shared state)

**Chinese in tests:**
- Chinese test names accepted: `test_中文描述`
- Chinese comments in test code
- Chinese assertion messages: `"不应抛异常"`

---

*Testing analysis: 2026-04-05*
