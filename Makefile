SHELL := /bin/bash
.DEFAULT_GOAL := all

SEVEN_Z := $(shell command -v 7zz 2>/dev/null || command -v 7z 2>/dev/null || command -v 7za 2>/dev/null)
ARCHIVE := dist/123pan-open-source.7z
PACKAGE_INPUTS := \
	LICENSE \
	Makefile \
	README.md \
	pyproject.toml \
	script/build.sh \
	src \
	uv.lock
PACKAGE_EXCLUDES := \
	-xr!__pycache__ \
	-xr!*.pyc

.PHONY: all run

run:
	uv run src/123pan-open.py
all: $(ARCHIVE)

$(ARCHIVE): $(PACKAGE_INPUTS) FORCE
	@if [[ -z "$(SEVEN_Z)" ]]; then \
		echo "7z command not found (tried: 7zz, 7z, 7za)" >&2; \
		exit 1; \
	fi
	mkdir -p dist
	rm -f "$(ARCHIVE)"
	"$(SEVEN_Z)" a -t7z "$(ARCHIVE)" $(PACKAGE_INPUTS) $(PACKAGE_EXCLUDES)

FORCE:

# ---- Tests / quality gates ----
# NOTE: coverage 命令必须用 `uv run python -m pytest`（本机 `uv run pytest`
# 会解析到 Homebrew 的 pytest，加载不到 venv 的 pytest-cov）。
.PHONY: test coverage lint mypy

test:
	uv run python -m pytest tests/ -q

coverage:
	uv run python -m pytest tests/ -q --cov=src/app --cov-report=term-missing

lint:
	bash script/lint.sh

mypy:
	bash script/mypy.sh
