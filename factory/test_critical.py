import json
import sys

import pytest
import yaml

import critical
import hygiene
from test_hygiene import diff, git, repo  # noqa: F401


def classify(text, **kwargs):
    files = hygiene.parse_diff(text)
    return critical.classify(files, hygiene.check(files), **kwargs)


RS = "app/crates/api/src/lib.rs"


def test_plain_app_edit_is_not_critical():
    assert classify(diff(RS)) == []


@pytest.mark.parametrize(
    "path, signal",
    [
        ("infra/main.tf", "infra"),
        (".github/workflows/ci.yml", "factory_config"),
        ("factory/critical.py", "factory_config"),
        ("CLAUDE.md", "factory_config"),
        ("app/Cargo.toml", "dependency"),
        ("app/crates/api/Cargo.toml", "dependency"),
        ("app/Cargo.lock", "dependency"),
        ("factory/uv.lock", "dependency"),
        ("app/crates/api/src/model/record.rs", "data_model"),
    ],
)
def test_path_signals(path, signal):
    assert signal in classify(diff(path))


def test_lookalike_paths_are_not_critical():
    assert classify(diff("app/infra/notes.md")) == []
    assert classify(diff("app/crates/api/src/models.rs")) == []


def test_deleted_test_needs_approval():
    assert classify(diff(RS, added=(), removed=["#[test]"])) == ["hygiene_failed"]


def test_edit_outside_app_needs_approval():
    assert classify(diff("README.md")) == ["hygiene_failed"]


def test_critical_label():
    assert classify(diff(RS), labels=("critical",)) == ["labeled_critical"]


def test_other_labels_do_not_count():
    assert classify(diff(RS), labels=("factory:build",)) == []


def test_oversized_threshold():
    assert classify(diff(RS, added=["x"] * 400)) == []
    assert classify(diff(RS, added=["x"] * 401)) == ["oversized"]


SPEC = """
paths:
  /health:
    get:
      responses:
        "200":
          description: ok
          content:
            application/json:
              schema:
                $ref: "#/components/schemas/Health"
components:
  schemas:
    Health:
      type: object
"""


@pytest.mark.parametrize(
    "head",
    [
        SPEC.replace("/health", "/healthz"),  # renamed route
        SPEC.replace("get:", "post:"),  # method removed
        SPEC.replace('"200"', '"201"'),  # response changed
        SPEC.replace("Health:\n      type: object", "Health:\n      type: string"),  # shared schema changed
        SPEC.replace("Health:", "Status:"),  # shared schema removed
        None,  # file deleted
    ],
)
def test_breaking_openapi_changes(head):
    assert classify(diff("app/openapi.yaml"), openapi_base=SPEC, openapi_head=head) == ["api_contract"]


def test_new_route_and_component_are_not_breaking():
    head = SPEC.replace("components:", "  /records:\n    get:\n      responses: {}\ncomponents:").replace(
        "      type: object", "      type: object\n    Record:\n      type: object"
    )
    assert yaml.safe_load(head)["paths"].keys() == {"/health", "/records"}
    assert classify(diff("app/openapi.yaml"), openapi_base=SPEC, openapi_head=head) == []


def test_description_only_change_outside_responses_is_not_breaking():
    head = "info:\n  title: new\n" + SPEC
    assert classify(diff("app/openapi.yaml"), openapi_base=SPEC, openapi_head=head) == []


def test_first_openapi_file_is_not_breaking():
    assert classify(diff("app/openapi.yaml"), openapi_base=None, openapi_head=SPEC) == []


def test_malformed_openapi_never_reads_as_safe():
    with pytest.raises(yaml.YAMLError):
        classify(diff("app/openapi.yaml"), openapi_base=SPEC, openapi_head="paths: [")


def cli(monkeypatch, capsys, base, head):
    monkeypatch.setattr(sys, "argv", ["critical.py", "--base", base, "--head", head])
    assert critical.main() == 0
    return json.loads(capsys.readouterr().out)["signals"]


def test_cli_compares_openapi_at_the_merge_base(repo, monkeypatch, capsys):  # noqa: F811
    spec = repo / "app" / "openapi.yaml"
    spec.write_text(SPEC)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    git(repo, "branch", "pr")

    # main moves on, and the PR must not be blamed for that.
    spec.write_text(SPEC.replace("/health", "/moved"))
    git(repo, "commit", "-qam", "main moves")
    git(repo, "checkout", "-q", "pr")
    (repo / "app" / "lib.rs").write_text("#[test]\nfn t() {}\n// note\n")
    git(repo, "commit", "-qam", "plain edit")
    assert cli(monkeypatch, capsys, "main", "pr") == []

    spec.write_text(SPEC.replace('"200"', '"201"'))
    git(repo, "commit", "-qam", "breaks the contract")
    assert cli(monkeypatch, capsys, "main", "pr") == ["api_contract"]
