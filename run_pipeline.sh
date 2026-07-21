#!/usr/bin/env bash
set -euo pipefail

source ~/.venv/bin/activate

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

# non-interactive by default; only --purge without --yes would block on a
# confirmation prompt, so purge is always run with --yes here
run_stage() {
    echo "--------------------------------------------------"
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] $*"
    echo "--------------------------------------------------"
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
    run_stage python -m core.vectorize --limit "$LIMIT"
else
    run_stage python -m core.vectorize
fi

run_stage python -m core.report

if [[ "$PURGE" -eq 1 ]]; then
    run_stage python -m core.purge --yes
fi
