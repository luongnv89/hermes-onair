"""sidecar.py: `hermes onair setup` logic with a fake uv and runner."""

from __future__ import annotations

import io
import subprocess

import pytest

from onair import adapter, sidecar

from fakes import OTHER_SHA, PINNED_SHA, FakeRunner, done, write_install

UV = "/opt/uv/bin/uv"


def which_uv(name):
    return UV if name == "uv" else None


def no_uv(name):
    return None


def is_smoke(argv):
    return argv[-1] == "--help"


def is_venv(argv):
    return argv[:2] == [UV, "venv"]


def is_install(argv):
    return argv[:3] == [UV, "pip", "install"]


def installing(data_dir, commit=None):
    """An install step that lays the pinned commit down, as uv would."""

    def respond(argv):
        write_install(sidecar.venv_dir(data_dir), commit or PINNED_SHA)
        return done(0)

    return respond


def creating_venv(data_dir):
    def respond(argv):
        write_install(sidecar.venv_dir(data_dir), commit=None)
        return done(0)

    return respond


def run_setup(data_dir, runner, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    kwargs.setdefault("which", which_uv)
    rc = sidecar.setup(data_dir, runner=runner, out=out, err=err, env={"PATH": "/bin"}, **kwargs)
    return rc, out.getvalue(), err.getvalue()


def test_fresh_setup_creates_the_venv_and_installs_the_pin(tmp_path):
    runner = (
        FakeRunner()
        .on(is_venv, creating_venv(tmp_path))
        .on(is_install, installing(tmp_path))
        .on(is_smoke, done(0))
    )
    rc, out, err = run_setup(tmp_path, runner)
    assert rc == 0, err
    venv_call, install_call, smoke_call = runner.calls
    venv = str(tmp_path / "sidecar")
    assert venv_call["argv"] == [UV, "venv", "--python", ">=3.11", venv]
    assert install_call["argv"] == [
        UV, "pip", "install", "--python", f"{venv}/bin/python", *adapter.install_args(),
    ]
    assert install_call["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert install_call["env"]["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"
    assert install_call["stdin"] is subprocess.DEVNULL
    assert smoke_call["argv"] == adapter.smoke_argv(tmp_path / "sidecar")
    assert "DROPCAST_PUBLISH" in smoke_call["env"]
    assert "✓ On Air sidecar ready" in out


def test_second_setup_is_a_no_op(tmp_path):
    write_install(sidecar.venv_dir(tmp_path))
    runner = FakeRunner().on(is_smoke, done(0))
    rc, out, err = run_setup(tmp_path, runner, which=no_uv)  # uv is not even needed
    assert rc == 0, err
    assert runner.argvs() == [adapter.smoke_argv(tmp_path / "sidecar")]
    assert "already set up" in out


def test_a_different_commit_is_reinstalled_in_the_same_venv(tmp_path):
    write_install(sidecar.venv_dir(tmp_path), OTHER_SHA)
    runner = FakeRunner().on(is_install, installing(tmp_path)).on(is_smoke, done(0))
    rc, _, err = run_setup(tmp_path, runner)
    assert rc == 0, err
    install, smoke = runner.argvs()
    assert is_install(install) and is_smoke(smoke)
    assert "--reinstall-package" in install


def test_a_venv_without_python_is_recreated_with_clear(tmp_path):
    (tmp_path / "sidecar").mkdir()
    runner = (
        FakeRunner()
        .on(is_venv, creating_venv(tmp_path))
        .on(is_install, installing(tmp_path))
        .on(is_smoke, done(0))
    )
    rc, _, err = run_setup(tmp_path, runner)
    assert rc == 0, err
    assert runner.argvs()[0][-2:] == ["--clear", str(tmp_path / "sidecar")]


def test_a_broken_smoke_triggers_a_reinstall(tmp_path):
    write_install(sidecar.venv_dir(tmp_path))
    smokes = iter([done(1), done(0)])
    runner = FakeRunner().on(is_smoke, lambda a: next(smokes)).on(is_install, done(0))
    rc, _, err = run_setup(tmp_path, runner)
    assert rc == 0, err
    assert sum(is_install(a) for a in runner.argvs()) == 1


def test_missing_uv_is_a_clear_error(tmp_path):
    rc, out, err = run_setup(tmp_path, FakeRunner(), which=no_uv)
    assert rc == 2
    assert "uv not found: uv is not on PATH" in err
    assert "https://docs.astral.sh/uv/" in err
    assert not (tmp_path / "sidecar").exists()


def test_configured_uv_must_exist(tmp_path):
    rc, _, err = run_setup(tmp_path, FakeRunner(), uv_bin="/nowhere/uv", which=no_uv)
    assert rc == 2
    assert "'/nowhere/uv' is not an executable" in err


def test_configured_uv_path_is_used(tmp_path):
    uv = tmp_path / "my-uv"
    uv.write_text("")
    uv.chmod(0o755)
    assert sidecar.find_uv(str(uv), which=no_uv) == str(uv)
    assert sidecar.find_uv("", which=which_uv) == UV


def test_python_request_is_passed_to_uv_venv(tmp_path):
    runner = (
        FakeRunner()
        .on(lambda a: a[:2] == [UV, "venv"], creating_venv(tmp_path))
        .on(is_install, installing(tmp_path))
        .on(is_smoke, done(0))
    )
    rc, _, _ = run_setup(tmp_path, runner, python_request="3.12")
    assert rc == 0
    assert runner.argvs()[0][2:4] == ["--python", "3.12"]


@pytest.mark.parametrize(
    "stderr",
    [
        "git@github.com: Permission denied (publickey).\n"
        "fatal: Could not read from remote repository.",
        "ERROR: Repository not found.",
        "ssh: Could not resolve hostname github.com: Temporary failure in name resolution",
        "error: Git operation failed\n  Caused by: failed to clone into: /cache",
    ],
)
def test_unreachable_repo_is_a_clear_error(tmp_path, stderr):
    write_install(sidecar.venv_dir(tmp_path), commit=None)
    runner = FakeRunner().on(is_install, done(1, "", stderr))
    rc, _, err = run_setup(tmp_path, runner)
    assert rc == 1
    assert "DropCast repository unavailable" in err
    assert adapter.install_spec() in err
    assert "ssh -T git@github.com" in err


def test_other_install_failures_show_uv_output(tmp_path):
    write_install(sidecar.venv_dir(tmp_path), commit=None)
    runner = FakeRunner().on(is_install, done(1, "", "No solution found when resolving"))
    rc, _, err = run_setup(tmp_path, runner)
    assert rc == 1
    assert "uv could not install DropCast (exit 1)" in err
    assert "No solution found" in err


def test_venv_failure_stops_before_installing(tmp_path):
    runner = FakeRunner().on(is_venv, done(2, "", "No interpreter found for Python >=3.11"))
    rc, _, err = run_setup(tmp_path, runner)
    assert rc == 1
    assert "could not create the sidecar venv" in err
    assert len(runner.calls) == 1


def test_timeouts_and_missing_binaries_are_reported(tmp_path):
    runner = FakeRunner().on(is_venv, subprocess.TimeoutExpired("uv", 1))
    assert run_setup(tmp_path, runner)[0] == 1
    runner = FakeRunner().on(is_venv, FileNotFoundError(2, "No such file"))
    rc, _, err = run_setup(tmp_path, runner)
    assert rc == 1 and "could not run uv" in err


def test_install_that_does_not_land_the_pin_fails(tmp_path):
    write_install(sidecar.venv_dir(tmp_path), commit=None)
    runner = FakeRunner().on(is_install, done(0))
    rc, _, err = run_setup(tmp_path, runner)
    assert rc == 1
    assert "sidecar check failed" in err


def test_existing_git_ssh_command_is_kept(tmp_path):
    write_install(sidecar.venv_dir(tmp_path), commit=None)
    runner = FakeRunner().on(is_install, installing(tmp_path)).on(is_smoke, done(0))
    out, err = io.StringIO(), io.StringIO()
    env = {"GIT_SSH_COMMAND": "ssh -i /k"}
    sidecar.setup(tmp_path, runner=runner, which=which_uv, env=env, out=out, err=err)
    assert runner.calls[0]["env"]["GIT_SSH_COMMAND"] == "ssh -i /k"


# --- --check ---


def run_check(data_dir, runner, which=which_uv):
    out = io.StringIO()
    return sidecar.check(data_dir, runner=runner, which=which, out=out), out.getvalue()


def test_check_reports_ok_without_changing_anything(tmp_path):
    write_install(sidecar.venv_dir(tmp_path))
    runner = FakeRunner().on(is_smoke, done(0))
    rc, out = run_check(tmp_path, runner, which=no_uv)
    assert rc == 0
    assert "Result: OK" in out
    assert f"(pin {adapter.DROPCAST_PIN}: ok)" in out
    assert "not found (needed only to install)" in out
    assert runner.argvs() == [adapter.smoke_argv(tmp_path / "sidecar")]


def test_check_reports_a_missing_sidecar(tmp_path):
    rc, out = run_check(tmp_path, FakeRunner())
    assert rc == 1
    assert "python:  missing" in out
    assert "not installed from a pinned commit" in out
    assert "NOT READY" in out
    assert not (tmp_path / "sidecar").exists()


def test_check_reports_a_pin_mismatch(tmp_path):
    write_install(sidecar.venv_dir(tmp_path), OTHER_SHA)
    rc, out = run_check(tmp_path, FakeRunner())
    assert rc == 1
    assert "MISMATCH" in out
    assert "smoke:   skipped" in out


def test_check_reports_a_failed_smoke(tmp_path):
    write_install(sidecar.venv_dir(tmp_path))
    rc, out = run_check(tmp_path, FakeRunner().on(is_smoke, done(3)))
    assert rc == 1
    assert "smoke:   exit 3" in out
