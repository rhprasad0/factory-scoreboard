import json
from pathlib import Path

import jsonschema
import pytest

import record


def trial(**overrides):
    return {
        "task": "T-001",
        "trial": 1,
        "config_ref": "main",
        "base_commit": "a" * 40,
        "pr": 6,
        "turns": 12,
        "wall_clock_seconds": 300.5,
        "graders": {
            "build": "pass",
            "pass_to_pass": "pass",
            "oracle": "fail",
            "hygiene": "pass",
            "scope": None,
            "human": {"merged": True, "review_minutes": 4, "comments": 0},
        },
        **overrides,
    }


def test_each_trial_is_one_line(tmp_path):
    out = tmp_path / "records.jsonl"
    record.append_record(out, trial(trial=1))
    record.append_record(out, trial(trial=2))
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert [line["trial"] for line in lines] == [1, 2]
    assert lines[0]["schema_version"] == 1 and lines[0]["recorded_at"]
    assert lines[0]["graders"]["oracle"] == "fail"


@pytest.mark.parametrize(
    "bad",
    [
        {"task": "4"},
        {"trial": 0},
        {"trial": True},
        {"base_commit": "abc"},
        {"commit": "abc"},
        {"surprise": 1},
        {"graders": {"build": "pass"}},
    ],
)
def test_invalid_trial_writes_nothing(tmp_path, bad):
    out = tmp_path / "records.jsonl"
    with pytest.raises(jsonschema.ValidationError):
        record.append_record(out, trial(**bad))
    assert not out.exists()


def test_no_pr_trial_names_its_commit(tmp_path):
    out = tmp_path / "records.jsonl"
    rec = record.append_record(out, trial(pr=None, commit="c" * 40))
    assert rec["pr"] is None and rec["commit"] == "c" * 40


def test_nan_writes_nothing(tmp_path):
    out = tmp_path / "records.jsonl"
    with pytest.raises(ValueError):
        record.append_record(out, trial(wall_clock_seconds=float("nan")))
    assert not out.exists()


def test_task_manifest_schema():
    schema = json.loads((Path(__file__).parent / "tasks" / "schema.json").read_text())
    manifest = {
        "base_commit": "a" * 40,
        "spec_sha": "b" * 64,
        "critical": False,
        "created": "2026-11-09",
        "source_issue": 4,
    }
    jsonschema.validate(manifest, schema)
    for bad in ({"critical": "no"}, {"spec_sha": "b"}, {"source_issue": 0}, {"extra": 1}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**manifest, **bad}, schema)
