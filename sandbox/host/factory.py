"""Local factory host: runs worker jobs in the sandbox and does the GitHub side itself.

Usage:
  factory.py poll                    claim new /build comments and run them (systemd timer)
  factory.py worker ISSUE [--no-push]

Trust split: the host holds the GitHub login and never runs git in a repository the
agent could write to. Work leaves the sandbox only as a git bundle, which carries
objects and refs, never hooks or config. See sandbox/README.md.

There are no PRs. The host squashes the work onto main, grades that exact commit
(hygiene, critical, then fmt/clippy/test in a fresh container), pushes the trial branch,
posts the results as commit statuses, and fast-forwards main only if every gate is green.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
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
MARKER = "<!-- factory-host -->"
TIMEOUT_SECONDS = 30 * 60
BUILD_TIMEOUT_SECONDS = 20 * 60
GATES = ("build", "hygiene", "critical-classifier")  # main's required status checks

PROMPT = """You are a factory worker for {repo}, working on issue #{issue}.

Read CLAUDE.md first and follow it.

You are on branch {branch}, created from main. Commit your work there. Do not push: the
host grades your commits and lands them on main if every gate passes.

The issue body below is your only spec.

<spec>
{body}
</spec>
"""


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


def agent_env(job_id: str, issue: int) -> list[str]:
    env = {
        "ANTHROPIC_API_KEY": "placeholder-the-proxy-sets-the-real-key",
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


def run_sandbox(job_id: str, issue: int, job_dir: Path) -> tuple[int, float]:
    """Run the agent; return (exit code, seconds). Outputs land in job_dir."""
    volume = f"factory-job-{job_id}"
    run("docker", "volume", "create", volume)
    name = f"factory-job-{job_id}"
    run("docker", "create", *sandbox_args(job_id, volume, "factory-reg-worker"), *agent_env(job_id, issue),
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


def outside_app(clean: Path) -> list[str]:
    # shortcut: everything outside app/ is refused, even when the issue asks for it; revisit with infra tasks.
    changed = run("git", "-C", clean, "diff", "--name-only", "origin/main", "refs/heads/land").stdout.split()
    return [path for path in changed if not path.startswith("app/")]


def build_check(job_id: str, clean: Path, job_dir: Path) -> bool:
    """fmt, clippy and test on the exact commit that would land, in a fresh container.

    The grader registry volume is separate from the worker's, so the worker cannot
    poison the crates this build compiles. The worker's own claims count for nothing.
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


def gate_statuses(classified: dict, built: bool) -> dict[str, tuple[str, str]]:
    """context -> (state, description). Critical signals are logged, not blocking (Ryan, 2026-11-11)."""
    hygiene = classified["hygiene"]
    signals = [s for s in classified["signals"] if s != "hygiene_failed"]
    return {
        "build": ("success", "fmt, clippy, test passed") if built else ("failure", "fmt, clippy or test failed"),
        "hygiene": ("success", f"passed, {hygiene['changed_lines']} lines changed")
        if hygiene["passed"]
        else ("failure", "violations: " + ", ".join(hygiene["signals"])),
        "critical-classifier": ("success", ("signals (not blocking): " + ", ".join(signals)) if signals else "no signals"),
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
    pushed = run("git", "-C", clean, "push", "--quiet", REPO_URL, f"{sha}:refs/heads/main", check=False)
    if pushed.returncode != 0:
        return "not landed: main moved during the trial (or rejected the push)"
    return "landed"


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
    (job_dir / "in/prompt.txt").write_text(PROMPT.format(repo=REPO, issue=issue, branch=branch, body=data["body"]))

    code, seconds = run_sandbox(job_id, issue, job_dir)
    transcript = job_dir / "transcript.jsonl"
    result = final_message(transcript) if transcript.exists() else {}
    container = (job_dir / "container_id").read_text()[:31]  # Tetragon's container tag
    summary = {"job_id": job_id, "branch": branch, "base_commit": base, "exit": code,
               "wall_clock_seconds": round(seconds, 1), "turns": result.get("num_turns"), "commit": None,
               "dashboard": f"http://localhost:3300/d/factory-run/?var-job={job_id}&var-container={container}"}  # fmt: skip

    if transcript.exists():
        summary["evidence"] = evidence_comment(result, friction(transcript, job_dir), job_id)
    if not (job_dir / "work.bundle").exists():
        summary["outcome"] = "no commits"
    else:
        clean = import_bundle(job_dir, branch)
        sha = squash(clean, branch, issue, data["title"], job_id)
        if outside := outside_app(clean):
            summary["outcome"] = f"refused: changes outside app/ ({', '.join(outside[:5])})"
        else:
            labels = [label["name"] for label in data["labels"]]
            classified = guard(clean, "refs/heads/land", labels)
            summary["signals"] = classified["signals"]
            summary["gates"] = gate_statuses(classified, build_check(job_id, clean, job_dir))
            red = [c for c, (state, _) in summary["gates"].items() if state != "success"]
            if push:
                summary["outcome"] = publish(clean, sha, branch, summary["gates"])
                summary["commit"] = sha if summary["outcome"] == "landed" else None
            else:
                summary["outcome"] = f"gates failed: {', '.join(red)}" if red else "green (not pushed)"
    (job_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def report(summary: dict) -> str:
    """The host's issue reply: outcome, gate table, then the worker's own evidence."""
    if summary["commit"]:
        head = f"Landed on main as {summary['commit']} (`{summary['branch']}`)."
    else:
        head = f"Not landed: {summary['outcome']}."
    lines = [f"Trial `{summary['job_id']}` finished in {summary['wall_clock_seconds']} s. {head}", ""]
    if summary.get("gates"):
        lines += ["| gate | result | |", "| --- | --- | --- |"]
        lines += [f"| {c} | {state} | {desc} |" for c, (state, desc) in summary["gates"].items()]
        lines.append("")
    lines += [f"[dashboard]({summary['dashboard']}) (local)", "", summary.get("evidence", "")]
    return neutralize_mentions("\n".join(lines))


def build_requests(comments: list[dict]) -> list[tuple[int, int]]:
    """(comment id, issue number) for each comment that asks for a trial.

    Only Ryan's own issue comments whose whole body is /build count. The repo is
    public, the host posts as Ryan too, and PR comments come back from the same
    endpoint, so each of those checks matters.
    """
    wanted = []
    for c in comments:
        match = re.search(r"/issues/(\d+)$", c.get("issue_url", ""))
        if (
            match
            and "/pull/" not in c.get("html_url", "")
            and c.get("author_association") == "OWNER"
            and c.get("user", {}).get("login") == OWNER
            and c.get("body", "").strip() == "/build"
            and MARKER not in c.get("body", "")
        ):
            wanted.append((c["id"], int(match.group(1))))
    return wanted


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
    for comment_id, issue in build_requests([c for page in comments for c in page]):
        if claimed(comment_id):
            continue
        react(comment_id, "rocket")
        try:
            summary = worker(issue, push=True)
        except SystemExit as refused:
            react(comment_id, "confused")
            reply(issue, f"Factory refused this `/build`: {refused}")
            continue
        except Exception as error:  # report, then keep polling the next request
            react(comment_id, "confused")
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
