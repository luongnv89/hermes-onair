"""Test doubles: a scripted process runner and a direct_url.json writer."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

from onair import adapter

#: The pinned commit as uv records it (full SHA), built from the one pin.
PINNED_SHA = adapter.DROPCAST_PIN + "ab1cb34d54913285597309403b892fe8b"
OTHER_SHA = "0123456789abcdef0123456789abcdef01234567"


def write_install(
    venv: Path,
    commit: str | None = PINNED_SHA,
    *,
    python: bool = True,
    marker: bool = True,
    uv_made: bool = True,
) -> None:
    """Lay out a sidecar venv the way uv leaves it after a git install.

    ``marker`` adds the ``.onair-sidecar`` file setup writes after creating a
    venv; ``uv_made`` the ``uv = …`` line uv writes in ``pyvenv.cfg``.
    """
    venv.mkdir(parents=True, exist_ok=True)
    if not (venv / "pyvenv.cfg").exists():  # keep the one a real `uv venv` wrote
        cfg = "home = /usr/bin\n" + ("uv = 0.13.0\n" if uv_made else "")
        (venv / "pyvenv.cfg").write_text(cfg + "version_info = 3.12.3\n")
    if marker:
        (venv / ".onair-sidecar").write_text("Created by the hermes-onair plugin.\n")
    if python:
        (venv / "bin").mkdir(parents=True, exist_ok=True)
        (venv / "bin" / "python").write_text("")
    if commit is not None:
        info = venv / "lib" / "python3.12" / "site-packages" / "dropcast-0.2.0.dist-info"
        info.mkdir(parents=True, exist_ok=True)
        url = adapter.DROPCAST_REPO.removeprefix("git+")
        record = {"url": url, "vcs_info": {"vcs": "git", "commit_id": commit}}
        (info / "direct_url.json").write_text(json.dumps(record))
        (info / "INSTALLER").write_text("uv\n")


class FakeRunner:
    """Answers each call with the first matching rule; records every call."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._rules: list[tuple[Callable[[list[str]], bool], Callable[[list[str]], object]]] = []

    def on(self, match: Callable[[list[str]], bool], respond) -> FakeRunner:
        self._rules.append((match, respond if callable(respond) else (lambda argv, r=respond: r)))
        return self

    def __call__(self, argv, **kwargs):
        argv = [str(a) for a in argv]
        self.calls.append({"argv": argv, **kwargs})
        for match, respond in self._rules:
            if match(argv):
                result = respond(argv)
                if isinstance(result, BaseException):
                    raise result
                return result
        raise AssertionError(f"unexpected command: {argv}")

    def argvs(self) -> list[list[str]]:
        return [call["argv"] for call in self.calls]


def done(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)
