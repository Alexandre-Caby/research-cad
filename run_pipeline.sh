#!/usr/bin/env bash
set -euo pipefail

source ~/.venv/bin/activate

export HF_HOME="${HF_HOME:-$HOME/.cache/huggingface}"

LIMIT=""
PURGE=0
YES=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --limit)
            LIMIT="$2"
            shift 2
            ;;
        --purge)
            PURGE=1
            shift
            ;;
        --yes)
            YES=1
            shift
            ;;
        *)
            echo "Unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

banner() {
    echo "--------------------------------------------------"
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $1"
    echo "--------------------------------------------------"
}

run_stage() {
    banner "$*"
    "$@"
}

run_stage python -m core.sources.github --org adafruit --search pcb
run_stage python -m core.sources.github --org sparkfun --search hardware --path-filter hardware

if [[ -n "$LIMIT" ]]; then
    run_stage python -m core.sources.huggingface --limit "$LIMIT"
else
    run_stage python -m core.sources.huggingface
fi

run_stage python -m core.sources.oshwlab

if [[ -n "$LIMIT" ]]; then
    run_stage python -m core.clean --limit "$LIMIT"
else
    run_stage python -m core.clean
fi

if [[ -n "$LIMIT" ]]; then
    run_stage python -m core.sources.mouser --limit "$LIMIT"
else
    run_stage python -m core.sources.mouser
fi

if [[ -n "$LIMIT" ]]; then
    run_stage python -m core.vectorize --limit "$LIMIT"
else
    run_stage python -m core.vectorize
fi

run_stage python -m core.report

if [[ "$PURGE" -eq 1 ]]; then
    purge_args=()
    [[ "$YES" -eq 1 ]] && purge_args+=(--yes)
    run_stage python -m core.purge "${purge_args[@]}"
fi

banner "pipeline completed, cleaning up run artifacts"
find . -type d -name "__pycache__" -exec rm -rf {} +
find . -type f -name "*.pyc" -delete

echo "--------------------------------------------------"
echo "Pipeline completed successfully."
echo "Metrics:"
python -c "
from core import config
from core import registry as R
from core.report import run_report
conn = R.connect(config.DB_PATH)
report = run_report(conn)
print(f'- Total projects: {report[\"total\"]}')
print(f'- By status:      {report[\"by_status\"]}')
print(f'- By source:      {report[\"by_source\"]}')
print(f'- Files by kind:  {report[\"files_by_kind\"]}')
"
echo "--------------------------------------------------"