#!/usr/bin/env bash
# Runs inside the sandbox. Inputs in /work/in (base.bundle, prompt.txt, branch),
# outputs in /work/out. The host never runs git in /work/repo.
set -euo pipefail

branch=$(cat /work/in/branch)
mkdir -p /work/out
git clone --quiet /work/in/base.bundle /work/repo
cd /work/repo
git checkout --quiet -b "$branch"
git config user.name "factory-worker"
git config user.email "factory-worker@localhost"

# The exit status is the agent's; the host decides what a missing commit means.
# WebSearch runs on Anthropic's servers, so the proxy allowlist can't constrain it,
# and it could find the public holdout repo. WebFetch goes nowhere useful anyway.
claude -p "$(cat /work/in/prompt.txt)" \
  --model "${FACTORY_MODEL:-claude-sonnet-5-5}" \
  --max-turns "${FACTORY_MAX_TURNS:-60}" \
  --dangerously-skip-permissions \
  --disallowedTools "WebSearch,WebFetch" \
  --output-format stream-json --verbose \
  > /work/out/transcript.jsonl
