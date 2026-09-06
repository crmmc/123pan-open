# Testing Guidelines

> Project-specific testing conventions established while reaching 100% line coverage (task 09-06-fill-test-coverage, 2026-09-06). 1032 tests, all 25 modules under `src/app` at 100%, pylint 10.00, mypy clean.

---

## Command Entry Points

```bash
uv run python -m pytest tests/ -q                                          # full suite
uv run python -m pytest tests/ -q --cov=src/app --cov-report=term-missing  # coverage
bash script/mypy.sh                                                        # uv run mypy .
bash script/lint.sh                                                        # uv run pylint src tests
make test / make coverage / make lint / make mypy                          # same via Makefile
```

> **Warning (macOS local dev)**: bare `uv run pytest` may resolve to a Homebrew pytest outside the project venv, which cannot load venv-installed `pytest-cov` (`unrecognized arguments: --cov`). Always use `uv run python -m pytest`.
> **Warning**: `--cov=src/app/common/config.py` (slash path) fails with "Module was never imported". Use dotted module form (`--cov=src.app.common.config`) plus a test file that actually imports it, or the full `--cov=src/app`.

### Coverage Standards

- Metric: **line coverage** (`[tool.coverage.*]` in pyproject.toml, `source = ["src/app"]`).
- Standard: every module at 100%; residual uncovered lines are forbidden unless annotated `# pragma: no cover` with an inline justification (unreachable defensive branch only — verified static unreachability, e.g. `filename_utils.py:31,34`, `api.py:1771`). Never pragma Qt boilerplate that *could* be tested.
- Pragmas live in source, count is 3; re-audit any new one in review.

---

## Conventions (mirror existing tests)

1. **Naming**: `test_<subject>_<behavior>`; group related cases in `class TestXxx:`. Tests import `src.app.*` directly (repo-root run, no installed package).
2. **Table tests**: branch-dense pure functions use `@pytest.mark.parametrize` (with `ids` where helpful). See `tests/test_upload_conflict_dialog.py` decision matrix.
3. **No real I/O**: network (`requests`), keyring, database, dialogs — all mocked. grep-audit for `requests.get|urlopen|socket` outside mocks must stay empty.
4. **No sleep-based races**: real `time.sleep` in tests only for deliberate tiny concurrency windows (max 3 known, each with a justifying comment); prefer `threading.Event` chains or fake thread pools.
5. **Append-only**: never modify existing tests' assertions; only append or create files. Shared fixtures go in `tests/conftest.py`.

---

## Fixture Infrastructure (tests/conftest.py)

- `qapp` — session-scoped QApplication; conftest sets `QT_QPA_PLATFORM=offscreen` **before** PySide6 import. Compatible with legacy test files that build module-level `QApplication.instance() or QApplication([])` — instance() dedupes.
- `temp_db` — monkeypatches `database._get_db_path` → `tmp_path/"123pan-open.db"`, `Database.reset()` + teardown reset. Gotcha: `Database._init_defaults()` writes default config keys at creation, so assert "unchanged", never "missing".
- `fake_response` — factory for mocked `requests.Response` (status_code / .json() / .headers).

---

## DI / Seaming Order of Preference

1. Monkeypatch module-level seams (`database._get_db_path`, `api.Database.instance`, `api.time.sleep`, module-path Qt classes).
2. Replace instance attributes post-construction (`pan.session = MagicMock()`).
3. Only if 1–2 impossible: optional constructor injection with unchanged default behavior. Zero DI changes were needed for 100% coverage — seams suffice.

---

## Qt Widget Testing Patterns (hard-won, reuse these)

| Problem | Solution |
|---|---|
| `shiboken6.isValid` False on `__new__`-created widgets | autouse fixture: `monkeypatch.setattr("<module>.shiboken6.isValid", lambda _obj: True)` |
| Real QThreadPool racing tests | patch `QThreadPool.globalInstance()` with a fake pool (`run_next`/`run_pending` sync execution); make `fi` fixtures depend on it **before** constructor runs |
| `dialog.exec() != dialog.DialogCode.Accepted` always True on MagicMock | `_accept_dialog` helper: set `mock.return_value.DialogCode.Accepted = QDialog.DialogCode.Accepted` |
| QDragEnterEvent segfault (dangling QMimeData) | attach QMimeData to the event wrapper attribute to keep it alive past GC |
| FluentWindow segfaults under offscreen (exit 139) | replace `FluentWindow.__init__` with real `QWidget.__init__` + MagicMock nav bar + real QStackedWidget; `__init__` body still real. Needs explicit `QWidget.__init__(self)` (pylint C2801 disable with reason) |
| qfluentwidgets TableWidget selection | `setCurrentCell(r, c)` **first**, then `selectionModel().select()` per row (reverse order resets selection) |
| `setCellWidget` silently no-ops when `row >= rowCount` | `setRowCount()` first |
| qfluentwidgets SettingCard | no `getContent()`; read `contentLabel.text()`. `ComboBox.setCurrentIndex` explicitly emits `currentIndexChanged` — usable as trigger |
| SettingCardGroup card assertions | no `itemCount`; use `card.parent() is group` |
| Dialog result value to caller | set `dialog._result` / module-level return value, then emit `accepted` |

---

## Common Mistakes (hit in this task, avoid repeating)

- **`monkeypatch.setattr(sys, "getwindowsversion", ...)`** raises AttributeError on non-Windows — pass `raising=False`.
- **Patching `os.name`** re-dispatches `pathlib.Path.__new__` to WindowsPath and breaks `Path()` construction; pin `module.Path = PosixPath` in the same test.
- **`set_many_config` JSON-serializes values**: a MagicMock `pan.user_name` raises ValueError inside the try, silently short-circuiting credential saving; give mock pans real string attributes.
- **`download_parts` FK → `download_tasks.resume_id`**: direct `_download_part` success-path tests must `save_download_task` first or `record_download_part` raises IntegrityError.
- **Constructor-triggered async work**: `expandItem` during `__init__` submits to the (real) global thread pool — sync-drain via fake pool after construction or tests race.
- **`self.close()` inside `__init__`** really fires closeEvent → Database singleton reset mid-test; re-fetch `Database.instance()` or reset mocks.
- **Top-level test imports vs legacy in-function imports**: duplicate import triggers pylint W0404; mirror the legacy in-function style when extending old files.
- **mypy on tests** only exempts `attr-defined`: annotate closure-captured lists (`statuses: list[str] = []`), `assert x is not None` before Optional attribute access, annotate class attrs holding mocks.
