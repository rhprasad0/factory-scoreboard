import json
import subprocess

import pytest

import factory


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def test_spec_check_accepts_the_frozen_body(tmp_path):
    body = "Add GET /health.\n"
    (tmp_path / "tasks/4").mkdir(parents=True)
    (tmp_path / "tasks/4/task.json").write_text(json.dumps({"spec_sha": factory.spec_sha(body)}))
    factory.check_spec(4, body, holdout=tmp_path)


def test_spec_check_refuses_an_edited_body(tmp_path):
    (tmp_path / "tasks/4").mkdir(parents=True)
    (tmp_path / "tasks/4/task.json").write_text(json.dumps({"spec_sha": factory.spec_sha("original")}))
    with pytest.raises(SystemExit, match="does not match"):
        factory.check_spec(4, "original, edited later", holdout=tmp_path)


def test_spec_check_refuses_an_issue_without_a_task(tmp_path):
    with pytest.raises(SystemExit, match="no holdout task"):
        factory.check_spec(9, "anything", holdout=tmp_path)


@pytest.mark.parametrize("text", ["ping @rhprasad0", "cc @claude please", "@team-x"])
def test_mentions_are_neutralized(text):
    assert "@" + text.split("@")[1][0] not in factory.neutralize_mentions(text)


def test_emails_and_lone_at_signs_survive():
    assert factory.neutralize_mentions("a @ b") == "a @ b"


def test_evidence_comment_has_no_live_mentions():
    comment = factory.evidence_comment({"result": "done, thanks @rhprasad0", "num_turns": 3}, None, "4-t2-x")
    assert "@rhprasad0" not in comment


def test_bundle_import_runs_nothing_the_agent_planted(tmp_path, monkeypatch):
    """E12: hooks and config written by the agent must never execute on the host."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", "-q", "-b", "main", cwd=seed)
    (seed / "README.md").write_text("hi\n")
    git("add", ".", cwd=seed)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base", cwd=seed)
    git("clone", "-q", "--bare", str(seed), str(origin), cwd=tmp_path)

    # The agent's workspace: a planted hook and fsmonitor that would leave a marker.
    agent = tmp_path / "agent"
    git("clone", "-q", str(origin), str(agent), cwd=tmp_path)
    marker = tmp_path / "PWNED"
    hook = f"#!/bin/sh\ntouch {marker}\n"
    for name in ("pre-push", "post-checkout", "reference-transaction", "post-merge"):
        (agent / ".git/hooks" / name).write_text(hook)
        (agent / ".git/hooks" / name).chmod(0o755)
    (tmp_path / "fsmon.sh").write_text(hook)
    (tmp_path / "fsmon.sh").chmod(0o755)
    git("config", "core.fsmonitor", str(tmp_path / "fsmon.sh"), cwd=agent)
    git("checkout", "-qb", "claude/issue-4-t2", cwd=agent)
    (agent / "app.rs").write_text("fn main() {}\n")
    git("add", ".", cwd=agent)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "work", cwd=agent)
    marker.unlink(missing_ok=True)  # the agent's own hooks may fire inside the sandbox

    job = tmp_path / "job"
    job.mkdir()
    git("bundle", "create", str(job / "work.bundle"), "refs/heads/claude/issue-4-t2", "^origin/main", cwd=agent)

    monkeypatch.setattr(factory, "ORIGIN", origin)
    clean = factory.import_bundle(job, "claude/issue-4-t2")
    git("diff", "origin/main...claude/issue-4-t2", cwd=clean)
    git("status", cwd=clean)
    remote = tmp_path / "remote.git"
    git("init", "-q", "--bare", str(remote), cwd=tmp_path)
    git("push", "-q", str(remote), "refs/heads/claude/issue-4-t2", cwd=clean)

    assert not marker.exists()
    assert "fsmonitor" not in git("config", "--list", cwd=clean)
    assert git("log", "--format=%s", "-1", "claude/issue-4-t2", cwd=clean).strip() == "work"


def comment(**over):
    base = {
        "id": 1,
        "issue_url": "https://api.github.com/repos/rhprasad0/factory-scoreboard/issues/4",
        "html_url": "https://github.com/rhprasad0/factory-scoreboard/issues/4#issuecomment-1",
        "author_association": "OWNER",
        "user": {"login": "rhprasad0"},
        "body": "/build",
    }
    return {**base, **over}


def test_owner_build_comment_on_an_issue_is_a_request():
    assert factory.build_requests([comment(body="/build\r\n")]) == [(1, 4)]


@pytest.mark.parametrize(
    "over",
    [
        {"author_association": "NONE", "user": {"login": "stranger"}},
        {"author_association": "OWNER", "user": {"login": "someone-else"}},
        {"html_url": "https://github.com/rhprasad0/factory-scoreboard/pull/7#issuecomment-1"},
        {"body": "/build please"},
        {"body": "please /build"},
        {"body": f"/build\n\n{factory.MARKER}"},
        {"issue_url": "https://api.github.com/repos/rhprasad0/factory-scoreboard/pulls/7"},
    ],
    ids=["stranger", "wrong-login", "pr-comment", "extra-words", "not-alone", "host-marked", "not-an-issue"],
)
def test_everything_else_is_ignored(over):
    assert factory.build_requests([comment(**over)]) == []


@pytest.fixture
def trial(tmp_path, monkeypatch):
    """A fake GitHub remote, the host's ORIGIN clone of it, and an imported agent branch."""
    github = tmp_path / "github.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", "-q", "-b", "main", cwd=seed)
    (seed / "README.md").write_text("hi\n")
    git("add", ".", cwd=seed)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base", cwd=seed)
    git("clone", "-q", "--bare", str(seed), str(github), cwd=tmp_path)
    origin = tmp_path / "origin.git"
    git("clone", "-q", "--bare", str(github), str(origin), cwd=tmp_path)

    agent = tmp_path / "agent"
    git("clone", "-q", str(origin), str(agent), cwd=tmp_path)
    git("checkout", "-qb", "claude/issue-4-t6", cwd=agent)
    (agent / "app").mkdir()
    (agent / "app/lib.rs").write_text("fn health() {}\n")
    git("add", ".", cwd=agent)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "Closes #5, thanks @someone", cwd=agent)
    job = tmp_path / "job"
    job.mkdir()
    git("bundle", "create", str(job / "work.bundle"), "refs/heads/claude/issue-4-t6", "^origin/main", cwd=agent)

    posted = []
    monkeypatch.setattr(factory, "ORIGIN", origin)
    monkeypatch.setattr(factory, "REPO_URL", str(github))
    monkeypatch.setattr(factory, "post_status", lambda sha, *status: posted.append((sha, *status)))
    clean = factory.import_bundle(job, "claude/issue-4-t6")
    sha = factory.squash(clean, "claude/issue-4-t6", 4, "Add GET /health", "4-t6-x")
    return {"github": github, "seed": seed, "clean": clean, "sha": sha, "posted": posted}


