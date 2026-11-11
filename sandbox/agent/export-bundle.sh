#!/usr/bin/env bash
# Runs in a --network none container after the agent exits. A bundle carries objects
# and refs only, never hooks or config, so the host can fetch it safely.
set -euo pipefail

branch=$(cat /work/in/branch)
git -C /work/repo bundle create /work/out/work.bundle "refs/heads/$branch" ^origin/main
