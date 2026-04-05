# Codebase Concerns

**Analysis Date:** 2026-04-05

## Tech Debt

### Large Monolithic Files
**File Interface (1,396 lines):**
- Files: `src/app/view/file_interface.py`
- Issue: Single file handles file browsing, tree management, upload/download preparation, file operations (create/delete/rename), drag-and-drop, context menus, and storage info calculation
- Impact: Difficult to maintain, high cognitive load, test coverage challenges
- Fix approach: Extract into separate modules:
  - `file_browser.py` - Core file browsing logic
  - `file_operations.py` - Delete/rename/create operations
  - `upload_preparer.py` - Upload preparation logic
  - `storage_calculator.py` - Storage calculation

**Transfer Interface (996 lines):**
- Files: `src/app/view/transfer_interface.py`
- Issue: Manages both upload and download tasks, table updates, progress tracking, speed calculation, and concurrency control
- Impact: Complex state management, difficult to test upload/download flows independently
- Fix approach: Split into:
  - `upload_manager.py` - Upload task management
  - `download_manager.py` - Download task management
  - `transfer_table.py` - Table UI updates
  - `task_concurrency.py` - Concurrency control logic

**API Module (1,016 lines):**
- Files: `src/app/common/api.py`
- Issue: Single `Pan123` class handles all API operations (login, file listing, upload, download, delete, rename, recycle bin)
- Impact: Difficult to navigate, violates single responsibility principle
- Fix approach: Group by functionality:
  - `api_auth.py` - Login/auth operations
  - `api_files.py` - File operations
  - `api_upload.py` - Upload logic
  - `api_download.py` - Download logic

### Global State Management
**Database Singleton Pattern:**
- Files: `src/app/common/database.py`
- Issue: Global `_db_instance` with module-level locks, called from multiple threads without proper transaction management
- Impact: Potential race conditions, difficult to test, hidden dependencies
- Fix approach: Pass database instance as parameter to functions that need it, or use dependency injection

**Pan API Client State:**
- Files: `src/app/common/api.py` (Pan123 class)
- Issue: Mutable instance state (`parent_file_id`, `file_page`, `list`, etc.) modified during operations
- Impact: State pollution when same instance reused, requires manual state snapshot/restore (see `download_metadata.py`)
- Fix approach: Make stateless or use context managers for state changes

## Known Bugs

### Empty Return Values
**Multiple Locations:**
- Files: `src/app/common/download_resume.py` (lines 96, 145, 197), `src/app/common/database.py` (lines 206, 315), `src/app/view/file_interface.py` (lines 256, 415, 425, 504), `src/app/view/transfer_interface.py` (line 174)
- Issue: Functions return `None`, `[]`, or `{}` on error without raising exceptions
- Impact: Callers must check for empty values, errors are silently swallowed
- Trigger: API failures, database errors, missing data
- Workaround: None currently - errors propagate as empty data
- Fix approach: Raise specific exceptions instead of returning empty values

### Metadata Version Incompatibility
**Download Task Metadata:**
- Files: `src/app/common/download_metadata.py`, `src/app/view/transfer_interface.py` (lines 453-455, 614, 838)
- Issue: Old download tasks with incompatible metadata versions fail permanently with `LEGACY_RESUME_TASK_ERROR`
- Symptoms: Failed downloads cannot be retried, user must delete and recreate task
- Trigger: Metadata version changes (currently v2)
- Workaround: Manual task deletion and recreation
- Fix approach: Implement migration logic for old metadata versions

## Security Considerations

### SQLite Thread Safety
**Database Access from Multiple Threads:**
- Risk: `check_same_thread=False` in `src/app/common/database.py` (line 28) allows connections to be shared across threads
- Files: `src/app/common/database.py`
- Impact: Potential data corruption if concurrent writes occur without proper locking
- Current mitigation: Thread-level write locks (`_write_lock`)
- Recommendations:
  - Use WAL mode (already enabled) for better concurrency
  - Consider connection pooling with thread-local connections
  - Add comprehensive concurrency tests

### Hardcoded Timeouts
**Network Request Timeouts:**
- Risk: Some timeouts are hardcoded (30s, 60s, 10s) without configurability
- Files: `src/app/common/api.py`, `src/app/common/download_resume.py`
- Impact: May cause premature failures on slow networks or excessive waiting on dead connections
- Current mitigation: None
- Recommendations: Make timeouts configurable via settings

### Temporary File Cleanup
**Download Temporary Files:**
- Risk: Temporary files in `CONFIG_DIR/tmp/` may not be cleaned up if process crashes
- Files: `src/app/common/download_resume.py` (cleanup_temp_dir function)
- Impact: Disk space leaks over time
- Current mitigation: Cleanup on successful completion, explicit delete on cancel
- Recommendations: Add startup cleanup routine for orphaned temp directories

## Performance Bottlenecks

### Synchronous File Operations
**File MD5 Calculation:**
- Problem: `_compute_md5()` in `src/app/common/download_resume.py` (lines 34-42) reads entire file synchronously
- Files: `src/app/common/download_resume.py`
- Cause: Single-threaded MD5 calculation for part verification
- Impact: Blocks worker thread during verification, especially for large parts (5MB)
- Improvement path: Use background thread or async I/O for MD5 calculation

