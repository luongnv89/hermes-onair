"""Setup only ever writes to a sidecar it owns (review finding 1).

The fake-runner tests prove setup refuses before any process starts. The
``real_uv`` tests drive the real ``uv venv`` (offline) to show what a rebuild
of On Air's own, marked venv does and does not touch.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from onair import sidecar

from fakes import PINNED_SHA, FakeRunner, done, write_install

UV = "/opt/uv/bin/uv"


def snapshot(root: Path) -> dict[str, str]:
    """Every file and link under *root*, without following links."""
    seen = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                seen[rel] = "-> " + os.readlink(path)
            elif path.is_file():
                seen[rel] = path.read_text(errors="replace")
    return seen


def venv_looking(path: Path, planted: str) -> None:
    """A dir uv would call a virtual environment, with no python and a planted file."""
    (path / "bin").mkdir(parents=True)
    (path / "pyvenv.cfg").write_text("home = /usr/bin\nversion_info = 3.12.0\n")
    (path / planted).write_text("do not delete\n")


def run_setup(data_dir, runner=None, which=lambda name: UV):
    out, err = io.StringIO(), io.StringIO()
    runner = runner or FakeRunner()  # no rules: any process start fails the test
    rc = sidecar.setup(data_dir, runner=runner, which=which, env={"PATH": "/bin"},
                       out=out, err=err)
    return rc, out.getvalue(), err.getvalue()


def run_check(data_dir):
    out = io.StringIO()
    rc = sidecar.check(data_dir, runner=FakeRunner(), which=lambda n: None, out=out)
    return rc, out.getvalue()


# --- refused before anything runs (reviewer scenarios A-E) ---


def test_symlink_to_a_venv_looking_dir_is_refused(tmp_path):  # scenario D
    data, other = tmp_path / "data", tmp_path / "othervenv"
    data.mkdir()
    venv_looking(other, "precious.txt")
    (other / ".onair-sidecar").write_text("planted marker\n")  # even a marker does not help
    (data / "sidecar").symlink_to(other)
    before = snapshot(tmp_path)
    rc, _, err = run_setup(data)
    assert rc == 1
    assert "is a symbolic link" in err and "Move it aside" in err
    assert snapshot(tmp_path) == before


def test_symlink_to_a_plain_dir_is_refused(tmp_path):  # scenario A
    data, victim = tmp_path / "data", tmp_path / "victim"
    data.mkdir()
    (victim / "inner").mkdir(parents=True)
    (victim / "keep.txt").write_text("keep\n")
    (victim / "inner" / "deep.txt").write_text("deep\n")
    (data / "sidecar").symlink_to(victim)
    before = snapshot(tmp_path)
    assert run_setup(data)[0] == 1
    assert snapshot(tmp_path) == before


def test_foreign_venv_at_the_plugin_path_is_refused(tmp_path):  # scenario E
    venv = tmp_path / "sidecar"
    venv_looking(venv, "foreign.txt")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "keep.txt").write_text("keep\n")
    (venv / "link-out").symlink_to(tmp_path / "outside")
    before = snapshot(tmp_path)
    rc, _, err = run_setup(tmp_path)
    assert rc == 1
    assert "was not created by On Air (no .onair-sidecar)" in err
    assert snapshot(tmp_path) == before


def test_foreign_plain_dir_is_refused(tmp_path):  # scenario B
    venv = tmp_path / "sidecar"
    (venv / "sub").mkdir(parents=True)
    (venv / "notes.txt").write_text("notes\n")
    (venv / "sub" / "x.txt").write_text("x\n")
    before = snapshot(tmp_path)
    assert run_setup(tmp_path)[0] == 1
    assert snapshot(tmp_path) == before


def test_unmarked_dir_with_an_inner_symlink_is_refused(tmp_path):  # scenario C
    data, victim = tmp_path / "data", tmp_path / "victim"
    (data / "sidecar" / "bin").mkdir(parents=True)
    victim.mkdir()
    (victim / "keep.txt").write_text("keep\n")
    (data / "sidecar" / "lib").symlink_to(victim)
    before = snapshot(tmp_path)
    assert run_setup(data)[0] == 1
    assert snapshot(tmp_path) == before


def test_a_file_at_the_sidecar_path_is_refused(tmp_path):
    (tmp_path / "sidecar").write_text("not a dir\n")
    assert run_setup(tmp_path)[0] == 1
    assert (tmp_path / "sidecar").read_text() == "not a dir\n"


def test_a_symlinked_marker_does_not_make_a_dir_ours(tmp_path):
    venv = tmp_path / "sidecar"
    venv_looking(venv, "foreign.txt")
    (tmp_path / "elsewhere").write_text("x\n")
    (venv / ".onair-sidecar").symlink_to(tmp_path / "elsewhere")
    assert sidecar.ownership(venv) == sidecar.FOREIGN
    assert run_setup(tmp_path)[0] == 1
    with pytest.raises(OSError):
        sidecar.write_marker(venv)  # O_NOFOLLOW: never writes through it
    assert (tmp_path / "elsewhere").read_text() == "x\n"


def test_a_venv_with_another_install_record_is_foreign(tmp_path):
    venv = tmp_path / "sidecar"
    write_install(venv, PINNED_SHA, marker=False)
    record = next(venv.glob("lib/*/site-packages/dropcast-*.dist-info/direct_url.json"))
    record.write_text('{"url": "file:///src/dropcast", "dir_info": {}}')
    assert sidecar.ownership(venv) == sidecar.FOREIGN
    assert run_setup(tmp_path)[0] == 1


def test_check_is_read_only_and_flags_unsafe_paths(tmp_path):
    data, other = tmp_path / "data", tmp_path / "othervenv"
    data.mkdir()
    venv_looking(other, "precious.txt")
    (data / "sidecar").symlink_to(other)
    before = snapshot(tmp_path)
    rc, out = run_check(data)
    assert rc == 1
    assert "path:    UNSAFE: it is a symbolic link" in out
    assert "move the path aside" in out
    assert snapshot(tmp_path) == before


# --- pre-marker sidecars (v0.1.0) are adopted, never confused with foreign dirs ---


def test_pre_marker_sidecar_is_reported_by_check_without_writing(tmp_path):
    venv = tmp_path / "sidecar"
    write_install(venv, PINNED_SHA, marker=False)
    out = io.StringIO()
    rc = sidecar.check(tmp_path, runner=FakeRunner().on(lambda a: True, done(0)),
                       which=lambda n: None, out=out)
    assert rc == 0
    assert "owner:   On Air, from before .onair-sidecar (setup adopts it)" in out.getvalue()
    assert not (venv / sidecar.MARKER).exists()


def test_setup_adopts_a_pre_marker_sidecar(tmp_path):
    venv = tmp_path / "sidecar"
    write_install(venv, PINNED_SHA, marker=False)
    before = snapshot(venv)
    rc, out, err = run_setup(tmp_path, FakeRunner().on(lambda a: a[-1] == "--help", done(0)))
    assert rc == 0, err
    assert "Adopted the existing sidecar" in out and "already set up" in out
    after = snapshot(venv)
    assert after.pop(sidecar.MARKER).startswith("Created by the hermes-onair plugin")
    assert after == before


# --- what a rebuild of On Air's own venv does (fake and real uv) ---


def test_only_a_marked_venv_is_cleared(tmp_path):
    venv = tmp_path / "sidecar"
    write_install(venv, commit=None, python=False)  # marked, broken
    runner = FakeRunner().on(lambda a: a[1] == "venv", done(1, "", "stop here"))
    run_setup(tmp_path, runner)
    assert runner.argvs()[0][-2:] == ["--clear", str(venv)]
    # An absent sidecar is created without --clear.
    runner = FakeRunner().on(lambda a: a[1] == "venv", done(1, "", "stop here"))
    run_setup(tmp_path / "fresh", runner)
    assert "--clear" not in runner.argvs()[0]


def hybrid_runner(data_dir: Path, uv_env: dict[str, str]):
    """The real ``uv venv``; a fake install that records the pin; a fake smoke."""
    calls = []

    def runner(argv, **kwargs):
        calls.append([str(a) for a in argv])
        if argv[1:2] == ["venv"]:
            return subprocess.run(argv, **{**kwargs, "env": uv_env})
        if argv[1:3] == ["pip", "install"]:
            write_install(sidecar.venv_dir(data_dir), PINNED_SHA, python=False, marker=False)
            return done(0)
        return done(0)  # the smoke check

    runner.calls = calls
    return runner


@pytest.mark.real_uv
def test_real_uv_rebuilds_a_marked_broken_venv(tmp_path, real_uv_env):
    data = tmp_path / "data"
    venv = data / "sidecar"
    venv_looking(venv, "stale.txt")
    sidecar.write_marker(venv)
    runner = hybrid_runner(data, real_uv_env)
    out, err = io.StringIO(), io.StringIO()
    rc = sidecar.setup(data, runner=runner, which=shutil.which, python_request=sys.executable,
                       env={"PATH": os.environ.get("PATH", "")}, out=out, err=err)
    assert rc == 0, err.getvalue()
    assert runner.calls[0][1:2] == ["venv"] and "--clear" in runner.calls[0]
    assert (venv / "bin" / "python").exists()
    assert (venv / sidecar.MARKER).is_file()
    assert not (venv / "stale.txt").exists()  # uv cleared On Air's own venv


@pytest.mark.real_uv
def test_real_uv_does_not_follow_a_symlink_inside_the_venv(tmp_path, real_uv_env):
    data, outside = tmp_path / "data", tmp_path / "outside"
    venv = data / "sidecar"
    venv_looking(venv, "stale.txt")
    sidecar.write_marker(venv)
    outside.mkdir()
    (outside / "keep.txt").write_text("keep\n")
    (venv / "link-out").symlink_to(outside)
    out, err = io.StringIO(), io.StringIO()
    rc = sidecar.setup(data, runner=hybrid_runner(data, real_uv_env), which=shutil.which,
                       python_request=sys.executable,
                       env={"PATH": os.environ.get("PATH", "")}, out=out, err=err)
    assert rc == 0, err.getvalue()
    assert (outside / "keep.txt").read_text() == "keep\n"
    assert not (venv / "link-out").exists() and not (venv / "link-out").is_symlink()


@pytest.mark.real_uv
@pytest.mark.parametrize("kind", ["symlink", "foreign"])
def test_real_uv_is_never_reached_for_dirs_that_are_not_ours(tmp_path, real_uv_env, kind):
    data = tmp_path / "data"
    data.mkdir()
    if kind == "symlink":
        venv_looking(tmp_path / "othervenv", "precious.txt")
        (data / "sidecar").symlink_to(tmp_path / "othervenv")
    else:
        venv_looking(data / "sidecar", "foreign.txt")
    before = snapshot(tmp_path)
    runner = hybrid_runner(data, real_uv_env)
    out, err = io.StringIO(), io.StringIO()
    rc = sidecar.setup(data, runner=runner, which=shutil.which, python_request=sys.executable,
                       env={"PATH": os.environ.get("PATH", "")}, out=out, err=err)
    assert rc == 1 and runner.calls == []
    assert snapshot(tmp_path) == before
