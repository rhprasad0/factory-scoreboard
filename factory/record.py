"""Append one eval record per trial to a JSONL file.

Usage: record.py --out PATH < trial.json
The input is one JSON object matching record.schema.json, minus schema_version and
recorded_at, which are stamped here. The S3 upload arrives with bench.yml in week 2.
"""

import argparse
import datetime
import json
import sys
from pathlib import Path

import jsonschema

SCHEMA_VERSION = 1
SCHEMA = json.loads((Path(__file__).parent / "record.schema.json").read_text())


def append_record(path: Path, trial: dict) -> dict:
    """Validate a trial, then append it as exactly one line. Nothing is written if invalid."""
    record = {
        **trial,
        "schema_version": SCHEMA_VERSION,
        "recorded_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
    }
    jsonschema.validate(record, SCHEMA)
    line = json.dumps(record, sort_keys=True, allow_nan=False)
    with open(path, "a", encoding="utf-8") as out:
        out.write(line + "\n")
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(append_record(args.out, json.load(sys.stdin)), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