### Tree Widget Performance
**Tree Loading and Expansion:**
- Problem: Tree widget loads all child items synchronously on expansion
- Files: `src/app/view/file_interface.py` (lines 301-332)
- Cause: Direct API call in UI thread when tree item expanded
- Impact: UI freezes during directory listing
- Improvement path: Already using `LoadListTask` for file list, apply same pattern to tree loading

### Storage Calculation
**Recursive Storage Calculation:**
- Problem: `calculate_total_storage()` recursively walks all files synchronously
- Files: `src/app/view/file_interface.py` (lines 1335-1368)
- Cause: Single-threaded iteration through all files in account
- Impact: Can take significant time for large accounts
- Improvement path: Already using background task (StorageTask), but could cache results

## Fragile Areas

### Download Resume Logic
**Multi-part Download Coordination:**
- Files: `src/app/common/download_resume.py` (577 lines of complex logic)
- Why fragile: Complex state machine with multiple failure modes, rate limiting, worker coordination, part verification, and merging
- Safe modification:
  - Add comprehensive unit tests for each state transition
  - Test failure scenarios (network errors, rate limits, cancellations)
  - Use integration tests with mock servers
- Test coverage: `tests/test_download_resume.py` (447 lines) - good coverage but needs more failure scenario tests

### Upload Session Management
**S3 Upload Resume:**
- Files: `src/app/common/api.py` (upload_file_stream method, ~400 lines)
- Why fragile: Complex multipart upload logic with session state, part tracking, retry logic, and error recovery
- Safe modification:
  - Test with various failure scenarios (network drops, part failures)
  - Verify session state persistence across restarts
  - Test rate limiting behavior
- Test coverage: Limited - needs comprehensive upload failure scenario tests

### File Deletion with Index Lookup
**Delete File by Index:**
- Files: `src/app/view/file_interface.py` (lines 1011-1023)
- Why fragile: Relies on list index matching file ID, requires fallback reload if index not found
- Safe modification:
  - Use file ID directly instead of index
  - Add defensive checks for list synchronization
  - Test with concurrent modifications
- Test coverage: Basic tests in `tests/test_file_interface.py` (51 lines) - insufficient

## Scaling Limits

### Concurrent Transfer Limits
**Download Concurrency:**
- Current capacity: Max 5 concurrent downloads (configurable, 1-5 range)
- Limit: UI performance degrades with too many concurrent transfers, no queue prioritization
- Scaling path: Implement priority queue for downloads, separate UI thread from transfer threads

### Thread Pool Usage
**QThreadPool for Background Tasks:**
- Current capacity: Uses global QThreadPool without limits
- Limit: May spawn unlimited background tasks (file list loading, folder creation, etc.)
- Scaling path: Set max thread count on QThreadPool, implement task queue with priorities

### Database Write Locking
**Single Write Lock:**
- Current capacity: One global `_write_lock` for all database writes
- Limit: All writes serialized, potential bottleneck with many concurrent transfers
- Scaling path: Use separate connections per thread with WAL mode, reduce write frequency

## Dependencies at Risk

### PyQt6
- Risk: Breaking changes in PyQt6 updates could break UI
- Impact: Entire UI layer depends on PyQt6 and qfluentwidgets
- Migration plan: Pin PyQt6 version, test thoroughly before upgrades, consider abstracting UI framework

### Requests Library
- Risk: No active alternative considered for HTTP client
- Impact: Core API communication depends on requests
- Migration plan: Standardize on requests, consider httpx for async support if needed

## Missing Critical Features

### Upload Queue Management
- Problem: No way to reorder or prioritize upload tasks
- Blocks: Users cannot control which files upload first in batch uploads
- Impact: Poor UX for large batch uploads

### Download Retry with Backoff
- Problem: Download failures require manual retry
- Blocks: Automatic recovery from transient network failures
- Impact: Poor UX for unstable networks
- Note: Upload has retry logic (lines 851-914 in api.py), download should follow same pattern

### Transfer Progress Persistence
- Problem: Transfer progress only saved to database on explicit updates
- Blocks: Recovery from crashes without losing all progress
- Impact: Data waste on application crashes

## Test Coverage Gaps

### UI Integration Tests
- What's not tested: Full user workflows (login → browse → download → verify)
- Files: `src/app/view/*.py`
- Risk: UI state management bugs, signal/slot connection issues
- Priority: High
- Recommendation: Add pytest-qt for UI testing

### Concurrent Operation Tests
- What's not tested: Multiple simultaneous uploads/downloads, database contention
- Files: `src/app/common/download_resume.py`, `src/app/common/api.py`
- Risk: Race conditions, deadlocks, data corruption
- Priority: High
- Recommendation: Add multi-threaded test scenarios

### Error Recovery Tests
- What's not tested: Network failures, API errors, database errors during operations
- Files: All API and transfer files
- Risk: Poor error handling, resource leaks, inconsistent state
- Priority: Medium
- Recommendation: Add chaos engineering style tests with fault injection

### API Mock Tests
- What's not tested: API contract validation, error response handling
- Files: `src/app/common/api.py`
- Risk: Breaking when 123pan API changes
- Priority: Medium
- Recommendation: Add VCR or similar tool for API recording/playback

---

*Concerns audit: 2026-04-05*
