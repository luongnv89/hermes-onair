"""adapter.py: pin and install surface, result parsing, run/resume contract."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from onair import adapter
from onair.adapter import AdapterError, JobStatus

from fakes import OTHER_SHA, PINNED_SHA, FakeRunner, done, write_install


def result_json(status="needs_agent", **overrides) -> str:
    data = {
        "version": 1,
        "status": status,
        "step": "script",
        "job": "/data/jobs/2026-11-05-abc",
        "message": "write the script",
        "reply": None,
        "next": "dropcast resume /data/jobs/2026-11-05-abc",
        "steps": {"ingest": "done"},
        "request": {},
        "notes": [],
    }
    data.update(overrides)
    return json.dumps(data, indent=2)


# --- install surface ---


def test_install_spec_is_the_pinned_git_ssh_url_with_extras():
    spec = adapter.install_spec()
    assert spec == f"dropcast[edgetts] @ git+ssh://git@github.com/luongnv89/dropcast@{adapter.DROPCAST_PIN}"
    assert adapter.install_args() == ["--reinstall-package", "dropcast", spec]


def test_smoke_argv_runs_the_sidecar_executable(tmp_path):
    assert adapter.smoke_argv(tmp_path) == [str(tmp_path / "bin" / "dropcast"), "--help"]


def test_installed_commit_reads_direct_url(tmp_path):
    write_install(tmp_path, PINNED_SHA)
    assert adapter.installed_commit(tmp_path) == PINNED_SHA
    assert adapter.pin_matches(PINNED_SHA)


@pytest.mark.parametrize(
    "record",
    [
        {"url": "file:///src/dropcast", "dir_info": {}},  # a local install, not the pin
        {"vcs_info": {"commit_id": adapter.DROPCAST_PIN}},  # short SHA: not what uv writes
        {"vcs_info": {"commit_id": "Z" * 40}},
        [],
    ],
)
def test_installed_commit_rejects_non_vcs_records(tmp_path, record):
    write_install(tmp_path, commit=None)
    info = tmp_path / "lib" / "python3.12" / "site-packages" / "dropcast-0.2.0.dist-info"
    info.mkdir(parents=True)
    (info / "direct_url.json").write_text(json.dumps(record))
    assert adapter.installed_commit(tmp_path) is None


def test_installed_commit_tolerates_missing_and_broken_files(tmp_path):
    assert adapter.installed_commit(tmp_path) is None
    info = tmp_path / "lib" / "python3.12" / "site-packages" / "dropcast-0.2.0.dist-info"
    info.mkdir(parents=True)
    (info / "direct_url.json").write_text("{not json")
    assert adapter.installed_commit(tmp_path) is None


@pytest.mark.parametrize(
    ("commit", "expected"),
    [
        (PINNED_SHA, True),
        (OTHER_SHA, False),
        (None, False),
        ("", False),
        (adapter.DROPCAST_PIN, False),
    ],
)
def test_pin_matches_needs_a_full_sha_starting_with_the_pin(commit, expected):
    assert adapter.pin_matches(commit) is expected


# --- result parsing and statuses ---


@pytest.mark.parametrize("status", [s.value for s in JobStatus])
def test_parse_result_accepts_every_contract_status(status):
    result = adapter.parse_result(result_json(status))
    assert result.status is JobStatus(status)
    assert result.job == "/data/jobs/2026-11-05-abc"
    assert result.raw["version"] == 1


def test_status_sets_follow_the_contract():
    assert {s.value for s in JobStatus} == {
        "needs_agent", "needs_approval", "rendering", "delivered", "stopped", "failed",
    }
    assert {s for s in JobStatus if s.terminal} == {JobStatus.DELIVERED, JobStatus.STOPPED}
    needs_input = {s for s in JobStatus if s.needs_input}
    assert needs_input == {JobStatus.NEEDS_AGENT, JobStatus.NEEDS_APPROVAL}
    assert not JobStatus.RENDERING.terminal and not JobStatus.RENDERING.needs_input
    # Resumability depends on the result, not the status: no status-level set.
    assert not hasattr(JobStatus.FAILED, "resumable")
    assert not hasattr(adapter, "RESUMABLE")


# --- resumability, one test per `next` shape the pinned DropCast's run.py builds ---

JOB = "/data/jobs/2026-11-05-abc"


def shape(status, step, next_, **extra):
    return adapter.parse_result(result_json(status, step=step, next=next_, **extra))


def test_declined_approval_stop_is_resumable_with_free():
    # run.py:1403-1410: Wait("stopped", "... run dropcast resume JOB --answer yes (or --answer
    # free)", approval=decision); the result's next is the bare resume (run.py:2314).
    result = shape(
        "stopped", "approval", f"dropcast resume {JOB}",
        approval={"status": "declined", "allowed": False},
        message=f"Stopped: the paid render was declined and nothing was spent. The job is kept; "
        f"to continue run dropcast resume {JOB} --answer yes (or --answer free).",
    )
    assert result.status.terminal  # nothing happens without a new decision ...
    assert result.resumable and result.resume_blocker is None  # ... which may be free


def test_headless_stop_without_a_free_option_is_blocked():
    # run.py:1484-1491 (+ 1443-1454): the free engine cannot be used; the message asks for
    # a budget (`--approve-budget N`) or a shorter --length.
    result = shape(
        "stopped", "approval", f"dropcast resume {JOB}",
        approval={"status": "needs_approval", "allowed": False},
        headless={"budget_credits": 0, "budget_usd": 0, "budget_origin": "flag",
                  "outcome": "stopped", "reason": "no budget"},
        message="Headless run stopped before rendering: ... To render, approve a budget: "
        f"dropcast resume {JOB} --approve-budget 120",
    )
    assert not result.resumable
    assert result.resume_blocker.startswith("headless run stopped")


def test_failed_ingest_with_a_job_is_blocked():
    # run.py:2283-2298: on resume, ingest never finished; next = request["retry"], the
    # original `dropcast run ...` (run.py:3105-3123), so a resume would start over.
    result = shape(
        "failed", "ingest",
        "dropcast run --adapter headless --headless --no-publish https://example.com/a",
    )
    assert result.job == JOB
    assert result.resume_blocker == "next starts over instead of resuming: dropcast run"


def test_failed_ingest_without_a_job_is_blocked():
    result = shape("failed", "ingest", "dropcast run https://x", job=None)
    assert result.resume_blocker.startswith("no job folder")


def test_failed_render_is_resumable_not_terminal():
    # run.py:2335-2344: a failed step keeps the job; next = `dropcast resume JOB`.
    result = shape("failed", "render", f"dropcast resume {JOB}")
    assert not result.status.terminal
    assert result.resumable


def test_needs_approval_is_resumable_with_free_or_no():
    # run.py:1415: answer_command = dropcast resume JOB --answer 'yes|no|free'.
    result = shape("needs_approval", "approval", f"dropcast resume {JOB} --answer 'yes|no|free'")
    assert result.resumable


@pytest.mark.parametrize(
    ("next_", "blocker"),
    [
        (f"dropcast resume {JOB} --answer yes", "needs --answer yes"),
        (f"dropcast resume {JOB} --answer=yes", "needs --answer yes"),
        (f"dropcast resume {JOB} --approve-budget 120", "needs a budget (--approve-budget)"),
        (f"dropcast resume {JOB} --approve-budget-usd=0.42", "needs a budget"),
        (f"dropcast resume {JOB} --engine elevenlabs", "needs --engine elevenlabs"),
        (f"dropcast resume {JOB} --engine=openrouter", "needs --engine openrouter"),
        ("dropcast resume 'unclosed", "not a command line"),
    ],
)
def test_resumes_needing_refused_flags_are_blocked(next_, blocker):
    result = shape("needs_approval", "approval", next_)
    assert blocker in result.resume_blocker


def test_resumable_shapes_rendering_needs_agent_and_story_groups():
    assert shape("rendering", "render", f"dropcast resume {JOB}").resumable
    assert shape("needs_agent", "script", f"dropcast resume {JOB}").resumable
    # run.py:2844: a story group offers --answer 'yes|free'.
    assert shape("stopped", "approval", f"dropcast resume {JOB} --answer 'yes|free'",
                 approval={"status": "declined"}).resumable
    assert shape("rendering", "render", f"dropcast resume {JOB} --engine edgetts").resumable


def test_delivered_and_missing_next_are_not_resumable():
    assert shape("delivered", "reply", None).resume_blocker == "the job is delivered"
    assert shape("needs_agent", "script", None).resume_blocker == "DropCast offers no next command"


def test_parse_result_reads_a_delivered_reply():
    reply = {"adapter": "headless", "text": "Your episode is ready", "attachments": ["/x/ep.mp3"]}
    result = adapter.parse_result(result_json("delivered", step="reply", reply=reply, next=None))
    assert result.status.terminal
    assert result.reply["attachments"] == ["/x/ep.mp3"]
    assert result.next is None


def test_parse_result_skips_stray_lines_before_the_json():
    result = adapter.parse_result("warming up\n" + result_json("rendering"))
    assert result.status is JobStatus.RENDERING


@pytest.mark.parametrize(
    ("stdout", "message"),
    [
        ("", "no result"),
        ("not json at all", "not JSON"),
        ("[1, 2]", "not a JSON object"),
        (json.dumps({"status": "delivered"}), "lacks"),
        (result_json("exploded"), "unknown DropCast status"),
        (result_json("delivered", reply="text"), "malformed reply"),
        (result_json(version=2), "unsupported DropCast result version: 2"),
        (result_json(version="1"), "unsupported DropCast result version"),
        (result_json(version=True), "unsupported DropCast result version"),
        (result_json(step="mix"), "unknown DropCast step"),
        (result_json(job=7), "malformed job"),
        (result_json(next=["dropcast", "resume"]), "malformed next"),
        (result_json(message=""), "malformed message"),
        (result_json(reply={"text": "hi", "attachments": []}), "malformed reply"),
        (result_json(reply={"adapter": "headless", "attachments": []}), "malformed reply"),
        (result_json(reply={"adapter": "headless", "text": ""}), "malformed reply"),
        (result_json(reply={"adapter": "h", "text": "t", "attachments": "a"}), "malformed reply"),
        (result_json(reply={"adapter": "h", "text": "t", "attachments": [""]}), "malformed reply"),
        (result_json(steps=[]), "malformed steps or request"),
        (result_json(request=None), "malformed steps or request"),
        (result_json(notes="n"), "malformed notes"),
        (result_json(notes=[1]), "malformed notes"),
    ],
)
def test_parse_result_rejects_output_outside_the_contract(stdout, message):
    with pytest.raises(AdapterError, match=message):
        adapter.parse_result(stdout)


@pytest.mark.parametrize(
    "key", ["version", "status", "step", "job", "message", "reply", "next", "steps", "request",
            "notes"],
)
def test_parse_result_requires_every_schema_key(key):
    data = json.loads(result_json())
    del data[key]
    with pytest.raises(AdapterError, match=f"lacks: {key}"):
        adapter.parse_result(json.dumps(data))


# --- run / resume ---


def _safe(argv: list[str]) -> None:
    joined = " ".join(argv)
    assert "--adapter headless --headless" in joined
    assert "--no-publish" in argv
    assert "--approve-budget 0 --approve-budget-usd 0" in joined
    assert "--engine edgetts" in joined
    assert "--publish" not in argv


def test_run_argv_is_headless_free_and_unpublished(tmp_path):
    argv = adapter.build_run_argv(tmp_path, Path("/r"), link="https://example.com/a")
    assert argv[:2] == [str(tmp_path / "bin" / "dropcast"), "run"]
    assert argv[argv.index("--root") + 1] == "/r"
    assert argv[-2:] == ["--", "https://example.com/a"]
    _safe(argv)


def test_run_argv_reads_text_from_stdin_never_argv(tmp_path):
    argv = adapter.build_run_argv(tmp_path, Path("/r"), text="--looks like a flag")
    assert argv[-2:] == ["--text", "-"]
    assert "--looks like a flag" not in " ".join(argv)
    _safe(argv)


def test_run_pipes_text_to_stdin_whatever_its_size(tmp_path):
    text = "word " * 100_000  # 500 KB: far above Linux's 128 KiB per-argument cap
    runner = FakeRunner().on(lambda a: a[1] == "run", done(1, result_json()))
    adapter.run(tmp_path, Path("/r"), text=text, runner=runner, env={})
    call = runner.calls[0]
    assert call["input"] == text
    assert "stdin" not in call  # subprocess.run refuses stdin= together with input=
    assert max(len(a) for a in call["argv"]) < 1000


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"link": "a", "text": "b"}, {"link": ""}, {"text": "  "}, {"link": "-"}],
)
def test_run_argv_refuses_bad_sources(tmp_path, kwargs):
    with pytest.raises(AdapterError):
        adapter.build_run_argv(tmp_path, Path("/r"), **kwargs)


@pytest.mark.parametrize("answer", [None, "no", "free"])
def test_resume_argv_is_headless_free_and_unpublished(tmp_path, answer):
    argv = adapter.build_resume_argv(tmp_path, "/r/jobs/j1", Path("/r"), answer)
    assert argv[1] == "resume"
    assert argv[-2:] == ["--", "/r/jobs/j1"]
    assert ("--answer" in argv) is (answer is not None)
    _safe(argv)


@pytest.mark.parametrize("answer", ["yes", "YES", "maybe", ""])
def test_resume_never_approves_spending(tmp_path, answer):
    with pytest.raises(AdapterError, match="not allowed"):
        adapter.build_resume_argv(tmp_path, "/r/jobs/j1", Path("/r"), answer)


def test_child_env_scrubs_paid_and_publish_knobs():
    base = {name: "x" for name in adapter.SCRUBBED_ENV} | {"PATH": "/bin", "DROPCAST_PUBLISH": "1"}
    env = adapter.child_env(base)
    assert env == {"PATH": "/bin", "DROPCAST_PUBLISH": "0"}
    assert {
        "OPENROUTER_API_KEY", "ELEVENLABS_API_KEY", "DROPCAST_PUBLISH_CMD",
        "DROPCAST_APPROVE_BUDGET", "DROPCAST_APPROVE_BUDGET_USD",
    } <= adapter.SCRUBBED_ENV


@pytest.mark.parametrize(("code", "status"), [(0, "delivered"), (1, "needs_agent"), (1, "stopped")])
def test_run_parses_exit_0_and_1(tmp_path, code, status):
    runner = FakeRunner().on(lambda a: a[1] == "run", done(code, result_json(status)))
    result = adapter.run(tmp_path, Path("/r"), link="https://example.com/a", runner=runner,
                         env={"PATH": "/bin", "OPENROUTER_API_KEY": "k"})
    assert result.status is JobStatus(status)
    call = runner.calls[0]
    assert call["env"] == {"PATH": "/bin", "DROPCAST_PUBLISH": "0"}
    assert call["stdin"] is subprocess.DEVNULL
    assert "input" not in call
    assert call["check"] is False


def test_resume_passes_through_the_runner(tmp_path):
    runner = FakeRunner().on(lambda a: a[1] == "resume", done(1, result_json("needs_approval")))
    result = adapter.resume(tmp_path, "/r/jobs/j1", Path("/r"), "free", runner=runner, env={})
    assert result.status.needs_input
    assert runner.argvs()[0][-4:] == ["--answer", "free", "--", "/r/jobs/j1"]


def test_usage_error_raises_with_the_last_stderr_line(tmp_path):
    runner = FakeRunner().on(lambda a: True, done(2, "", "Traceback…\nerror: unknown engine"))
    with pytest.raises(AdapterError, match=r"exit 2\): error: unknown engine"):
        adapter.run(tmp_path, Path("/r"), link="x", runner=runner, env={})


@pytest.mark.parametrize(
    "error",
    [
        PermissionError(13, "Permission denied"),
        OSError(7, "Argument list too long"),  # E2BIG
        OSError(8, "Exec format error"),
    ],
)
def test_other_os_errors_are_adapter_errors(tmp_path, error):
    runner = FakeRunner().on(lambda a: True, error)
    with pytest.raises(AdapterError, match=f"could not start: {error.strerror}"):
        adapter.run(tmp_path, Path("/r"), link="x", runner=runner, env={})
    with pytest.raises(AdapterError, match="could not start"):
        adapter.resume(tmp_path, "j", Path("/r"), runner=runner, env={})


def test_missing_executable_and_timeout_are_adapter_errors(tmp_path):
    runner = FakeRunner().on(lambda a: True, FileNotFoundError("dropcast"))
    with pytest.raises(AdapterError, match="hermes onair setup"):
        adapter.run(tmp_path, Path("/r"), link="x", runner=runner, env={})
    runner = FakeRunner().on(lambda a: True, subprocess.TimeoutExpired("dropcast", 1))
    with pytest.raises(AdapterError, match="did not answer"):
        adapter.resume(tmp_path, "j", Path("/r"), runner=runner, env={})


# --- smoke (every engine process starts from adapter.py) ---


def test_smoke_runs_help_with_the_scrubbed_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    runner = FakeRunner().on(lambda a: True, done(0))
    assert adapter.smoke(tmp_path, runner) == (True, "ok")
    call = runner.calls[0]
    assert call["argv"] == adapter.smoke_argv(tmp_path)
    assert "ELEVENLABS_API_KEY" not in call["env"] and call["env"]["DROPCAST_PUBLISH"] == "0"
    assert call["stdin"] is subprocess.DEVNULL


@pytest.mark.parametrize(
    ("respond", "detail"),
    [
        (done(3), "exit 3"),
        (subprocess.TimeoutExpired("dropcast", 1), "no answer within"),
        (PermissionError(13, "Permission denied"), "could not start: PermissionError"),
        (FileNotFoundError(2, "No such file"), "could not start: FileNotFoundError"),
    ],
)
def test_smoke_reports_failures_without_raising(tmp_path, respond, detail):
    ok, text = adapter.smoke(tmp_path, FakeRunner().on(lambda a: True, respond))
    assert not ok and text.startswith(detail)


def test_installed_commit_needs_the_dropcast_repo_url(tmp_path):
    write_install(tmp_path, PINNED_SHA)
    record = next(tmp_path.glob("lib/*/site-packages/dropcast-*.dist-info/direct_url.json"))
    data = json.loads(record.read_text())
    for url in (
        "ssh://git@github.com/luongnv89/dropcast.git",
        "git+ssh://git@github.com/luongnv89/dropcast/",
    ):
        data["url"] = url
        record.write_text(json.dumps(data))
        assert adapter.installed_commit(tmp_path) == PINNED_SHA, url
    data["url"] = "ssh://git@github.com/someone-else/dropcast"
    record.write_text(json.dumps(data))
    assert adapter.installed_commit(tmp_path) is None
