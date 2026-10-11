#!/usr/bin/env bash
# Week 2 pipeline, end to end, in the order the research rules require:
#   in-sample raw data → book grids → features → liquidity buckets (frozen BEFORE any IC)
#   → data-quality review → univariate ICs (logged in research/trials.csv) → EDA notebook
#   → holdout raw data → book grids → features (built, never evaluated before Week 7).
#
# The Tardis free tier has a daily transfer quota; the loader sleeps until it resets.
# Every step skips work that is already done, so the script is safe to rerun.
#
#   nohup scripts/week2_pipeline.sh > data/logs/week2_pipeline.log 2>&1 &
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
IN_SAMPLE=$($PY -c "from common.universe import load_universe; print(' '.join(load_universe().in_sample))")
HOLDOUT=$($PY -c "from common.universe import load_universe; print(' '.join(load_universe().holdout))")
step() { echo; echo "=== $(date '+%Y-%m-%d %H:%M:%S') $*"; }

step "download in-sample raw data"
$PY -m ingest.tardis --dates $IN_SAMPLE --wait-for-quota
# --force: rebuild everything from the code as it is now, so every derived file in the
# research sample carries the same code_git_sha (commit before this step runs).
step "book grids (in-sample)"
$PY -m book.build --dates $IN_SAMPLE --workers 4 --force
step "features (in-sample)"
$PY -m features.build --dates $IN_SAMPLE --workers 4 --force

step "liquidity buckets (frozen before any IC)"
if [ ! -f configs/liquidity_buckets.yaml ]; then
  $PY -m research.studies.liquidity_buckets
else
  echo "configs/liquidity_buckets.yaml exists; not recomputing (frozen)"
fi
step "data-quality review"
$PY -m research.studies.data_quality
step "univariate ICs"
if ! grep -q "research/studies/univariate_ic.py" research/trials.csv; then
  $PY -m research.studies.univariate_ic
else
  echo "univariate IC trials already logged; not rerunning (trials are append-only)"
fi
step "EDA notebook"
# The kernel must be the project venv: put it first on PATH and prefer env kernelspecs.
PATH="$PWD/.venv/bin:$PATH" JUPYTER_PREFER_ENV_PATH=1 $PY -m jupyter nbconvert --to notebook --execute --inplace \
  --ExecutePreprocessor.timeout=3600 research/notebooks/01_eda.ipynb

step "live capture report (Week 1 gate), after the 26 h collector run ends"
while pgrep -f "ingest.coinbase_ws" > /dev/null; do sleep 300; done
$PY -m ingest.capture_report > data/logs/capture_report.txt 2>&1 || true
tail -1 data/logs/capture_report.txt

step "download holdout raw data (not evaluated)"
$PY -m ingest.tardis --dates $HOLDOUT --wait-for-quota
step "book grids + features (holdout; built, not evaluated)"
$PY -m book.build --dates $HOLDOUT --workers 4
$PY -m features.build --dates $HOLDOUT --workers 4
step "done"
