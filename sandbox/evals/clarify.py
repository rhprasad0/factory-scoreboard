"""Clarify eval: does the worker infer minor gaps, ask on critical ones, and build from the answer?

Usage (from sandbox/host, like factory.py):
  uv run --project ../../factory python ../evals/clarify.py [--trials 3] [--parallel 2] [--case NAME ...]
  uv run --project ../../factory python ../evals/clarify.py --summary

Cases live in the holdout checkout, never in this repo, because the worker clones all of
main and the labels are the answer key: HOLDOUT/tasks/clarify-<name>/ holds spec.md (sent
verbatim), case.json ({"expect": "proceed"|"ask", "kind": "complete"|"inferable"|"critical",
"gap": ...}), answer.md for critical cases (Ryan's canned reply), and test_*.py oracle
scenarios. Proceed cases' oracles test the spec; critical cases' test what the answer asks for.

Each trial runs the unchanged production worker (factory.prompt, CLAUDE.md from main, same
model and flags). Turn 1 sends the spec. If a critical case asks, turn 2 reruns from the
same base with the questions and answer.md as the <clarifications> thread, built by the same
factory.thread() the host uses. Commits are graded with the host's own build and oracle gates.
A case passes when the decision matches its label (asking means no commit) and the oracle is
green: on turn 1 for proceed cases, on turn 2 for critical ones.

Nothing is pushed or posted. Results are appended to state/evals/clarify.jsonl (gitignored);
a rerun skips (case, trial) pairs already there. Pause the poll timer during a batch:
systemctl --user stop factory-poll.timer.
"""

import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "host"))
import factory as f  # noqa: E402

OUT = f.STATE / "evals"
RESULTS = OUT / "clarify.jsonl"
ISSUE = 998  # fake issue number for the prompt; nothing is posted
LOCK = threading.Lock()


def cases() -> list[Path]:
    return sorted((f.HOLDOUT / "tasks").glob("clarify-*"))


def as_comment(body: str) -> dict:
    """An issue comment as GitHub returns it, from Ryan's account (the host posts as him too)."""
    return {"issue_url": f"https://api.github.com/repos/{f.REPO}/issues/{ISSUE}",
            "html_url": f"https://github.com/{f.REPO}/issues/{ISSUE}#issuecomment-1",
            "author_association": "OWNER", "user": {"login": f.OWNER}, "body": body}  # fmt: skip


def grade(expect: str, t1: dict, t2: dict | None) -> dict:
    decision_ok = t1["asked"] == (expect == "ask") and not (t1["asked"] and t1["committed"])
    resolved = None
    if expect == "ask":
        resolved = bool(t2) and not t2["asked"] and t2["committed"] and t2["oracle"] == "success"
        passed = decision_ok and resolved
    else:
        passed = decision_ok and t1["oracle"] == "success"
    return {
        "decision_ok": decision_ok,
        "resolved": resolved,
        "pass": passed,
        "under_asked": expect == "ask" and not t1["asked"],
        "over_asked": expect == "proceed" and t1["asked"],
        "asked_and_committed": t1["asked"] and t1["committed"],
        "re_asked": bool(t2) and t2["asked"],
    }


def turn(case: Path, job_id: str, clarifications: str, graded: bool) -> dict:
    """One worker run; build and oracle only when it committed and `graded`."""
    job_dir = OUT / "jobs" / job_id
    branch = f"claude/issue-{ISSUE}-eval"
    (job_dir / "in").mkdir(parents=True)
    f.run("git", "-C", f.ORIGIN, "bundle", "create", "--quiet", job_dir / "in/base.bundle", "HEAD", "main")
    (job_dir / "in/branch").write_text(branch)
    (job_dir / "in/prompt.txt").write_text(f.prompt(ISSUE, branch, (case / "spec.md").read_text(), clarifications))
    holdout = f.holdout_snapshot(case.name, job_dir / "oracle")

    code, seconds = f.run_sandbox(job_id, ISSUE, job_dir)
    transcript = job_dir / "transcript.jsonl"
    result = f.final_message(transcript) if transcript.exists() else {}
    final = result.get("result") or ""
    committed = (job_dir / "work.bundle").exists()
    rec = {"job_id": job_id, "exit": code, "seconds": round(seconds, 1), "turns": result.get("num_turns"),
           "cost_usd": result.get("total_cost_usd"), "asked": f.asked(result), "committed": committed,
           "assumptions_listed": bool(re.search(r"^#+\s*assumptions", final, re.M | re.I)),
           "build": None, "oracle": None, "holdout": holdout, "final": final,
           "evidence": f.evidence_comment(result, None, job_id) if transcript.exists() else ""}  # fmt: skip
    if committed and graded:
        clean = f.import_bundle(job_dir, branch)
        f.squash(clean, branch, ISSUE, f"eval {case.name}", job_id)
        rec["build"] = f.build_check(job_id, clean, job_dir)
        rec["oracle"] = f.oracle_check(job_id, job_dir, holdout)[0] if rec["build"] else "not run"
    return rec


