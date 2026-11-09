import json
import subprocess
import sys

import pytest

import hygiene


def diff(path, added=("x",), removed=()):
    body = "".join(f"-{line}\n" for line in removed) + "".join(f"+{line}\n" for line in added)
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n{body}"


def check(text):
    return hygiene.check(hygiene.parse_diff(text))


RS = "app/crates/api/src/lib.rs"


def test_plain_app_edit_passes():
    assert check(diff(RS)) == {"passed": True, "signals": {}, "changed_lines": 1}


def test_edit_outside_app_fails():
    assert check(diff("README.md"))["signals"] == {"outside_app": ["README.md"]}


@pytest.mark.parametrize("attr", ["#[test]", "#[tokio::test]", "    #[tokio::test(flavor = \"multi_thread\")]", "#[rstest]"])
def test_deleted_test_fails(attr):
    assert check(diff(RS, added=(), removed=[attr]))["signals"] == {"deleted_tests": 1}


def test_commented_out_test_counts_as_deleted():
    assert check(diff(RS, added=["// #[test]"], removed=["#[test]"]))["signals"] == {"deleted_tests": 1}


def test_test_moved_between_files_is_not_deleted():
    moved = diff(RS, added=(), removed=["#[test]"]) + diff("app/crates/api/src/other.rs", added=["#[test]"])
    assert check(moved)["passed"]


@pytest.mark.parametrize("attr", ["#[ignore]", '#[ignore = "flaky"]', '#[cfg_attr(ci, ignore)]', "#[cfg(any())]"])
def test_skipped_test_fails(attr):
    assert check(diff(RS, added=[attr]))["signals"] == {"skipped_tests": [f"{RS}: {attr}"]}


def test_cfg_not_test_is_ordinary_production_code():
    assert check(diff(RS, added=["#[cfg(not(test))]"]))["passed"]


def test_test_attributes_only_count_in_rust_files():
    assert check(diff("app/web/notes.md", added=["#[ignore]"], removed=["#[test]"]))["passed"]


def test_diff_size_is_measured_not_failed():
    result = check(diff(RS, added=["x"] * 1000, removed=["y"] * 5))
    assert result["passed"] and result["changed_lines"] == 1005


def test_file_content_cannot_forge_a_header():
    # Content is prefixed, so these are one app/ file's lines and not a second file.
    files = hygiene.parse_diff(diff(RS, added=["diff --git a/infra/x b/infra/x"], removed=["-- note"]))
    assert [f.path for f in files] == [RS]
    assert files[0].removed == ["-- note"]


def test_quoted_path_is_refused():
    with pytest.raises(ValueError):
        hygiene.parse_diff('diff --git "a/app/x\\ty" "b/app/x\\ty"\n')


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "infra").mkdir()
    (tmp_path / "infra" / "main.tf").write_text("a\nb\nc\nd\n")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "lib.rs").write_text("#[test]\nfn t() {}\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_real_git_output_parses_with_spaces_and_empty_files(repo):
    (repo / "app" / "a file.txt").write_text("hello\n")
    (repo / "app" / "empty").write_text("")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "head")
    files = hygiene.parse_diff(hygiene.git_diff("HEAD~1", "HEAD"))
    assert sorted(f.path for f in files) == ["app/a file.txt", "app/empty"]


def test_real_git_rename_out_of_infra_shows_the_infra_path(repo):
    git(repo, "mv", "infra/main.tf", "app/main.tf")
    git(repo, "commit", "-qm", "head")
    files = hygiene.parse_diff(hygiene.git_diff("HEAD~1", "HEAD"))
    assert sorted(f.path for f in files) == ["app/main.tf", "infra/main.tf"]


def test_real_git_gitattributes_cannot_hide_a_deleted_test(repo):
    (repo / "app" / ".gitattributes").write_text("*.rs -diff\n")
    (repo / "app" / "lib.rs").write_text("fn t() {}\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "head")
    assert hygiene.check(hygiene.parse_diff(hygiene.git_diff("HEAD~1", "HEAD")))["signals"] == {"deleted_tests": 1}


def test_cli_reports_a_violation_without_failing_the_check(repo, monkeypatch, capsys):
    (repo / "infra" / "main.tf").write_text("changed\n")
    git(repo, "commit", "-qam", "head")
    monkeypatch.setattr(sys, "argv", ["hygiene.py", "--base", "HEAD~1", "--head", "HEAD"])
    assert hygiene.main() == 0
    assert json.loads(capsys.readouterr().out)["passed"] is False
