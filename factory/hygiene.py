"""Hygiene grader: did a diff stay in app/ and leave the tests alone?

Usage: hygiene.py --base SHA --head SHA
Prints the result as JSON. Exits 0 whenever it ran, violation or not: a violation fires
hygiene_failed in critical.py, which holds the PR for Ryan's approval, and a red required
check could not be approved past. Any error exits non-zero.
"""

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field

TEST_ATTR = re.compile(r"^\s*#\[(?:\w+::)*(?:test|rstest|test_case)\b")
# shortcut: textual match on attributes, misses a test body gutted by an early return
# or wrapped in a block comment; upgrade if the week 5-6 audit finds one.
SKIP_ATTR = re.compile(r"^\s*#!?\[(?:ignore\b|cfg_attr\(.*\bignore\b|cfg\((?:any\(\)|false\b))")


@dataclass
class FileDiff:
    path: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def git_diff(base: str, head: str) -> str:
    """Return the diff in the exact form parse_diff expects."""
    return subprocess.run(
        [
            "git", "-c", "core.quotePath=false", "diff",
            # A rename out of infra/ must show the infra/ path.
            "--no-renames",
            # A committed .gitattributes must not be able to hide text as binary.
            "--text",
            "--no-color", "--no-ext-diff", "--no-textconv",
            "--src-prefix=a/", "--dst-prefix=b/",
            f"{base}...{head}",
        ],
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8", "replace")


def _path(header: str) -> str:
    rest = header.removeprefix("diff --git ")
    # With --no-renames both sides name the same file, so "a/P b/P" splits exactly.
    path = rest[2 : 2 + (len(rest) - 5) // 2]
    if rest != f"a/{path} b/{path}":
        raise ValueError(f"unparseable diff header: {header!r}")
    return path


def parse_diff(diff: str) -> list[FileDiff]:
    """Parse git_diff output. Raises ValueError on a path it cannot read exactly."""
    files: list[FileDiff] = []
    in_hunk = False
    # Not splitlines(): it also breaks on form feeds and the like inside file content.
    for line in diff.split("\n"):
        # Hunk content always has a prefix, so these two never match file content.
        if line.startswith("diff --git "):
            files.append(FileDiff(_path(line)))
            in_hunk = False
        elif line.startswith("@@ "):
            in_hunk = True
        elif in_hunk and line.startswith("+"):
            files[-1].added.append(line[1:])
        elif in_hunk and line.startswith("-"):
            files[-1].removed.append(line[1:])
    return files


def check(files: list[FileDiff]) -> dict:
    """Return which hygiene signals fired, plus the diff size as a measurement."""
    signals: dict = {}
    # shortcut: every worker edit outside app/ is a violation, add an issue label
    # such as scope:root when a task first needs to edit outside app/.
    outside = [f.path for f in files if not f.path.startswith("app/")]
    if outside:
        signals["outside_app"] = outside

    rust = [f for f in files if f.path.endswith(".rs")]
    # Net across the diff, so a test moved between files does not count as deleted.
    deleted = sum(TEST_ATTR.match(line) is not None for f in rust for line in f.removed) - sum(
        TEST_ATTR.match(line) is not None for f in rust for line in f.added
    )
    if deleted > 0:
        signals["deleted_tests"] = deleted

    skipped = [f"{f.path}: {line.strip()}" for f in rust for line in f.added if SKIP_ATTR.match(line)]
    if skipped:
        signals["skipped_tests"] = skipped

    return {
        "passed": not signals,
        "signals": signals,
        "changed_lines": sum(len(f.added) + len(f.removed) for f in files),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    args = parser.parse_args()
    result = check(parse_diff(git_diff(args.base, args.head)))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
