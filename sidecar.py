"""The sidecar venv behind ``hermes onair setup``.

The sidecar is a uv-made virtualenv at ``<plugin data dir>/sidecar`` holding
the pinned engine package. This module runs uv itself (``uv venv`` and
``uv pip install``, with the install arguments ``adapter`` supplies) and asks
``adapter`` everything engine-specific: what to install, which commit is
installed, and whether the engine starts (AGENTS.md rule 4).

Setup only ever writes to an absent path or a sidecar it owns. An existing
directory is On Air's only when ALL of these hold:

- it is a real directory (not a symbolic link) directly inside the data dir;
- its ``pyvenv.cfg`` is a regular file that says uv made it (``uv = …``);
- ``adapter.pinned_install``: uv installed the engine there from its own
  repository at the pinned commit (PEP 610 ``direct_url.json``);
- and either it holds the ``.onair-sidecar`` marker (OWNED), or it has a
  Python and no marker: a v0.1.0 sidecar, adopted by writing the marker
  (ADOPTABLE). ``--check`` never writes; it reports it as adoptable.

A marker alone is never enough. Only an owned venv is recreated with
``uv venv --clear``; a venv setup creates in the same run is used for the
install that follows. Anything else (a marked venv whose install never
finished, a sidecar at another commit after a pin change, a foreign dir) is
refused with a message asking the user to move it aside. This module never
deletes anything itself.

Not checked: whether the directory is older than its ``pyvenv.cfg``. Linux
gives no directory birth time through ``os.stat`` (``st_birthtime`` is
BSD/macOS-only), the directory's mtime/ctime move whenever uv or setup adds
an entry (``.lock``, the marker), and a planted directory is naturally
created before the file it holds, so the test would refuse nothing it should.

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
MARKER = ".onair-sidecar"
DEFAULT_UV_BIN = ""
DEFAULT_SIDECAR_PYTHON = ">=3.11"

VENV_TIMEOUT_S = 300
INSTALL_TIMEOUT_S = 900

# Who owns what sits at the sidecar path.
ABSENT = "absent"
OWNED = "owned"  # marker + uv-made venv + pinned engine install
ADOPTABLE = "adoptable"  # no marker, a Python, uv-made, pinned engine install
FOREIGN = "foreign"  # anything else: never written to

_OWNER_TEXT = {
    ABSENT: "none yet",
    OWNED: f"On Air ({MARKER} present, pinned install)",
    ADOPTABLE: f"On Air, from before {MARKER} (setup adopts it)",
    FOREIGN: "not On Air's; setup will not touch it",
}

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
    """What is on disk, who owns it, and whether it is the pinned, working install."""

    venv: Path
    problem: str | None
    owner: str
    owner_note: str
    python_ok: bool
    commit: str | None
    pin_ok: bool
    smoke_ok: bool
    smoke_detail: str

    @property
    def ok(self) -> bool:
        return (
            self.problem is None
            and self.owner in (OWNED, ADOPTABLE)
            and self.python_ok
            and self.pin_ok
            and self.smoke_ok
        )


def venv_dir(data_dir: Path) -> Path:
    return Path(data_dir) / VENV_DIRNAME


def venv_python(venv: Path) -> Path:
    return Path(venv) / "bin" / "python"


def path_problem(data_dir: Path, venv: Path) -> str | None:
    """Why *venv* is not a safe place to create or clear a venv, or None."""
    if venv.is_symlink():
        return "it is a symbolic link"
    try:
        inside = venv.resolve().parent == Path(data_dir).resolve()
    except (OSError, RuntimeError) as exc:
        return f"it cannot be resolved ({exc})"
    return None if inside else "it resolves outside the plugin data dir"


def ownership(venv: Path) -> str:
    """ABSENT, OWNED, ADOPTABLE or FOREIGN. Reads only."""
    return _ownership(venv)[0]


def _uv_made(venv: Path) -> bool:
    cfg = venv / "pyvenv.cfg"
    if cfg.is_symlink() or not cfg.is_file():
        return False
    try:
        lines = cfg.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    return any(line.partition("=")[0].strip() == "uv" for line in lines)


def _ownership(venv: Path) -> tuple[str, str]:
    """(owner, why) for what sits at *venv*. Reads only."""
    if not venv.exists() and not venv.is_symlink():
        return ABSENT, ""
    if venv.is_symlink() or not venv.is_dir():
        return FOREIGN, "it is not a real directory"
    marker = venv / MARKER
    marked = marker.is_file() and not marker.is_symlink()
    if not _uv_made(venv):
        return FOREIGN, "it is not a venv made by uv (no `uv = …` in pyvenv.cfg)"
    if not adapter.pinned_install(venv):
        if marked:
            return FOREIGN, (
                f"it holds {MARKER} but no {adapter.NAME} installed by uv at pin "
                f"{adapter.DROPCAST_PIN} (an unfinished or older setup, or a planted marker)"
            )
        return FOREIGN, (
            f"it was not created by On Air (no {MARKER}, no {adapter.NAME} at pin "
            f"{adapter.DROPCAST_PIN})"
        )
    if marked:
        return OWNED, ""
    if venv_python(venv).is_file():
        return ADOPTABLE, ""
    return FOREIGN, f"it has no {MARKER} and no Python"


def write_marker(venv: Path) -> None:
    """Mark *venv* as On Air's own; never follows a symlink planted at the marker."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(venv / MARKER, flags, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write("Created by the hermes-onair plugin (`hermes onair setup`).\n")


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


