import pytest

from friction import measure


def transcript(*calls, denied=()):
    """Each call is (id, command) or (id, command, error text)."""
    events = []
    for call_id, command, *error in calls:
        use = {"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": command}}
        result = {"type": "tool_result", "tool_use_id": call_id, "content": error[0] if error else "ok"}
        if error:
            result["is_error"] = True
        events.append({"type": "assistant", "message": {"content": [use]}})
        events.append({"type": "user", "message": {"content": [result]}})
    denials = [{"tool_use_id": call_id} for call_id in denied]
    return [*events, {"type": "result", "permission_denials": denials}]


def test_counts_denials_and_tool_blocks_by_reason():
    result = measure(
        transcript(
            ("1", "mkdir -p x", "mkdir in '/w/x' needs approval."),
            ("2", "echo $X", "A variable in this command can't be checked before it runs"),
            ("3", "sleep 45; ls", "<tool_use_error>Blocked: sleep 45 followed by: ls"),
            ("4", "ls"),
            denied=("1", "2"),
        )
    )
    assert result["tool_calls"] == 4
    assert result["blocked"] == 3
    assert result["reasons"] == {"needs_approval": 1, "uncheckable_shell": 1, "tool_policy": 1}


def test_an_ordinary_command_failure_is_not_a_block():
    result = measure(transcript(("1", "cat missing", "Exit code 1")))
    assert result["blocked"] == 0
    assert result["reasons"] == {}


def test_a_blocked_required_check_does_not_count_as_completed():
    result = measure(
        transcript(
            ("1", "cargo clippy; echo $?", "A variable in this command can't be checked"),
            ("2", "cargo fmt --check && cargo test"),
            denied=("1",),
        )
    )
    assert result["required_checks_completed"] == {"fmt": True, "clippy": False, "test": True}


@pytest.mark.parametrize(
    ("calls", "denied", "expected"),
    [
        ([("1", "cargo test")], (), "not_attempted"),
        ([("1", "curl localhost:9000/health", "curl requires approval")], ("1",), "blocked"),
        ([("1", "curl 127.0.0.1:9000/health", "Exit code 7")], (), "failed"),
        (
            [("1", "curl localhost:9000/health", "curl requires approval"), ("2", "curl localhost:9000/health")],
            ("1",),
            "completed",
        ),
    ],
)
def test_live_check_states(calls, denied, expected):
    assert measure(transcript(*calls, denied=denied))["live_check"] == expected


def test_an_unrecognized_transcript_raises_rather_than_reporting_zero():
    with pytest.raises(ValueError):
        measure([])
    with pytest.raises(KeyError):
        measure([{"type": "result"}])
