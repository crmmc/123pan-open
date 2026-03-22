#!/usr/bin/env bash
set -euo pipefail

project=$(realpath $(dirname $0)/..)

(
  cd $project

  uv run mypy --disable-error-code import-not-found "$@"
  )
