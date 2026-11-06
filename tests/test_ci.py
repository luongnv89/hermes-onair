"""The CI workflow stays free, minimal and pinned."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
TEXT = WORKFLOW.read_text(encoding="utf-8")
DOC = yaml.safe_load(TEXT)


def walk(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, value
            yield from walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item)


def test_triggers_are_pull_request_and_push_to_main():
    triggers = DOC.get("on", DOC.get(True))  # YAML 1.1 reads a bare `on` as True
    assert set(triggers) == {"pull_request", "push"}
    assert triggers["push"]["branches"] == ["main"]


def test_no_path_filters_and_no_continue_on_error():
    keys = {key for key, _ in walk(DOC)}
    assert not keys & {"paths", "paths-ignore", "continue-on-error"}
    assert "continue-on-error" not in TEXT


def test_permissions_are_read_only():
    assert DOC["permissions"] == {"contents": "read"}
    for job in DOC["jobs"].values():
        assert job.get("permissions", {"contents": "read"}) == {"contents": "read"}


def test_actions_are_pinned_to_full_shas_with_a_version_comment():
    uses = re.findall(r"^\s*(?:-\s*)?uses:\s*(.+)$", TEXT, flags=re.MULTILINE)
    assert uses
    for line in uses:
        assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40} # v\d+(\.\d+)*", line.strip()), line


def test_free_runner_small_matrix_and_the_offline_commands():
    (job,) = DOC["jobs"].values()
    assert job["runs-on"] == "ubuntu-latest"
    assert job["strategy"]["matrix"] == {"python-version": ["3.11", "3.13"]}
    runs = [step.get("run", "") for step in job["steps"]]
    assert "ruff check ." in runs
    assert "pytest -q -m 'not live'" in runs
    assert "pip install -r requirements-dev.txt" in " ".join(runs)
