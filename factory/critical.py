"""Critical-change classifier: does this diff need Ryan's approval?

Usage: critical.py --base SHA --head SHA [--label NAME ...]
Prints {"critical": bool, "signals": [...], "hygiene": {...}} as JSON and exits 0.
Any error exits non-zero, so a diff that cannot be classified never reads as safe.
"""

import argparse
import json
import subprocess
import sys

import yaml

import hygiene

MAX_CHANGED_LINES = 400
DEPENDENCY_FILES = {"Cargo.toml", "Cargo.lock", "uv.lock"}
MODEL_PREFIX = "app/crates/api/src/model/"
OPENAPI_PATH = "app/openapi.yaml"
HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


def _routes(spec: dict) -> dict:
    return {
        (path, method): operation.get("responses")
        for path, item in (spec.get("paths") or {}).items()
        for method, operation in item.items()
        if method in HTTP_METHODS
    }


def breaks_contract(base_text: str | None, head_text: str | None) -> bool:
    """True if a route was removed or renamed, or an existing response changed."""
    if base_text is None:  # The task that first creates the file removes nothing.
        return False
    base = yaml.safe_load(base_text) or {}
    head = yaml.safe_load(head_text or "") or {}

    head_routes = _routes(head)
    if any(route not in head_routes or head_routes[route] != responses for route, responses in _routes(base).items()):
        return True

    # Responses can $ref shared schemas, so an existing component changing counts too.
    # A brand new component is fine: it is how a new route describes itself.
    head_components = head.get("components") or {}
    return any(
        (head_components.get(section) or {}).get(name) != value
        for section, entries in (base.get("components") or {}).items()
        for name, value in entries.items()
    )


def classify(
    files: list[hygiene.FileDiff],
    hygiene_result: dict,
    labels: tuple[str, ...] = (),
    openapi_base: str | None = None,
    openapi_head: str | None = None,
) -> list[str]:
    """Return the signals that fired, in PLAN.md merge-policy order. Empty means auto-merge."""
    paths = [f.path for f in files]
    # A Terraform table resource is under infra/, so the infra signal already covers it.
    fired = {
        "infra": any(p.startswith("infra/") for p in paths),
        "factory_config": any(p.startswith((".github/", "factory/")) or p == "CLAUDE.md" for p in paths),
        "dependency": any(p.rsplit("/", 1)[-1] in DEPENDENCY_FILES for p in paths),
        "data_model": any(p.startswith(MODEL_PREFIX) for p in paths),
        "api_contract": breaks_contract(openapi_base, openapi_head),
        "hygiene_failed": not hygiene_result["passed"],
        "labeled_critical": "critical" in labels,
        "oversized": hygiene_result["changed_lines"] > MAX_CHANGED_LINES,
    }
    return [name for name, hit in fired.items() if hit]


def _show(rev: str, path: str) -> str | None:
    """Return a file's text at rev, or None if it does not exist there."""
    shown = subprocess.run(["git", "show", f"{rev}:{path}"], capture_output=True)
    return shown.stdout.decode("utf-8", "replace") if shown.returncode == 0 else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--label", action="append", default=[], help="a label on the source issue, repeatable")
    args = parser.parse_args()

    files = hygiene.parse_diff(hygiene.git_diff(args.base, args.head))
    hygiene_result = hygiene.check(files)
    # The diff is against the merge base, so the OpenAPI comparison has to be too.
    merge_base = subprocess.run(
        ["git", "merge-base", args.base, args.head], check=True, capture_output=True, text=True
    ).stdout.strip()
    signals = classify(
        files, hygiene_result, tuple(args.label), _show(merge_base, OPENAPI_PATH), _show(args.head, OPENAPI_PATH)
    )
    print(json.dumps({"critical": bool(signals), "signals": signals, "hygiene": hygiene_result}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
