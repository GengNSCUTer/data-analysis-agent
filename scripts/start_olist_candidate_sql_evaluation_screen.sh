#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec bash "${ROOT}/scripts/post_training/launchers/start_olist_candidate_sql_evaluation_screen.sh" "$@"
