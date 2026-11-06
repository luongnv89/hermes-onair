"""The offline guards in conftest.py really block (AGENTS.md rule 5)."""

from __future__ import annotations

import os
import socket
import subprocess
import tomllib
from pathlib import Path

import pytest

import onair

ROOT = Path(__file__).resolve().parent.parent
CONFTEST = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
BLACKHOLE = ("192.0.2.1", 9)  # TEST-NET-1 (RFC 5737): never routed


def _udp():
    return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


# Each entry: (id, code that tries the call and lets the guard's error escape).
BLOCKED_CALLS = {
    "connect": lambda: socket.socket().connect(BLACKHOLE),
    "connect_ex": lambda: socket.socket().connect_ex(BLACKHOLE),
    "sendto": lambda: _udp().sendto(b"x", BLACKHOLE),
    "sendto_flags": lambda: _udp().sendto(b"x", 0, BLACKHOLE),
    "sendmsg": lambda: _udp().sendmsg([b"x"], [], 0, BLACKHOLE),
    "sendto_mapped_v6": lambda: socket.socket(socket.AF_INET6, socket.SOCK_DGRAM).sendto(
        b"x", ("::ffff:192.0.2.1", 9)
    ),
    "getaddrinfo": lambda: socket.getaddrinfo("example.com", 443),
    "gethostbyname": lambda: socket.gethostbyname("example.com"),
    "gethostbyname_ex": lambda: socket.gethostbyname_ex("example.com"),
    "gethostbyaddr": lambda: socket.gethostbyaddr("192.0.2.1"),
    "subprocess_run": lambda: subprocess.run(["true"], check=False),
    "popen": lambda: subprocess.Popen(["true"]),
    "os_system": lambda: os.system("true"),  # noqa: S605 - blocked by the guard
    "posix_spawn": lambda: os.posix_spawn("/bin/true", ["true"], {}),
    "posix_spawnp": lambda: os.posix_spawnp("true", ["true"], {}),
}


@pytest.mark.parametrize("name", sorted(BLOCKED_CALLS))
def test_each_path_is_blocked_and_recorded(offline_guard, name):
    with pytest.raises((OSError, RuntimeError)):
        BLOCKED_CALLS[name]()
    # Caught or not, the attempt is recorded, so teardown fails the test.
    assert len(offline_guard.attempts) == 1, offline_guard.attempts
    offline_guard.attempts.clear()


def test_loopback_stays_usable(offline_guard):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.connect(server.getsockname())
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver, _udp() as sender:
        receiver.bind(("127.0.0.1", 0))
        sender.sendto(b"x", receiver.getsockname())
        assert receiver.recv(1) == b"x"
    assert socket.getaddrinfo("localhost", 80)
    assert socket.gethostbyname("localhost")
    assert offline_guard.attempts == []


# --- the guard fails a test at teardown even when the test caught the error ---


def _inner_module(names) -> str:
    lines = [
        "import os, socket, subprocess",
        "import pytest",
        f"BLACKHOLE = {BLACKHOLE!r}",
        "def _udp():",
        "    return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)",
        "CALLS = {",
    ]
    sources = {
        "connect": "socket.socket().connect(BLACKHOLE)",
        "sendto": "_udp().sendto(b'x', BLACKHOLE)",
        "sendmsg": "_udp().sendmsg([b'x'], [], 0, BLACKHOLE)",
        "gethostbyname": "socket.gethostbyname('example.com')",
        "gethostbyname_ex": "socket.gethostbyname_ex('example.com')",
        "gethostbyaddr": "socket.gethostbyaddr('192.0.2.1')",
        "subprocess_run": "subprocess.run(['true'], check=False)",
        "os_system": "os.system('true')",
        "posix_spawn": "os.posix_spawn('/bin/true', ['true'], {})",
        "posix_spawnp": "os.posix_spawnp('true', ['true'], {})",
    }
    lines += [f"    {n!r}: lambda: {sources[n]}," for n in names]
    lines += [
        "}",
        "@pytest.mark.parametrize('name', sorted(CALLS))",
        "def test_caught(name):",
        "    try:",
        "        CALLS[name]()",
        "    except Exception:",
        "        pass  # the code under test swallowed the error",
    ]
    return "\n".join(lines) + "\n"


CAUGHT = [
    "connect", "sendto", "sendmsg", "gethostbyname", "gethostbyname_ex", "gethostbyaddr",
    "subprocess_run", "os_system", "posix_spawn", "posix_spawnp",
]


def test_a_caught_attempt_still_fails_the_test(pytester):
    pytester.makeconftest(CONFTEST)
    pytester.makepyfile(test_inner=_inner_module(CAUGHT))
    result = pytester.runpytest_inprocess("-p", "no:cacheprovider")
    # Every call "passes" (the error was swallowed) and then errors at teardown.
    result.assert_outcomes(passed=len(CAUGHT), errors=len(CAUGHT))
    for name in ("gethostbyname", "sendto", "sendmsg", "os.system", "os.posix_spawn",
                 "process 'true'"):
        result.stdout.fnmatch_lines([f"*blocked network or process call*{name}*"])


def test_live_tests_are_exempt(pytester):
    pytester.makeconftest(CONFTEST)
    pytester.makepyfile(
        test_live="""
import os, socket, subprocess
import pytest

# Taken at import (collection), before this run's guard fixture patches anything.
BEFORE = (socket.gethostbyname, socket.socket.sendto, subprocess.Popen, os.system)

def now():
    return (socket.gethostbyname, socket.socket.sendto, subprocess.Popen, os.system)

@pytest.mark.live
def test_live_is_left_alone():
    assert all(a is b for a, b in zip(now(), BEFORE))

def test_default_is_guarded():
    assert not any(a is b for a, b in zip(now(), BEFORE))
"""
    )
    result = pytester.runpytest_inprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=2)


def test_api_key_is_not_in_the_environment():
    assert "OPENROUTER_API_KEY" not in os.environ


def test_live_tests_are_deselected_by_default():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pytest_cfg = config["tool"]["pytest"]["ini_options"]
    assert "-m 'not live'" in pytest_cfg["addopts"]
    assert any(marker.startswith("live:") for marker in pytest_cfg["markers"])
    assert any(marker.startswith("real_uv:") for marker in pytest_cfg["markers"])


def test_the_plugin_is_loaded_from_the_repo_root():
    assert Path(onair.__file__).resolve() == ROOT / "__init__.py"
