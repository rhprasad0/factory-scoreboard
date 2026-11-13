"""Local factory host: runs worker jobs in the sandbox and does the GitHub side itself.

Usage:
  factory.py poll                    claim new /build comments and run them (systemd timer)
  factory.py worker ISSUE [--no-push]

Trust split: the host holds the GitHub login and never runs git in a repository the
agent could write to. Work leaves the sandbox only as a git bundle, which carries
objects and refs, never hooks or config. See sandbox/README.md.

There are no PRs. The host squashes the work onto main and refuses it outright if
gitleaks finds a secret. Otherwise it grades that exact commit (hygiene, critical,
fmt/clippy/test in a fresh container, then the task's holdout oracle in another), pushes
the trial branch, posts the results as commit statuses, and fast-forwards main only if
every gate is green.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = "rhprasad0/factory-scoreboard"
OWNER = REPO.split("/")[0]
REPO_URL = f"https://github.com/{REPO}.git"
SANDBOX = Path(__file__).resolve().parent.parent
CHECKOUT = SANDBOX.parent
STATE = SANDBOX / "state"
ORIGIN = STATE / "origin.git"  # host-only bare clone; the agent never touches it
HOLDOUT = Path(os.environ.get("FACTORY_HOLDOUT", Path.home() / "Documents/factory-scoreboard-holdout"))
IMAGE = os.environ.get("FACTORY_IMAGE", "factory-agent:dev")
ORACLE_IMAGE = os.environ.get("FACTORY_ORACLE_IMAGE", "factory-oracle:dev")
MARKER = "<!-- factory-host -->"
TIMEOUT_SECONDS = 30 * 60
BUILD_TIMEOUT_SECONDS = 20 * 60
GATES = ("build", "hygiene", "critical-classifier", "oracle")  # main's required status checks
GITLEAKS_CONFIG = SANDBOX / "host/gitleaks.toml"
ASK = "NEEDS CLARIFICATION"  # first line of a final message that asks instead of building
NEEDS_ANSWER = "factory:needs-answer"  # while set, Ryan's next comment on the issue is his answer

PROMPT = """You are a factory worker for {repo}, working on issue #{issue}.

Read CLAUDE.md first and follow it.

You are on branch {branch}, created from main. Commit your work there. Do not push: the
host grades your commits and lands them on main if every gate passes.

The issue body below is your only spec.

<spec>
{body}
</spec>
"""


def prompt(issue: int, branch: str, body: str, clarifications: str = "") -> str:
    text = PROMPT.format(repo=REPO, issue=issue, branch=branch, body=body)
    if clarifications:
        text += ("\nThe repo owner answered your questions on the issue. Treat this as part of the spec.\n\n"
                 f"<clarifications>\n{clarifications}\n</clarifications>\n")  # fmt: skip
    return text


def asked(result: dict) -> bool:
    return (result.get("result") or "").strip().split("\n", 1)[0].strip() == ASK


def run(*args: str | Path, cwd: Path | None = None, check: bool = True, **kw) -> subprocess.CompletedProcess:
    return subprocess.run([str(a) for a in args], cwd=cwd, check=check, text=True, capture_output=True, **kw)


def spec_sha(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def check_spec(issue: int, body: str, holdout: Path = HOLDOUT) -> None:
    """Refuse to start unless the issue body is the frozen spec the oracle was written for."""
    manifest = holdout / "tasks" / str(issue) / "task.json"
    if not manifest.exists():
        raise SystemExit(f"no holdout task for issue #{issue}")
    expected = json.loads(manifest.read_text())["spec_sha"]
    if spec_sha(body) != expected:
        raise SystemExit(f"issue #{issue} body does not match the frozen spec_sha")


def neutralize_mentions(text: str) -> str:
    """Agent text is posted under Ryan's account, so it must not ping anyone."""
    return re.sub(r"@(?=[A-Za-z0-9])", "@‍", text)