GREEN = {gate: ("success", "ok") for gate in factory.GATES}


def test_squash_writes_the_host_message_over_the_agent_tree(trial):
    message = git("log", "-1", "--format=%an%n%B", trial["sha"], cwd=trial["clean"])
    assert message.startswith("factory-worker\nAdd GET /health (#4)\n\nCloses #4")
    assert "#5" not in message and "@someone" not in message
    assert git("rev-parse", f"{trial['sha']}^", cwd=trial["clean"]) == git("rev-parse", "origin/main", cwd=trial["clean"])
    assert factory.outside_app(trial["clean"]) == []


def test_green_gates_land_on_main_with_statuses(trial):
    assert factory.publish(trial["clean"], trial["sha"], "claude/issue-4-t6", GREEN) == "landed"
    assert git("rev-parse", "main", cwd=trial["github"]).strip() == trial["sha"]
    assert git("rev-parse", "claude/issue-4-t6", cwd=trial["github"]).strip() == trial["sha"]
    assert {p[1] for p in trial["posted"]} == set(factory.GATES)


def test_a_red_gate_pushes_the_branch_but_not_main(trial):
    before = git("rev-parse", "main", cwd=trial["github"])
    gates = {**GREEN, "build": ("failure", "cargo test failed")}
    assert factory.publish(trial["clean"], trial["sha"], "claude/issue-4-t6", gates) == "gates failed: build"
    assert git("rev-parse", "main", cwd=trial["github"]) == before
    assert ("build", "failure") in {(p[1], p[2]) for p in trial["posted"]}


def test_main_moving_during_the_trial_is_never_forced(trial):
    seed = trial["seed"]
    (seed / "other.txt").write_text("someone else\n")
    git("add", ".", cwd=seed)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "meanwhile", cwd=seed)
    git("push", "-q", str(trial["github"]), "main", cwd=seed)
    moved = git("rev-parse", "main", cwd=trial["github"])
    assert factory.publish(trial["clean"], trial["sha"], "claude/issue-4-t6", GREEN).startswith("not landed")
    assert git("rev-parse", "main", cwd=trial["github"]) == moved


def test_report_shows_the_landing_gates_and_evidence():
    summary = {"job_id": "4-t7-x", "wall_clock_seconds": 30.0, "commit": "abc123", "outcome": "landed",
               "branch": "claude/issue-4-t7", "dashboard": "http://localhost:3300/d/x",
               "gates": {"build": ["success", "ok"]}, "evidence": "### Worker evidence\n\nthanks @rhprasad0"}
    text = factory.report(summary)
    assert "Landed on main as abc123" in text and "| build | success | ok |" in text
    assert "@rhprasad0" not in text
    refused = factory.report({**summary, "commit": None, "outcome": "refused: changes outside app/ (x)"})
    assert "Not landed: refused" in refused


def test_gate_table_logs_critical_signals_without_blocking():
    classified = {"signals": ["dependency", "oversized"], "hygiene": {"passed": True, "signals": {}, "changed_lines": 500}}
    gates = factory.gate_statuses(classified, built=True)
    assert all(state == "success" for state, _ in gates.values())
    assert "dependency" in gates["critical-classifier"][1]


def test_a_hygiene_violation_is_a_red_gate():
    hygiene = {"passed": False, "signals": {"deleted_tests": ["x"]}, "changed_lines": 3}
    gates = factory.gate_statuses({"signals": ["hygiene_failed"], "hygiene": hygiene}, built=True)
    assert gates["hygiene"] == ("failure", "violations: deleted_tests")
    assert gates["critical-classifier"] == ("success", "no signals")
