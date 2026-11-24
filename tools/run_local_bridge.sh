#!/bin/sh
# Short-process JSON bridge; independent of the host's cwd and PATH.
set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

if [ "$#" -gt 1 ]; then
    printf 'Usage: %s [absolute-data-directory]\n' "$0" >&2
    exit 2
fi
default_data_dir="$project_root/.work-rag-data"
if [ ! -e "$default_data_dir" ] && [ -d "$project_root/.it-rag-data" ]; then
    default_data_dir="$project_root/.it-rag-data"
fi
data_dir=${1:-${WORK_RAG_DATA_DIR:-${IT_RAG_DATA_DIR:-"$default_data_dir"}}}
case "$data_dir" in
    /*) ;;
    *) printf 'Bridge data directory must be an absolute path: %s\n' "$data_dir" >&2; exit 2 ;;
esac

python_override=${WORK_RAG_PYTHON:-${IT_RAG_PYTHON:-}}
if [ -n "$python_override" ]; then
    python=$python_override
elif [ -x "$project_root/.dev-data/run-python" ]; then
    python="$project_root/.dev-data/run-python"
else
    python="$project_root/.venv/bin/python"
fi
case "$python" in
    /*) ;;
    *) printf 'WORK_RAG_PYTHON must be an absolute executable path\n' >&2; exit 2 ;;
esac
if [ ! -x "$python" ]; then
    printf 'Python runtime unavailable: %s; install the project in .venv or set WORK_RAG_PYTHON.\n' "$python" >&2
    exit 1
fi

cd -- "$project_root"
exec "$python" -m work_rag.local_bridge --data-dir "$data_dir"