def next_trial(issue: int) -> int:
    heads = run("git", "ls-remote", "--heads", REPO_URL, f"claude/issue-{issue}-t*").stdout
    taken = [int(m) for m in re.findall(rf"refs/heads/claude/issue-{issue}-t(\d+)$", heads, re.M)]
    local = [int(p.name.split("-t")[1].split("-")[0]) for p in (STATE / "jobs").glob(f"{issue}-t*")]
    return max(taken + local + [1]) + 1  # trial 1 was claude/issue-N on Actions


def refresh_origin() -> str:
    if not ORIGIN.exists():
        run("git", "clone", "--quiet", "--bare", REPO_URL, ORIGIN)
    run("git", "-C", ORIGIN, "fetch", "--quiet", "origin", "+refs/heads/main:refs/heads/main")
    return run("git", "-C", ORIGIN, "rev-parse", "main").stdout.strip()


PLACEHOLDER_KEY = "placeholder-the-proxy-sets-the-real-key"


def agent_env(job_id: str, issue: int, key: str = PLACEHOLDER_KEY) -> list[str]:
    env = {
        "ANTHROPIC_API_KEY": key,  # the proxy replaces it; evals pass a canary
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",  # E5: OTel still flows, noise stops
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://proxy:4318",
        "OTEL_METRIC_EXPORT_INTERVAL": "10000",
        "OTEL_LOG_USER_PROMPTS": "1",
        "OTEL_LOG_TOOL_DETAILS": "1",
        "OTEL_LOG_TOOL_CONTENT": "1",
        "OTEL_RESOURCE_ATTRIBUTES": f"factory.job_id={job_id},factory.issue={issue}",
    }
    return [arg for key, value in env.items() for arg in ("-e", f"{key}={value}")]


def sandbox_args(job_id: str, volume: str, registry: str) -> list[str]:
    return [
        "--name", f"factory-job-{job_id}",
        "--label", f"factory.job={job_id}",
        "--network", "factory-internal",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", "1024",
        "--memory", "8g",
        "--cpus", "8",
        "-v", f"{volume}:/work",
        "-v", f"{registry}:/home/agent/.cargo/registry",
    ]  # fmt: skip


def run_sandbox(job_id: str, issue: int, job_dir: Path, key: str = PLACEHOLDER_KEY) -> tuple[int, float]:
    """Run the agent; return (exit code, seconds). Outputs land in job_dir."""
    volume = f"factory-job-{job_id}"
    run("docker", "volume", "create", volume)
    name = f"factory-job-{job_id}"
    run("docker", "create", *sandbox_args(job_id, volume, "factory-reg-worker"), *agent_env(job_id, issue, key),
        IMAGE, "run-agent.sh")  # fmt: skip
    run("docker", "cp", job_dir / "in", f"{name}:/work/in")
    # Tetragon tags events with this ID (first 31 chars), so the dashboard can find the job.
    (job_dir / "container_id").write_text(run("docker", "inspect", "-f", "{{.Id}}", name).stdout.strip())
    (STATE / "current-job").write_text(job_id)
    started = time.monotonic()
    try:
        code = run("docker", "start", "-a", name, check=False, timeout=TIMEOUT_SECONDS).returncode
    except subprocess.TimeoutExpired:
        run("docker", "kill", name, check=False)
        code = 124
    finally:
        (STATE / "current-job").write_text("none")
    seconds = time.monotonic() - started
    run("docker", "cp", f"{name}:/work/out/.", job_dir, check=False)
    run("docker", "rm", name)

    # Export in a container with no network, then copy the bundle out.
    export = f"{name}-export"
    run("docker", "create", "--name", export, "--network", "none", "--cap-drop", "ALL",
        "-v", f"{volume}:/work", IMAGE, "export-bundle.sh")  # fmt: skip
    exported = run("docker", "start", "-a", export, check=False).returncode == 0
    if exported:
        run("docker", "cp", f"{export}:/work/out/work.bundle", job_dir / "work.bundle")
    run("docker", "rm", export)
    run("docker", "volume", "rm", volume)
    return code, seconds


