# Technology Stack

**Analysis Date:** 2026-04-05

## Languages

**Primary:**
- Python 3.12+ - Desktop application and API client

**Secondary:**
- Bash - Build and linting scripts
- QSS - Qt stylesheet theming

## Runtime

**Environment:**
- Python 3.12+ (required)
- UV package manager

**Package Manager:**
- UV - Fast Python package manager
- Lockfile: `uv.lock` (present)

## Frameworks

**Core:**
- PyQt6 6.10.2+ - Qt6 Python bindings for GUI
- PyQt6-Fluent-Widgets 1.11.1+ [full] - Fluent Design UI components

**Testing:**
- pytest 8.0.0+ - Testing framework

**Build/Dev:**
- Nuitka 4.0.5+ - Python to C compiler for standalone executables
- pylint 4.0.5+ - Code linting
- mypy 1.19.1+ - Static type checking

## Key Dependencies

**Critical:**
- requests 2.32.5+ - HTTP client for 123pan API communication
- zstandard 0.25.0+ - Zstandard compression for data transfer

**Infrastructure:**
- sqlite3 (stdlib) - Local database for configuration and task persistence
- urllib3 (via requests) - HTTP connection pooling and retry logic

## Configuration

**Environment:**
- No environment variables used
- Configuration stored in SQLite database (`~/.config/123pan/123pan.db`)
- User settings managed through Settings UI

**Build:**
- `pyproject.toml` - Project metadata and dependencies
- `script/build.sh` - Nuitka build script
- `script/lint.sh` - Pylint checking
- `script/mypy.sh` - Type checking

## Platform Requirements

**Development:**
- Python 3.12+
- UV package manager
- PyQt6 development packages (Linux: libxcb-* libraries)

**Production:**
- Windows 10+ or Linux (Ubuntu/Debian)
- Standalone executables built with Nuitka
- No Python runtime required for end users

---

*Stack analysis: 2026-04-05*
