"""The sidecar venv behind ``hermes onair setup``.

The sidecar is a uv-made virtualenv at ``<plugin data dir>/sidecar`` holding
the pinned engine package. This module only runs uv and the commands
``adapter`` hands it; what to install and how to tell it is right is
``adapter``'s business (AGENTS.md rule 4).

Every process goes through an injected ``runner`` (``subprocess.run`` by
default) and uv is found through an injected ``which``, so tests never spawn
anything.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from . import adapter

VENV_DIRNAME = "sidecar"
DEFAULT_UV_BIN = ""
DEFAULT_SIDECAR_PYTHON = ">=3.11"

VENV_TIMEOUT_S = 300
INSTALL_TIMEOUT_S = 900
SMOKE_TIMEOUT_S = 60

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
Which = Callable[[str], "str | None"]

# What git/ssh print when the repository cannot be reached or read.
_REPO_UNAVAILABLE = re.compile(
    r"permission denied \(publickey\)|repository not found|could not read from remote"
    r"|host key verification failed|could not resolve host(name)?|connection timed out"
    r"|connection refused|network is unreachable|failed to clone|failed to fetch"
    r"|git operation failed|unable to access",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SidecarState:
    """What is on disk, and whether it is the pinned, working install."""

    venv: Path
    python_ok: bool
    commit: str | None
    pin_ok: bool
    smoke_ok: bool
    smoke_detail: str

    @property
    def ok(self) -> bool:
        return self.python_ok and self.pin_ok and self.smoke_ok


def venv_dir(data_dir: Path) -> Path:
    return Path(data_dir) / VENV_DIRNAME


def venv_python(venv: Path) -> Path:
    return Path(venv) / "bin" / "python"


def find_uv(configured: str = DEFAULT_UV_BIN, which: Which = shutil.which) -> str | None:
    """The uv executable: the configured path or name, else ``uv`` on PATH."""
    if configured:
        path = Path(configured).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        return which(configured)
    return which("uv")


def _tail(text: str | None, lines: int = 5) -> str:
    kept = [line for line in (text or "").strip().splitlines() if line.strip()][-lines:]
    return "\n".join(f"    {line}" for line in kept)


def _run(runner: Runner, argv: list[str], timeout: int, env: Mapping[str, str] | None = None):
    return runner(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=dict(env) if env is not None else None,
        check=False,
    )


def _install_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Never prompt: a missing key or unknown host fails instead of hanging."""
    env = dict(os.environ if base is None else base)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return env


def inspect(data_dir: Path, runner: Runner = subprocess.run) -> SidecarState:
    """Read the sidecar's state. Runs only the sidecar's own smoke command."""
    venv = venv_dir(data_dir)
    python_ok = venv_python(venv).is_file()
    commit = adapter.installed_commit(venv) if python_ok else None
    pin_ok = adapter.pin_matches(commit)
    smoke_ok, detail = False, "skipped"
    if python_ok and pin_ok:
        try:
            proc = _run(runner, adapter.smoke_argv(venv), SMOKE_TIMEOUT_S, adapter.child_env())
        except (OSError, subprocess.SubprocessError) as exc:
            detail = f"could not start: {exc.__class__.__name__}"
        else:
            smoke_ok = proc.returncode == 0
            detail = "ok" if smoke_ok else f"exit {proc.returncode}"
    return SidecarState(venv, python_ok, commit, pin_ok, smoke_ok, detail)