def trial(case: Path, n: int, batch: str) -> dict:
    meta = json.loads((case / "case.json").read_text())
    stamp = time.strftime("%Y%m%dT%H%M%S")
    name = case.name.removeprefix("clarify-")
    t1 = turn(case, f"eval-clarify-{name}-{n}-1-{stamp}", "", graded=meta["expect"] == "proceed")
    t2 = None
    if meta["expect"] == "ask" and t1["asked"]:
        replies = [as_comment(f"{t1['evidence']}\n\n{f.MARKER}"), as_comment((case / "answer.md").read_text())]
        t2 = turn(case, f"eval-clarify-{name}-{n}-2-{stamp}", f.thread(replies), graded=True)
    record = {"batch": batch, "case": name, "kind": meta["kind"], "expect": meta["expect"], "trial": n,
              "t1": t1, "t2": t2, **grade(meta["expect"], t1, t2)}  # fmt: skip
    with LOCK, RESULTS.open("a") as out:
        out.write(json.dumps(record) + "\n")
    return record


def attempt(case: Path, n: int, batch: str) -> None:
    try:
        r = trial(case, n, batch)
        line = (f"{r['case']} #{n}: pass={r['pass']} asked={r['t1']['asked']} committed={r['t1']['committed']} "
                f"oracle={(r['t2'] or r['t1'])['oracle']}")  # fmt: skip
    except (RuntimeError, OSError, json.JSONDecodeError, f.subprocess.CalledProcessError) as error:
        line = f"{case.name} #{n}: harness error {type(error).__name__}: {error}"
    with LOCK:
        print(line, flush=True)


def summary(records: list[dict]) -> str:
    cols = ["n", "pass", "decision_ok", "under_asked", "over_asked", "asked_and_committed", "re_asked",
            "assumptions", "oracle_green", "cost"]  # fmt: skip
    rows: dict[str, dict] = {}
    for r in records:
        row = rows.setdefault(f"{r['kind']}/{r['case']}", dict.fromkeys(cols, 0))
        row["n"] += 1
        for k in ("pass", "decision_ok", "under_asked", "over_asked", "asked_and_committed", "re_asked"):
            row[k] += bool(r[k])
        row["assumptions"] += r["t1"]["assumptions_listed"]
        row["oracle_green"] += (r["t2"] or r["t1"])["oracle"] == "success"
        row["cost"] += sum((t or {}).get("cost_usd") or 0 for t in (r["t1"], r["t2"]))
    total = {c: sum(row[c] for row in rows.values()) for c in cols}
    lines = ["| case | " + " | ".join(cols) + " |", "|---" * (len(cols) + 1) + "|"]
    for name, row in sorted(rows.items()) + [("**all**", total)]:
        cells = [f"${row[c]:.2f}" if c == "cost" else (str(row[c]) if c == "n" else f"{row[c]}/{row['n']}") for c in cols]
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--parallel", type=int, default=2, help="concurrent trials (worker, build and oracle containers each cap 8 CPU / 8 GB)")
    parser.add_argument("--case", action="append", help="case name (clarify-<name> in the holdout); default all")
    parser.add_argument("--summary", action="store_true", help="print the table for all recorded trials and exit")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    load = lambda: [json.loads(line) for line in RESULTS.read_text().splitlines()] if RESULTS.exists() else []  # noqa: E731
    if not args.summary:
        todo_cases = cases()
        if args.case:
            todo_cases = [c for c in todo_cases if c.name.removeprefix("clarify-") in args.case]
            if len(todo_cases) != len(args.case):
                raise SystemExit(f"unknown case in {args.case}")
        if not todo_cases:
            raise SystemExit(f"no clarify-* cases under {f.HOLDOUT / 'tasks'}")
        if (state := (f.STATE / "current-job").read_text().strip()) != "none":
            raise SystemExit(f"another job is running ({state}); stop factory-poll.timer and retry")
        done = {(r["case"], r["trial"]) for r in load()}
        todo = [(c, n) for n in range(1, args.trials + 1) for c in todo_cases
                if (c.name.removeprefix("clarify-"), n) not in done]  # fmt: skip
        f.preflight()
        f.refresh_origin()  # once: concurrent fetches into the bare clone would race on its locks
        batch = time.strftime("%Y%m%dT%H%M%S")
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            list(pool.map(lambda job: attempt(*job, batch), todo))
    print(summary(load()))


if __name__ == "__main__":
    main()
