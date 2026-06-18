#!/bin/bash
# Easy one-off / manual watcher runner for Railway Shell or local
# Usage examples:
#   ./run_watcher.sh 0xYourContract --blast --supply 10000
#   ./run_watcher.sh 0xYourContract --mode reveal
#
# Or with env:
#   CONTRACT=0x... ./run_watcher.sh --blast

set -e

# If first arg is not starting with --, treat as contract
if [[ $# -gt 0 && ! "$1" =~ ^-- ]]; then
  CONTRACT="$1"
  shift
fi

# Fallback to env var
if [ -z "$CONTRACT" ]; then
  CONTRACT="${CONTRACT:-${WATCHER_CONTRACT}}"
fi

if [ -z "$CONTRACT" ]; then
  echo "? No contract provided."
  echo "Usage: ./run_watcher.sh 0xContract [options...]"
  echo "   or:  CONTRACT=0x... ./run_watcher.sh [options...]"
  exit 1
fi

echo "?? Running watcher for contract: $CONTRACT"
echo "   (All other args passed through)"
python watcher.py "$CONTRACT" "$@"