def setup(
    data_dir: Path,
    *,
    uv_bin: str = DEFAULT_UV_BIN,
    python_request: str = DEFAULT_SIDECAR_PYTHON,
    runner: Runner = subprocess.run,
    which: Which = shutil.which,
    env: Mapping[str, str] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Create or repair the sidecar. Idempotent: a good install is left alone.

    Returns 0 when the sidecar is ready, 1 when uv failed, 2 when uv is missing.
    """
    out = out or sys.stdout
    err = err or sys.stderr
    name = adapter.NAME
    state = inspect(data_dir, runner)
    if state.ok:
        print(f"✓ On Air sidecar already set up ({name} {state.commit[:12]})", file=out)
        print(f"  venv: {state.venv}", file=out)
        return 0

    uv = find_uv(uv_bin, which)
    if uv is None:
        where = f"'{uv_bin}' is not an executable" if uv_bin else "uv is not on PATH"
        print(f"✗ uv not found: {where}.", file=err)
        print(
            "  Install uv (https://docs.astral.sh/uv/) or set "
            "plugins.entries.onair.settings.uv_bin, then run `hermes onair setup` again.",
            file=err,
        )
        return 2

    venv = state.venv
    if not state.python_ok:
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        argv = [uv, "venv", "--python", python_request or DEFAULT_SIDECAR_PYTHON]
        if venv.exists():
            argv.append("--clear")
        argv.append(str(venv))
        print(f"● Creating the sidecar venv at {venv}", file=out)
        try:
            proc = _run(runner, argv, VENV_TIMEOUT_S, _install_env(env))
        except subprocess.TimeoutExpired:
            print(f"✗ uv venv did not finish within {VENV_TIMEOUT_S} s.", file=err)
            return 1
        except OSError as exc:
            print(f"✗ could not run uv ({uv}): {exc.strerror or exc}", file=err)
            return 1
        if proc.returncode != 0:
            print(f"✗ uv could not create the sidecar venv (exit {proc.returncode}):", file=err)
            print(_tail(proc.stderr), file=err)
            return 1

    spec = adapter.install_spec()
    print(f"● Installing {spec}", file=out)
    argv = [uv, "pip", "install", "--python", str(venv_python(venv)), *adapter.install_args()]
    try:
        proc = _run(runner, argv, INSTALL_TIMEOUT_S, _install_env(env))
    except subprocess.TimeoutExpired:
        print(f"✗ uv pip install did not finish within {INSTALL_TIMEOUT_S} s.", file=err)
        return 1
    except OSError as exc:
        print(f"✗ could not run uv ({uv}): {exc.strerror or exc}", file=err)
        return 1
    if proc.returncode != 0:
        if _REPO_UNAVAILABLE.search(proc.stderr or ""):
            print(f"✗ {name} repository unavailable: uv could not fetch {spec}.", file=err)
            print(
                "  Check that `ssh -T git@github.com` works here and that this "
                "GitHub account can read the repository.",
                file=err,
            )
        else:
            print(f"✗ uv could not install {name} (exit {proc.returncode}):", file=err)
        print(_tail(proc.stderr), file=err)
        return 1

    state = inspect(data_dir, runner)
    if not state.ok:
        print(f"✗ {name} installed, but the sidecar check failed:", file=err)
        _report(state, err)
        return 1
    print(f"✓ On Air sidecar ready ({name} {state.commit[:12]})", file=out)
    print(f"  venv: {state.venv}", file=out)
    return 0


def _report(state: SidecarState, out: TextIO) -> None:
    name = adapter.NAME
    print(f"  venv:    {state.venv} ({'present' if state.venv.exists() else 'missing'})", file=out)
    print(f"  python:  {'ok' if state.python_ok else 'missing'}", file=out)
    if state.commit is None:
        print(f"  {name}: not installed from a pinned commit", file=out)
    else:
        match = "ok" if state.pin_ok else "MISMATCH"
        print(f"  {name}: {state.commit[:12]} (pin {adapter.DROPCAST_PIN}: {match})", file=out)
    print(f"  smoke:   {state.smoke_detail}", file=out)


def check(
    data_dir: Path,
    *,
    uv_bin: str = DEFAULT_UV_BIN,
    runner: Runner = subprocess.run,
    which: Which = shutil.which,
    out: TextIO | None = None,
) -> int:
    """Report the sidecar's state without changing anything. 0 = ready."""
    out = out or sys.stdout
    state = inspect(data_dir, runner)
    print("On Air sidecar", file=out)
    _report(state, out)
    uv = find_uv(uv_bin, which)
    print(f"  uv:      {uv or 'not found (needed only to install)'}", file=out)
    if state.ok:
        print("Result: OK", file=out)
        return 0
    print("Result: NOT READY (run `hermes onair setup`)", file=out)
    return 1