def _run(runner: Runner, argv: list[str], timeout: int, env: Mapping[str, str]):
    return runner(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=dict(env),
        check=False,
    )


def _install_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """uv's environment: no paid keys or engine knobs, and never a prompt.

    A missing key or unknown host fails instead of hanging.
    """
    source = os.environ if base is None else base
    env = {k: v for k, v in source.items() if k not in adapter.SCRUBBED_ENV}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return env


def inspect(data_dir: Path, runner: Runner = subprocess.run) -> SidecarState:
    """Read the sidecar's state; never writes. Starts only the engine's smoke check,
    and only in a venv that is safely placed and On Air's own."""
    venv = venv_dir(data_dir)
    problem = path_problem(data_dir, venv)
    owner, note = _ownership(venv)
    python_ok = venv_python(venv).is_file()
    commit = adapter.installed_commit(venv) if python_ok else None
    pin_ok = adapter.pin_matches(commit)
    smoke_ok, detail = False, "skipped"
    if python_ok and pin_ok and problem is None and owner in (OWNED, ADOPTABLE):
        smoke_ok, detail = adapter.smoke(venv, runner)
    return SidecarState(venv, problem, owner, note, python_ok, commit, pin_ok, smoke_ok, detail)


def _refuse(venv: Path, why: str, err: TextIO) -> int:
    print(f"✗ Not touching {venv}: {why}.", file=err)
    print(
        "  On Air only creates, repairs or clears its own sidecar: a real directory "
        f"inside its data dir, marked with {MARKER}.",
        file=err,
    )
    print(
        f"  Move it aside (for example `mv {venv} {venv}.old`), then run "
        "`hermes onair setup` again.",
        file=err,
    )
    return 1


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

    Returns 0 when the sidecar is ready, 1 when the path is not On Air's to
    write or uv failed, 2 when uv is missing.
    """
    out = out or sys.stdout
    err = err or sys.stderr
    name = adapter.NAME
    state = inspect(data_dir, runner)
    venv = state.venv
    if state.problem:
        return _refuse(venv, state.problem, err)
    if state.owner == FOREIGN:
        return _refuse(venv, state.owner_note, err)

    try:
        if state.owner == ADOPTABLE:
            write_marker(venv)
            print(f"● Adopted the existing sidecar at {venv} (wrote {MARKER})", file=out)
    except OSError as exc:
        print(f"✗ could not mark {venv} as On Air's: {exc.strerror or exc}", file=err)
        return 1

    if state.ok:
        print(f"✓ On Air sidecar already set up ({name} {state.commit[:12]})", file=out)
        print(f"  venv: {venv}", file=out)
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

    created = False
    if not state.python_ok:
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        argv = [uv, "venv", "--python", python_request or DEFAULT_SIDECAR_PYTHON]
        if state.owner == OWNED:  # never --clear a directory On Air did not create
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
        created = True
        try:
            write_marker(venv)
        except OSError as exc:
            print(f"✗ could not mark {venv} as On Air's: {exc.strerror or exc}", file=err)
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
        _unfinished(venv, created, err)
        return 1

    state = inspect(data_dir, runner)
    if not state.ok:
        print(f"✗ {name} installed, but the sidecar check failed:", file=err)
        _report(state, err)
        _unfinished(venv, created, err)
        return 1
    print(f"✓ On Air sidecar ready ({name} {state.commit[:12]})", file=out)
    print(f"  venv: {state.venv}", file=out)
    return 0


def _unfinished(venv: Path, created: bool, err: TextIO) -> None:
    """A marker alone does not make a venv On Air's: say what the next run needs."""
    if created:
        print(
            f"  The venv this run created at {venv} holds no pinned {adapter.NAME}, so the "
            "next setup will not reuse it: move it aside first.",
            file=err,
        )


def _report(state: SidecarState, out: TextIO) -> None:
    name = adapter.NAME
    print(f"  venv:    {state.venv} ({'present' if state.venv.exists() else 'missing'})", file=out)
    if state.problem:
        print(f"  path:    UNSAFE: {state.problem}", file=out)
    note = f" ({state.owner_note})" if state.owner_note else ""
    print(f"  owner:   {_OWNER_TEXT[state.owner]}{note}", file=out)
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
    if state.problem or state.owner == FOREIGN:
        print("Result: NOT READY (move the path aside, then run `hermes onair setup`)", file=out)
    else:
        print("Result: NOT READY (run `hermes onair setup`)", file=out)
    return 1
