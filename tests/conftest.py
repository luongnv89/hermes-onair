"""Test harness: offline by default (AGENTS.md rule 5).

- Non-loopback sockets and DNS lookups are blocked, and an attempt fails the
  test even when the code under test caught the error.
- No real subprocess can start: code under test must take an injected runner.
- ``OPENROUTER_API_KEY`` is removed from the environment.
- Tests marked ``live`` are deselected by default (``-m 'not live'`` in
  pyproject.toml) and are the only ones exempt from these guards.

The repo root is the plugin package; it is imported here as ``onair`` the way
Hermes loads it (``spec_from_file_location`` with the root as its search path).
"""

from __future__ import annotations

import importlib.util
import ipaddress
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
    if host in (None, "", "localhost") or str(host).endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(str(host).split("%", 1)[0]).is_loopback
    except ValueError:
        return False


class NetworkGuard:
    """Records every blocked network attempt of one test."""

    def __init__(self) -> None:
        self.attempts: list[str] = []

    def allowed(self, sock: socket.socket, address) -> bool:
        if sock.family not in (socket.AF_INET, socket.AF_INET6):
            return True
        host = address[0] if isinstance(address, tuple) else address
        return _is_local_host(host)


@pytest.fixture(autouse=True)
def network_guard(request, monkeypatch):
    if request.node.get_closest_marker("live"):
        yield None
        return
    guard = NetworkGuard()
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def connect(sock, address):
        if not guard.allowed(sock, address):
            guard.attempts.append(f"connect {address!r}")
            raise OSError(f"live network call blocked in tests: {address!r}")
        return real_connect(sock, address)

    def connect_ex(sock, address):
        if not guard.allowed(sock, address):
            guard.attempts.append(f"connect_ex {address!r}")
            raise OSError(f"live network call blocked in tests: {address!r}")
        return real_connect_ex(sock, address)

    def getaddrinfo(host, *args, **kwargs):
        if not _is_local_host(host):
            guard.attempts.append(f"getaddrinfo {host!r}")
            raise socket.gaierror(socket.EAI_NONAME, f"DNS lookup blocked in tests: {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield guard
    if guard.attempts:
        tried = "; ".join(guard.attempts)
        pytest.fail(f"the test tried a live network call: {tried}", pytrace=False)


class _NoPopen:
    def __init__(self, *args, **kwargs):
        raise RuntimeError(
            "tests must not start real processes; inject a fake runner "
            f"(attempted: {args[0] if args else kwargs.get('args')!r})"
        )


@pytest.fixture(autouse=True)
def no_subprocess(request, monkeypatch):
    if not request.node.get_closest_marker("live"):
        monkeypatch.setattr(subprocess, "Popen", _NoPopen)


@pytest.fixture(autouse=True)
def no_api_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
