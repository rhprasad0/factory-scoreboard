"""Friction measurement: what did the permission checker stop a worker from doing?

Usage: friction.py claude-execution-output.json
Prints the result as JSON. It measures a trial and gates nothing. Any error, including a
transcript format it does not recognize, exits non-zero rather than reporting zero.
"""

import argparse
import json
import re
import sys

REQUIRED_CHECKS = {"fmt": "cargo fmt --check", "clippy": "cargo clippy", "test": "cargo test"}
LIVE_CHECK = re.compile(r"(?:localhost|127\.0\.0\.1):9000")
TOOL_BLOCK = "<tool_use_error>Blocked"
REASONS = {
    "uncheckable_shell": ("can't be checked", "expansion obfuscation"),
    "needs_approval": ("needs approval", "requires approval"),
    "tool_policy": (TOOL_BLOCK,),
}


def _text(content) -> str:
    if isinstance(content, list):
        return " ".join(block.get("text", "") for block in content)
    return str(content)


def _reason(error: str) -> str:
    return next((name for name, marks in REASONS.items() if any(m in error for m in marks)), "other")


def _tool_calls(events: list[dict]) -> dict:
    calls: dict = {}
    for event in events:
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if block.get("type") == "tool_use":
                command = block["input"].get("command", "") if block["name"] == "Bash" else ""
                calls[block["id"]] = {"command": command, "error": None}
            elif block.get("type") == "tool_result" and block["tool_use_id"] in calls:
                if block.get("is_error"):
                    calls[block["tool_use_id"]]["error"] = _text(block.get("content"))
    return calls


def measure(events: list[dict]) -> dict:
    results = [event for event in events if event.get("type") == "result"]
    if not results:
        raise ValueError("transcript has no result event")
    denied = {denial["tool_use_id"] for result in results for denial in result["permission_denials"]}

    calls = _tool_calls(events)
    blocked = [
        call
        for call_id, call in calls.items()
        if call_id in denied or TOOL_BLOCK in (call["error"] or "")
    ]
    completed = [call["command"] for call in calls.values() if call["error"] is None]

    reasons: dict = {}
    for call in blocked:
        reason = _reason(call["error"] or "")
        reasons[reason] = reasons.get(reason, 0) + 1

    if any(LIVE_CHECK.search(command) for command in completed):
        live_check = "completed"
    elif any(LIVE_CHECK.search(call["command"]) for call in blocked):
        live_check = "blocked"
    elif any(LIVE_CHECK.search(call["command"]) for call in calls.values()):
        live_check = "failed"
    else:
        live_check = "not_attempted"

    return {
        "tool_calls": len(calls),
        "blocked": len(blocked),
        "reasons": reasons,
        # A pipe into tail can hide a failure, so True means not blocked, not passed.
        # Whether the checks pass is the build grader's job.
        "required_checks_completed": {
            name: any(check in command for command in completed)
            for name, check in REQUIRED_CHECKS.items()
        },
        "live_check": live_check,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transcript")
    args = parser.parse_args()
    with open(args.transcript, encoding="utf-8") as transcript:
        print(json.dumps(measure(json.load(transcript)), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