def guard(clean: Path, branch: str, labels: list[str] = ()) -> dict:
    """Classify the agent's branch with main's critical.py, from a clean host clone."""
    graders = clean.parent / "graders"
    graders.mkdir(exist_ok=True)
    archive = subprocess.run(["git", "-C", ORIGIN, "archive", "main", "factory"], capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", graders], input=archive.stdout, check=True)
    result = run("uv", "run", "--quiet", "--project", CHECKOUT / "factory", "python",
                 graders / "factory/critical.py", "--base", "origin/main", "--head", branch,
                 *[arg for label in labels for arg in ("--label", label)], cwd=clean)  # fmt: skip
    return json.loads(result.stdout)


def import_bundle(job_dir: Path, branch: str) -> Path:
    clean = job_dir / "clean"
    run("git", "clone", "--quiet", "--no-checkout", ORIGIN, clean)
    run("git", "-C", clean, "bundle", "verify", "--quiet", job_dir / "work.bundle")
    run("git", "-C", clean, "fetch", "--quiet", job_dir / "work.bundle", f"refs/heads/{branch}:refs/heads/{branch}")
    return clean


def final_message(transcript: Path) -> dict:
    events = [json.loads(line) for line in transcript.read_text().splitlines() if line.strip()]
    results = [e for e in events if e.get("type") == "result"]
    return results[-1] if results else {}


def friction(transcript: Path, job_dir: Path) -> dict | None:
    """friction.py takes the action's JSON array, so join the stream-json lines."""
    events = [json.loads(line) for line in transcript.read_text().splitlines() if line.strip()]
    joined = job_dir / "transcript.json"
    joined.write_text(json.dumps(events))
    out = run("uv", "run", "--quiet", "--project", CHECKOUT / "factory", "python",
              CHECKOUT / "factory/friction.py", joined, check=False)  # fmt: skip
    return json.loads(out.stdout) if out.returncode == 0 else None


def squash(clean: Path, branch: str, issue: int, title: str, job_id: str) -> str:
    """One host-written commit on main with the agent's tree.

    Agent commit messages never reach main, so they cannot close issues or ping anyone,
    and `Closes #N` closes the issue when main moves.
    """
    message = f"{title} (#{issue})\n\nCloses #{issue}\n\nFactory trial {job_id}, agent branch {branch}.\n"
    env = {**os.environ, "GIT_AUTHOR_NAME": "factory-worker", "GIT_AUTHOR_EMAIL": "factory-worker@localhost",
           "GIT_COMMITTER_NAME": "factory-host", "GIT_COMMITTER_EMAIL": "factory-host@localhost"}
    sha = run("git", "-C", clean, "commit-tree", f"refs/heads/{branch}^{{tree}}", "-p", "origin/main",
              "-m", message, env=env).stdout.strip()  # fmt: skip
    run("git", "-C", clean, "update-ref", "refs/heads/land", sha)
    return sha


def secrets(clean: Path, job_dir: Path) -> list[str]:
    """"rule in file" for each secret gitleaks finds in the commit that would be pushed.

    Any finding blocks every push, the trial branch included: the repo is public, so a red
    status on a pushed branch is already a leak. The config, the ignore file and inline
    allows all come from the host, never from the worker's tree. Only rule and file are
    reported, never the match. gitleaks exits 0 on a bad range, so a scan that did not
    cover exactly the one squash commit raises and the job fails without pushing.
    """
    out = job_dir / "gitleaks.json"
    done = run("gitleaks", "git", "--no-banner", "--no-color", "--redact", "--config", GITLEAKS_CONFIG,
               "--gitleaks-ignore-path", job_dir, "--ignore-gitleaks-allow", "--log-opts", "origin/main..refs/heads/land",
               "--report-format", "json", "--report-path", out, "--exit-code", "0", clean)  # fmt: skip
    if " ERR " in done.stderr or not re.search(r"\b1 commits scanned", done.stderr):
        raise RuntimeError(f"secrets scan did not cover the land commit: {done.stderr[-300:]}")
    return sorted({f"{f['RuleID']} in {f['File']}" for f in json.loads(out.read_text())})


def outside_app(clean: Path) -> list[str]:
    # shortcut: everything outside app/ is refused, even when the issue asks for it; revisit with infra tasks.
    changed = run("git", "-C", clean, "diff", "--name-only", "origin/main", "refs/heads/land").stdout.split()
    return [path for path in changed if not path.startswith("app/")]


def build_check(job_id: str, clean: Path, job_dir: Path) -> bool:
    """fmt, clippy and test on the exact commit that would land, in a fresh container.

    The grader registry volume is separate from the worker's, so a worker cannot plant
    crate sources for this build. The worker's own claims count for nothing.
    shortcut: the build runs the trial's build.rs and tests with that volume writable, so
    one trial could tamper with the next trial's crates; fetch, then build --offline read-only.
    """
    run("git", "-C", clean, "bundle", "create", "--quiet", job_dir / "land.bundle", "refs/heads/land")
    script = ("set -e; git clone -q --branch land /work/land.bundle /work/repo; cd /work/repo; "
              "if [ ! -f app/Cargo.toml ]; then echo 'no Rust workspace, nothing to build'; exit 0; fi; "
              "cd app; cargo fmt --check; cargo clippy --all-targets -- -D warnings; cargo test")
    name = f"factory-check-{job_id}"
    run("docker", "create", "--name", name, "--label", f"factory.job={job_id}", "--network", "factory-internal",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "1024",
        "--memory", "8g", "--cpus", "8", "-v", "factory-reg-grader:/home/agent/.cargo/registry",
        IMAGE, "bash", "-c", script)  # fmt: skip
    run("docker", "cp", job_dir / "land.bundle", f"{name}:/work/land.bundle")
    (STATE / "current-job").write_text(job_id)
    try:
        done = run("docker", "start", "-a", name, check=False, timeout=BUILD_TIMEOUT_SECONDS)
        (job_dir / "build.log").write_text(done.stdout + done.stderr)
        return done.returncode == 0
    except subprocess.TimeoutExpired:
        (job_dir / "build.log").write_text("timed out\n")
        return False
    finally:
        (STATE / "current-job").write_text("none")
        run("docker", "rm", "-f", name, check=False)


def holdout_snapshot(issue: int | str, dest: Path, holdout: Path = HOLDOUT) -> str:
    """Export the committed oracle for one task into dest; return the holdout commit.

    Only the harness and this task's directory are copied, never .git or other tasks.
    Uncommitted edits raise, so a grade always names the exact oracle it ran.
    """
    paths = ["conftest.py", "pyproject.toml", f"tasks/{issue}"]
    if run("git", "-C", holdout, "status", "--porcelain", "--", *paths).stdout.strip():
        raise RuntimeError(f"holdout has uncommitted changes in {', '.join(paths)}")
    archive = subprocess.run(["git", "-C", holdout, "archive", "HEAD", *paths], capture_output=True, check=True)
    dest.mkdir()
    subprocess.run(["tar", "-x", "-C", dest], input=archive.stdout, check=True)
    return run("git", "-C", holdout, "rev-parse", "HEAD").stdout.strip()


def oracle_verdict(code: int | None, junit: str | None) -> tuple[str, str]:
    """pytest exit code and JUnit XML -> (state, description), with counts only.

    Green needs exit 0 and at least one scenario passed with none skipped. A failed
    scenario is the trial's fault ("failure"); anything else, including no scenarios,
    is the grader's ("error"). Both block landing.
    """
    if code is None:
        return "error", "oracle timed out"
    if junit is None:
        return "error", f"oracle left no result (exit {code})"
    root = ET.fromstring(junit)
    suite = root if root.tag == "testsuite" else root.find("testsuite")
    total = int(suite.get("tests", 0))
    failed = int(suite.get("failures", 0)) + int(suite.get("errors", 0))
    skipped = int(suite.get("skipped", 0))
    passed = total - failed - skipped
    if code == 0 and passed > 0 and failed == 0 and skipped == 0:
        return "success", f"{passed}/{total} scenarios passed"
    if code == 1 and failed > 0:
        return "failure", f"{failed} of {total} scenarios failed"
    return "error", f"no verdict (pytest exit {code}, {passed}/{total} passed, {skipped} skipped)"


def oracle_check(job_id: str, job_dir: Path, holdout_sha: str) -> tuple[str, str]:
    """The task's holdout scenarios against the exact commit that would land.

    Runs after build_check, on its land.bundle, with the oracle exported to job_dir/oracle.
    No network: crates come read-only from the grader registry the build gate just filled,
    so trial code cannot send the scenarios anywhere. Only counts leave this function; the
    pytest log stays in job_dir, because statuses and the issue reply are public.
    shortcut: trial code runs in the same container as the scenarios, so it could read or
    rewrite them; run the app and pytest in separate containers before trusting a hostile worker.
    shortcut: no DynamoDB Local yet; start it inside this container when T-003 needs a store.
    """
    script = ("set -e; git clone -q --branch land /work/land.bundle /work/repo; cd /work/oracle; "
              "python3 -m pytest -q -p no:cacheprovider --junitxml=/work/junit.xml tasks")
    name = f"factory-oracle-{job_id}"
    run("docker", "create", "--name", name, "--label", f"factory.job={job_id}", "--network", "none",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "1024",
        "--memory", "8g", "--cpus", "8", "-v", "factory-reg-grader:/home/agent/.cargo/registry:ro",
        "-e", "CARGO_NET_OFFLINE=true", "-e", "APP_DIR=/work/repo/app",
        ORACLE_IMAGE, "bash", "-c", script)  # fmt: skip
    run("docker", "cp", job_dir / "land.bundle", f"{name}:/work/land.bundle")
    run("docker", "cp", job_dir / "oracle", f"{name}:/work/oracle")
    (STATE / "current-job").write_text(job_id)
    try:
        try:
            done = run("docker", "start", "-a", name, check=False, timeout=BUILD_TIMEOUT_SECONDS)
            code, log = done.returncode, done.stdout + done.stderr
        except subprocess.TimeoutExpired:
            code, log = None, "timed out\n"
        (job_dir / "oracle.log").write_text(log)
        junit = job_dir / "oracle-junit.xml"
        copied = run("docker", "cp", f"{name}:/work/junit.xml", junit, check=False).returncode == 0
        state, description = oracle_verdict(code, junit.read_text() if copied else None)
        return state, f"{description}, holdout {holdout_sha[:7]}"
    finally:
        (STATE / "current-job").write_text("none")
        run("docker", "rm", "-f", name, check=False)


def gate_statuses(classified: dict, built: bool, oracle: tuple[str, str]) -> dict[str, tuple[str, str]]:
    """context -> (state, description). Critical signals are logged, not blocking (Ryan, 2026-11-11)."""
    hygiene = classified["hygiene"]
    signals = [s for s in classified["signals"] if s != "hygiene_failed"]
    return {
        "build": ("success", "fmt, clippy, test passed") if built else ("failure", "fmt, clippy or test failed"),
        "hygiene": ("success", f"passed, {hygiene['changed_lines']} lines changed")
        if hygiene["passed"]
        else ("failure", "violations: " + ", ".join(hygiene["signals"])),
        "critical-classifier": ("success", ("signals (not blocking): " + ", ".join(signals)) if signals else "no signals"),
        "oracle": oracle,
    }


def post_status(sha: str, context: str, state: str, description: str) -> None:
    run("gh", "api", "-X", "POST", f"repos/{REPO}/statuses/{sha}", "-f", f"state={state}",
        "-f", f"context={context}", "-f", f"description={description[:140]}")  # fmt: skip


def publish(clean: Path, sha: str, branch: str, statuses: dict[str, tuple[str, str]]) -> str:
    """Push the trial branch, post the gate results on it, then fast-forward main if all green.

    Main's required checks then see the statuses this host just posted. Never forces.
    """
    run("git", "-C", clean, "push", "--quiet", REPO_URL, f"{sha}:refs/heads/{branch}")
    for context, (state, description) in statuses.items():
        post_status(sha, context, state, description)
    red = [context for context, (state, _) in statuses.items() if state != "success"]
    if red:
        return f"gates failed: {', '.join(red)}"
    pushed = run("git", "-C", clean, "push", REPO_URL, f"{sha}:refs/heads/main", check=False)
    (clean.parent / "push-main.log").write_text(pushed.stderr)
    if pushed.returncode != 0:
        return "not landed: main moved during the trial (or rejected the push)"
    # E10: an admin push succeeds even without the statuses, so say when GitHub bypassed them.
    return "landed (bypassed branch protection)" if "bypass" in pushed.stderr.lower() else "landed"


def evidence_comment(result: dict, frictions: dict | None, job_id: str) -> str:
    summary = neutralize_mentions(result.get("result") or "(the worker left no final message)")
    stats = f"turns: {result.get('num_turns')}, cost: ${result.get('total_cost_usd')}, job: `{job_id}`"
    return f"### Worker evidence\n\n{summary}\n\n---\n{stats}\n\nfriction: `{json.dumps(frictions)}`"


def preflight() -> None:
    """Prove the proxy blocks and injects before trusting it with a job.

    mitmproxy keeps serving if its addon unloads, and then it is an open proxy with no
    key. Both probes are free: a blocked CONNECT and Anthropic's model list.
    """
    (STATE / "current-job").write_text("preflight")
    probe = ("code() { curl -s -o /dev/null -w '%{http_code}' \"$@\"; }; "
             "echo \"$(code -m 10 https://example.com) "
             "$(code -m 10 -H 'anthropic-version: 2023-06-01' -H 'x-api-key: placeholder' "
             "https://api.anthropic.com/v1/models)\"")
    try:
        out = run("docker", "run", "--rm", "--network", "factory-internal", "--cap-drop", "ALL",
                  IMAGE, "bash", "-c", probe, check=False).stdout.split()  # fmt: skip
    finally:
        (STATE / "current-job").write_text("none")
    blocked, models = (out + ["?", "?"])[:2]
    if blocked not in {"000", "403"} or models != "200":
        raise SystemExit(f"proxy preflight failed (example.com -> {blocked}, model list -> {models})")


def worker(issue: int, push: bool) -> dict:
    data = json.loads(run("gh", "issue", "view", str(issue), "--repo", REPO, "--json", "title,body,labels").stdout)
    if "oracle:ready" not in {label["name"] for label in data["labels"]}:
        raise SystemExit(f"issue #{issue} is not labeled oracle:ready")
    check_spec(issue, data["body"])
    preflight()

    trial = next_trial(issue)
    branch = f"claude/issue-{issue}-t{trial}"
    job_id = f"{issue}-t{trial}-{time.strftime('%Y%m%dT%H%M%S')}"
    job_dir = STATE / "jobs" / job_id
    (job_dir / "in").mkdir(parents=True)
    base = refresh_origin()
    run("git", "-C", ORIGIN, "bundle", "create", "--quiet", job_dir / "in/base.bundle", "HEAD", "main")
    (job_dir / "in/branch").write_text(branch)
    pages = json.loads(run("gh", "api", "--paginate", "--slurp", f"repos/{REPO}/issues/{issue}/comments?per_page=100").stdout)
    clarifications = thread([c for page in pages for c in page])
    (job_dir / "in/prompt.txt").write_text(prompt(issue, branch, data["body"], clarifications))
    holdout = holdout_snapshot(issue, job_dir / "oracle")  # before the paid run; pins the oracle the trial is graded on

    code, seconds = run_sandbox(job_id, issue, job_dir)
    transcript = job_dir / "transcript.jsonl"
    result = final_message(transcript) if transcript.exists() else {}
    container = (job_dir / "container_id").read_text()[:31]  # Tetragon's container tag
    summary = {"job_id": job_id, "branch": branch, "base_commit": base, "exit": code,
               "wall_clock_seconds": round(seconds, 1), "turns": result.get("num_turns"), "commit": None, "holdout": holdout,
               "clarifications": clarifications or None,
               "dashboard": f"http://localhost:3300/d/factory-run/?var-job={job_id}&var-container={container}"}  # fmt: skip

    if transcript.exists():
        summary["evidence"] = evidence_comment(result, friction(transcript, job_dir), job_id)
    if asked(result):  # never land a guess next to an open critical question, even if it committed
        summary["outcome"] = "needs clarification"
        if push:
            run("gh", "issue", "edit", str(issue), "--repo", REPO, "--add-label", NEEDS_ANSWER)
    elif not (job_dir / "work.bundle").exists():
        summary["outcome"] = "no commits"
    else:
        clean = import_bundle(job_dir, branch)
        sha = squash(clean, branch, issue, data["title"], job_id)
        if found := secrets(clean, job_dir):
            summary["outcome"] = f"refused: possible secrets ({', '.join(found[:5])})"
        elif outside := outside_app(clean):
            summary["outcome"] = f"refused: changes outside app/ ({', '.join(outside[:5])})"
        else:
            labels = [label["name"] for label in data["labels"]]
            classified = guard(clean, "refs/heads/land", labels)
            summary["signals"] = classified["signals"]
            built = build_check(job_id, clean, job_dir)
            oracle = oracle_check(job_id, job_dir, holdout) if built else ("failure", "not run: build failed")
            summary["gates"] = gate_statuses(classified, built, oracle)
            red = [c for c, (state, _) in summary["gates"].items() if state != "success"]
            if push:
                summary["outcome"] = publish(clean, sha, branch, summary["gates"])
                summary["commit"] = sha if summary["outcome"].startswith("landed") else None
            else:
                summary["outcome"] = f"gates failed: {', '.join(red)}" if red else "green (not pushed)"
    (job_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def report(summary: dict) -> str:
    """The host's issue reply: outcome, gate table, then the worker's own evidence."""
    if summary["commit"]:
        head = f"{summary['outcome'].capitalize()} on main as {summary['commit']} (`{summary['branch']}`)."
    else:
        head = f"Not landed: {summary['outcome']}."
    lines = [f"Trial `{summary['job_id']}` finished in {summary['wall_clock_seconds']} s. {head}", ""]
    if summary.get("gates"):
        lines += ["| gate | result | |", "| --- | --- | --- |"]
        lines += [f"| {c} | {state} | {desc} |" for c, (state, desc) in summary["gates"].items()]
        lines.append("")
    if summary["outcome"] == "needs clarification":
        lines += ["The worker needs answers before it builds. Reply in a comment; your reply starts the next trial.", ""]
    lines += [f"[dashboard]({summary['dashboard']}) (local)", "", summary.get("evidence", "")]
    return neutralize_mentions("\n".join(lines))


def owner_issue(c: dict) -> int | None:
    """The issue number if this is Ryan's own comment on an issue, else None.

    The repo is public and PR comments come back from the same endpoint, so each check
    matters. The host posts as Ryan too; callers tell its comments apart by MARKER.
    """
    match = re.search(r"/issues/(\d+)$", c.get("issue_url", ""))
    if (
        match
        and "/pull/" not in c.get("html_url", "")
        and c.get("author_association") == "OWNER"
        and c.get("user", {}).get("login") == OWNER
    ):
        return int(match.group(1))
    return None


def build_requests(comments: list[dict]) -> list[tuple[int, int]]:
    """(comment id, issue number) for each of Ryan's comments whose whole body is /build."""
    return [(c["id"], issue) for c in comments
            if (issue := owner_issue(c)) and c.get("body", "").strip() == "/build" and MARKER not in c["body"]]  # fmt: skip


def answer_requests(comments: list[dict], waiting: set[int]) -> list[tuple[int, int]]:
    """(comment id, issue number) for Ryan's other comments on issues waiting for an answer."""
    return [(c["id"], issue) for c in comments
            if (issue := owner_issue(c)) in waiting and c.get("body", "").strip() not in {"", "/build"}
            and MARKER not in c["body"]]  # fmt: skip


def questions(body: str) -> str:
    """The worker's NEEDS CLARIFICATION block from a host reply, or "" if it did not ask."""
    _, found, rest = body.partition("### Worker evidence\n\n")
    text = rest.rpartition("\n\n---\nturns: ")[0].strip()
    return text if found and asked({"result": text}) else ""


def thread(comments: list[dict]) -> str:
    """The worker's questions and Ryan's replies, oldest first, for the <clarifications> block.

    Comments are not instructions; this is the one exception, and only Ryan's own comments
    (and the host's, which carry the worker's questions) get in. "" until the worker asks.
    """
    parts = []
    for c in comments:
        body = c.get("body", "")
        if not owner_issue(c):
            continue
        if MARKER in body:
            if q := questions(body):
                parts.append(f"Worker asked:\n{q}")
        elif parts and body.strip() not in {"", "/build"}:
            parts.append(f"Owner replied:\n{body.strip()}")
    return "\n\n".join(parts)


def claimed(comment_id: int) -> bool:
    reactions = json.loads(run("gh", "api", f"repos/{REPO}/issues/comments/{comment_id}/reactions?content=rocket").stdout)
    return any(r["user"]["login"] == OWNER for r in reactions)


def react(comment_id: int, content: str) -> None:
    run("gh", "api", "-X", "POST", f"repos/{REPO}/issues/comments/{comment_id}/reactions", "-f", f"content={content}")


def reply(issue: int, text: str) -> None:
    run("gh", "issue", "comment", str(issue), "--repo", REPO, "--body", f"{text}\n\n{MARKER}")


def poll() -> None:
    """One pass: claim each new /build with a rocket, then run it. systemd keeps it single."""
    since_file = STATE / "poll-since"
    started = time.time()
    # Look back from the last pass with some overlap; the rocket makes re-reads harmless.
    since = since_file.read_text().strip() if since_file.exists() else time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 86400))
    comments = json.loads(run("gh", "api", "--paginate", "--slurp",
                              f"repos/{REPO}/issues/comments?since={since}&per_page=100").stdout)  # fmt: skip
    comments = [c for page in comments for c in page]
    labeled = json.loads(run("gh", "issue", "list", "--repo", REPO, "--label", NEEDS_ANSWER, "--json", "number").stdout)
    waiting = {i["number"] for i in labeled}
    jobs = [(issue, [cid]) for cid, issue in build_requests(comments) if not claimed(cid)]
    answers: dict[int, list[int]] = {}
    for cid, issue in answer_requests(comments, waiting):
        if not claimed(cid):
            answers.setdefault(issue, []).append(cid)
    # shortcut: several replies in one pass start one trial (all are in its thread); replies
    # posted while it runs start another only if it asks again.
    for issue, ids in jobs + list(answers.items()):
        for cid in ids:
            react(cid, "rocket")
        if issue in waiting:
            run("gh", "issue", "edit", str(issue), "--repo", REPO, "--remove-label", NEEDS_ANSWER)
            waiting.discard(issue)
        try:
            summary = worker(issue, push=True)
        except SystemExit as refused:
            react(ids[-1], "confused")
            reply(issue, f"Factory refused this trial: {refused}")
            continue
        except Exception as error:  # report, then keep polling the next request
            react(ids[-1], "confused")
            reply(issue, f"Factory job failed on the host: `{type(error).__name__}: {error}`")
            continue
        reply(issue, report(summary))
    since_file.write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started - 300)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    w = sub.add_parser("worker")
    w.add_argument("issue", type=int)
    w.add_argument("--no-push", action="store_true")
    sub.add_parser("poll")
    args = parser.parse_args()
    if args.command == "poll":
        poll()
    elif args.command == "worker":
        json.dump(worker(args.issue, push=not args.no_push), sys.stdout, indent=2)
        print()


if __name__ == "__main__":
    main()
