"""The offline guards in conftest.py really block (AGENTS.md rule 5)."""

from __future__ import annotations

import socket
import subprocess
import tomllib
from pathlib import Path

import pytest

import onair

ROOT = Path(__file__).resolve().parent.parent


def test_non_loopback_connect_is_blocked_and_recorded(network_guard):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(OSError, match="blocked in tests"):
            sock.connect(("192.0.2.1", 443))
        with pytest.raises(OSError, match="blocked in tests"):
            sock.connect_ex(("192.0.2.1", 443))
    finally:
        sock.close()
    # Caught or not, the attempts are recorded, so the fixture fails the test.
    assert len(network_guard.attempts) == 2
    network_guard.attempts.clear()


def test_dns_is_blocked_but_localhost_resolves(network_guard):
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("example.com", 443)
    assert network_guard.attempts == ["getaddrinfo 'example.com'"]
    network_guard.attempts.clear()
    assert socket.getaddrinfo("localhost", 80)


def test_loopback_connect_is_allowed(network_guard):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.connect(server.getsockname())
    assert network_guard.attempts == []


def test_real_processes_cannot_start():
    with pytest.raises(RuntimeError, match="must not start real processes"):
        subprocess.run(["true"], check=False)


def test_api_key_is_not_in_the_environment():
    import os

    assert "OPENROUTER_API_KEY" not in os.environ


def test_live_tests_are_deselected_by_default():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pytest_cfg = config["tool"]["pytest"]["ini_options"]
    assert "-m 'not live'" in pytest_cfg["addopts"]
    assert any(marker.startswith("live:") for marker in pytest_cfg["markers"])


def test_the_plugin_is_loaded_from_the_repo_root():
    assert Path(onair.__file__).resolve() == ROOT / "__init__.py"
