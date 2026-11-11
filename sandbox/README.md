# Local factory sandbox

Runs the factory worker on this machine instead of GitHub Actions. The agent works in a
container whose only way out is a logging allowlist proxy, and every job is visible at
three layers in one Grafana dashboard.

**This is a demonstration, not a hardened deployment.** The agent shares a kernel with
the workstation, and Tetragon watches it but does not block anything.

## How a job runs

```
host (Ryan's gh login)                       sandbox (no GitHub, no key)
──────────────────────                       ───────────────────────────
base.bundle from state/origin.git ──cp──►    clone, work, commit (claude -p)
                                             export work.bundle (no network)
fresh clone ◄── fetch bundle ◄──────cp──
squash onto main (host-written message, Closes #N)
refuse anything outside app/
gates on that exact commit:
  hygiene + critical.py (from main)
  fmt, clippy, test ──────────────cp──►     fresh build container, grader registry
push trial branch, post statuses
fast-forward main if all green, reply on the issue
```

There are no PRs. Main's required checks (`build`, `hygiene`, `critical-classifier`) are
satisfied by the statuses the host posts on the commit before it moves main. Critical
signals are reported in the `critical-classifier` status and on the issue but do not
block (Ryan, 2026-11-11). A hygiene violation, a failed build, or main moving during the
trial does. The host never force-pushes.

- **Network.** `factory-internal` has no route out, and no external DNS (E1). The proxy
  (`proxy/allowlist.py`) allows only `proxy/allowlist.txt`, logs every flow to
  `state/flows/flows.jsonl`, and sets the real `x-api-key` for `api.anthropic.com`. The
  container only ever holds a placeholder key.
- **Git.** The host never runs git in a repo the agent could write to: work comes out
  as a bundle, which carries objects and refs but never hooks or config (E12,
  `host/test_factory.py`).
- **Telemetry.** Claude Code sends OTLP to the proxy's reverse listener on `:4318`,
  which forwards to lgtm. lgtm is not on the agent's network.
- **OS events.** Tetragon runs on the host and exports container events only, never host
  processes, to `state/tetragon/`.
- **Disabled tools.** `WebSearch` runs on Anthropic's servers, past the proxy, and could
  find the public holdout repo, so it is disabled along with `WebFetch`.

## Setup

```bash
cp sandbox/.env.example sandbox/.env    # add a dedicated API key with a spend limit
./sandbox/setup.sh                       # CA, agent image, proxy + lgtm
sudo ./sandbox/setup.sh tetragon         # in a real terminal; sudo needs a TTY
```

Grafana runs on <http://localhost:3300>, because port 3000 is often taken by VS Code.

## Run a job

```bash
cd sandbox/host
uv run --project ../../factory python factory.py worker 4 --no-push
```

The JSON summary includes a `dashboard` link with the job and container filled in.
`--no-push` still runs every gate and reports `green (not pushed)` or the failing gates.
Without it, the host pushes `claude/issue-N-t<K>`, posts the statuses, and lands it on main.

Tests:

```bash
uv run --project ../../factory --with pytest pytest -q
```

## Findings so far (Stage 1, 2026-11-11)

| Experiment | Result |
|---|---|
| E1 DNS | External names do not resolve from the sandbox. |
| E2 Claude via proxy | Works with a placeholder key. Startup calls Datadog and event logging, which `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` removes while OTel keeps flowing. |
| E3/E7 cargo via proxy | Cold build, fmt, clippy and test of T-001 took 19 s (Actions CI took 51 s). |
| E4 friction.py | Reads the joined stream-json transcript: 0 blocked calls, and the live `/health` check now completes. |
| E5/E11 OTel | Events reach Loki, traces reach Tempo (use a TraceQL `q`), and `TRACEPARENT` reaches Bash. |
| E6 Tetragon | Works on kernel 7.0 with rootless Docker; events carry a 31-character container ID. Other containers on the machine show up too; the dashboard filters by container. |
| E9 egress | Blocked hosts get a 403 and are logged; direct IPs fail; lgtm and Grafana are unreachable. |
| E12 bundle | Hooks and `core.fsmonitor` planted in the agent's repo never run on the host. |
