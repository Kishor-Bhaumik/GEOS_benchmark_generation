#!/bin/bash
# Launch one deck of GEOS_SWEEP_BENCH_AGENT_v1.md inside tmux (tmux does not inherit exported variables).
# Usage:  bash run_agent_deck.sh            -> starts tmux session "ag1" (Ctrl-b d to detach, tmux attach -t ag1)
#         bash run_agent_deck.sh --inside   -> run in the current terminal (already inside tmux)
#         bash run_agent_deck.sh --check    -> free startup checks only, no model call
# Edit the export lines (or set the variables before calling). The API key is read from NEMONIST_ENV_FILE
# (default ~/.config/nemonist/env, a file with one line: export NEMONIST_API_KEY=...), never written here.

REPO_DIR=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)

export GEOS_ROOT=${GEOS_ROOT:-$HOME/codes/GEOS}                       # GEOS source checkout (git repo)
export GEOS_BIN=${GEOS_BIN:-$GEOS_ROOT/build-conda-geos-release/bin/geosx}
export BENCH_DIR=${BENCH_DIR:-$REPO_DIR/bench}                        # results + harness (bench/_global/...)
export PROTOCOL_FILE=${PROTOCOL_FILE:-$REPO_DIR/GEOS_SWEEP_BENCH_AGENT_v1.md}
export AGENT_RO_BINDS=${AGENT_RO_BINDS:-}                             # extra read-only dirs GEOS needs at run time
                                                                      # (e.g. the thirdPartyLibs install), ':'-separated
export PARALLEL_JOBS=${PARALLEL_JOBS:-12}
export INPUT_XML=${INPUT_XML:-inputFiles/wellbore/DeviatedElasticWellbore_smoke.xml}   # relative to GEOS_ROOT
export RUN_TAG=${RUN_TAG:-run1}
export QUESTION_TYPE=${QUESTION_TYPE:-infer_then_predict}
export MODEL=${MODEL:-anthropic/claude-sonnet-5.5}
export REASONING_MAX_TOKENS=${REASONING_MAX_TOKENS:-16000}
export MAX_ATTEMPTS=${MAX_ATTEMPTS:-1}
CONDA_ENV=${CONDA_ENV:-geos}                                          # conda env with python3, numpy, h5py, bwrap
NEMONIST_ENV_FILE=${NEMONIST_ENV_FILE:-$HOME/.config/nemonist/env}
SESSION=${SESSION:-ag1}

if [ "$1" != "--inside" ] && [ "$1" != "--check" ]; then
  tmux new -s "$SESSION" "bash '$(readlink -f "$0")' --inside; echo; echo '[run finished: press Enter to close]'; read"
  exit
fi

source "$NEMONIST_ENV_FILE" || { echo "cannot read $NEMONIST_ENV_FILE"; exit 1; }
cd "$GEOS_ROOT" || exit 1
CONDA_BASE=$(conda info --base 2>/dev/null || echo "$HOME/anaconda3")
source "$CONDA_BASE/etc/profile.d/conda.sh"
conda activate "$CONDA_ENV"
export PYTHONDONTWRITEBYTECODE=1
mkdir -p "$BENCH_DIR/_global/controller_logs"
if [ "$1" = "--check" ]; then
  python "$BENCH_DIR/_global/scripts/qa/qa_run_deck.py" --check
else
  python "$BENCH_DIR/_global/scripts/qa/qa_run_deck.py" 2>&1 | tee "$BENCH_DIR/_global/controller_logs/qa_$(date +%Y%m%d_%H%M%S).log"
fi
