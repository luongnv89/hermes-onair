"""Test harness: offline by default (AGENTS.md rule 5).

- Non-loopback network use is blocked: ``connect``/``connect_ex``,
  ``sendto``/``sendmsg`` to an address, and the DNS calls ``getaddrinfo``,
  ``gethostbyname``, ``gethostbyname_ex`` and ``gethostbyaddr``.
- No process can start: ``subprocess.Popen`` (so ``subprocess.run``),
  ``os.system`` and ``os.posix_spawn``/``posix_spawnp`` raise.
- Every blocked attempt is recorded and fails the test at teardown, even when
  the code under test caught the error.
- ``OPENROUTER_API_KEY`` is removed from the environment.
- Tests marked ``live`` are deselected by default (``-m 'not live'`` in
  pyproject.toml) and are the only ones exempt from these guards.
- Tests marked ``real_uv`` may start the real ``uv`` (and nothing else), with
  an environment that keeps it offline (see ``real_uv_env``); the network
  guard still applies to them.

The repo root is the plugin package; it is imported here as ``onair`` the way
Hermes loads it (``spec_from_file_location`` with the root as its search path).
"""

from __future__ import annotations

import importlib.util
import ipaddress
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_plugin_package() -> None:
    if "onair" in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        "onair", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["onair"] = module
    spec.loader.exec_module(module)


_load_plugin_package()


def _is_local_host(host) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if host in (None, "", "localhost") or str(host).endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(str(host).split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return (mapped or address).is_loopback


class OfflineGuard:
    """Records every blocked network or process attempt of one test."""

    def __init__(self) -> None:
        self.attempts: list[str] = []
        self.allowed_programs: set[str] = set()

    def allowed(self, sock: socket.socket, address) -> bool:
        if sock.family not in (socket.AF_INET, socket.AF_INET6):
            return True
        host = address[0] if isinstance(address, tuple) else address
        return _is_local_host(host)

    def block(self, what: str, error: type[BaseException] = OSError, *args) -> BaseException:
        self.attempts.append(what)
        return error(*args) if args else error(f"blocked in tests: {what}")


def _program(args, kwargs) -> str:
    argv = args[0] if args else kwargs.get("args")
    if isinstance(argv, (list, tuple)) and argv:
        return str(argv[0])
    return str(argv)


@pytest.fixture(autouse=True)
def offline_guard(request, monkeypatch):
    if request.node.get_closest_marker("live"):
        yield None
        return
    guard = OfflineGuard()

    # --- network ---
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_sendto = socket.socket.sendto
    real_sendmsg = socket.socket.sendmsg

    def connect(sock, address):
        if not guard.allowed(sock, address):
            raise guard.block(f"connect {address!r}")
        return real_connect(sock, address)

    def connect_ex(sock, address):
        if not guard.allowed(sock, address):
            raise guard.block(f"connect_ex {address!r}")
        return real_connect_ex(sock, address)

    def sendto(sock, data, *rest):
        address = rest[-1] if rest else None
        if address is not None and not guard.allowed(sock, address):
            raise guard.block(f"sendto {address!r}")
        return real_sendto(sock, data, *rest)

    def sendmsg(sock, buffers, *rest):
        address = rest[2] if len(rest) >= 3 else None
        if address is not None and not guard.allowed(sock, address):
            raise guard.block(f"sendmsg {address!r}")
        return real_sendmsg(sock, buffers, *rest)

    def dns(name, real):
        def lookup(host, *args, **kwargs):
            if not _is_local_host(host):
                raise guard.block(
                    f"{name} {host!r}", socket.gaierror, socket.EAI_NONAME,
                    f"DNS lookup blocked in tests: {host!r}",
                )
            return real(host, *args, **kwargs)

        return lookup

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", sendto)
    monkeypatch.setattr(socket.socket, "sendmsg", sendmsg)
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
        monkeypatch.setattr(socket, name, dns(name, getattr(socket, name)))

    # --- processes ---
    real_popen = subprocess.Popen
    if request.node.get_closest_marker("real_uv"):
        uv = shutil.which("uv")
        if uv is None:
            pytest.skip("uv is not on PATH")
        guard.allowed_programs.add(uv)

    class GuardedPopen(real_popen):
        def __init__(self, *args, **kwargs):
            program = _program(args, kwargs)
            if program not in guard.allowed_programs:
                raise guard.block(
                    f"process {program!r}", RuntimeError,
                    f"tests must not start real processes; inject a fake runner "
                    f"(attempted: {program!r})",
                )
            super().__init__(*args, **kwargs)

    def no_system(command):
        raise guard.block(f"os.system {command!r}", RuntimeError,
                          f"tests must not start real processes (os.system {command!r})")

    def no_spawn(name):
        def spawn(path, *args, **kwargs):
            raise guard.block(f"os.{name} {path!r}", RuntimeError,
                              f"tests must not start real processes (os.{name} {path!r})")

        return spawn

    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)
    monkeypatch.setattr(os, "system", no_system)
    for name in ("posix_spawn", "posix_spawnp"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, no_spawn(name))

    yield guard
    if guard.attempts:
        tried = "; ".join(guard.attempts)
        pytest.fail(f"the test tried a blocked network or process call: {tried}", pytrace=False)


@pytest.fixture
def real_uv_env(tmp_path):
    """An environment that keeps a real ``uv venv`` offline and self-contained."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("UV_")}
    env.update(
        UV_OFFLINE="1",
        UV_PYTHON_DOWNLOADS="never",
        UV_NO_CONFIG="1",
        UV_CACHE_DIR=str(tmp_path / "uv-cache"),
        # Belt and braces: any HTTP(S) attempt goes to a closed loopback port.
        HTTP_PROXY="http://127.0.0.1:9",
        HTTPS_PROXY="http://127.0.0.1:9",
        ALL_PROXY="http://127.0.0.1:9",
        NO_PROXY="",
    )
    return env


@pytest.fixture(autouse=True)
def no_api_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
