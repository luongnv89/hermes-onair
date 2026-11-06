"""The offline guards in conftest.py really block (AGENTS.md rule 5)."""

from __future__ import annotations

import _socket
import os
import shutil
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


# Taken at import (collection), before any test's guard swaps the names.
ORIGINAL_RAW_SOCKET = _socket.socket
ORIGINAL_OS = {n: getattr(os, n) for n in ("fork", "forkpty", "execv", "spawnv", "execvpe")}


def test_raw_socket_names_and_os_starters_are_swapped():
    # Proves the swap without sending anything (the other tests try the calls).
    assert _socket.socket is not ORIGINAL_RAW_SOCKET
    assert issubclass(_socket.socket, ORIGINAL_RAW_SOCKET)
    assert socket.SocketType is _socket.socket
    assert all(getattr(os, n) is not f for n, f in ORIGINAL_OS.items())


def _raw_udp():
    # Looked up at call time, as code under test would: the guard swaps the name.
    return _socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def _child_exits(pid):
    """If an unguarded fork ever returns in the child, leave at once."""
    if (pid[0] if isinstance(pid, tuple) else pid) == 0:
        os._exit(0)


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
    "os_system": lambda: os.system("true"),
    "posix_spawn": lambda: os.posix_spawn("/bin/true", ["true"], {}),
    "posix_spawnp": lambda: os.posix_spawnp("true", ["true"], {}),
    # round 2 (N3): raw sockets, os.spawn*, os.fork/forkpty, os.exec*
    "raw_connect": lambda: _socket.socket().connect(BLACKHOLE),
    "raw_connect_ex": lambda: _socket.socket().connect_ex(BLACKHOLE),
    "raw_sendto": lambda: _raw_udp().sendto(b"x", BLACKHOLE),
    "raw_sendmsg": lambda: _raw_udp().sendmsg([b"x"], [], 0, BLACKHOLE),
    "sockettype_sendto": lambda: socket.SocketType(socket.AF_INET, socket.SOCK_DGRAM).sendto(
        b"x", BLACKHOLE
    ),
    "spawnv": lambda: os.spawnv(os.P_WAIT, "/bin/true", ["true"]),
    "spawnvp": lambda: os.spawnvp(os.P_WAIT, "true", ["true"]),
    "spawnl": lambda: os.spawnl(os.P_WAIT, "/bin/true", "true"),
    "spawnve": lambda: os.spawnve(os.P_WAIT, "/bin/true", ["true"], {}),
    "fork": lambda: _child_exits(os.fork()),
    "forkpty": lambda: _child_exits(os.forkpty()),
    "execv": lambda: os.execv("/bin/true", ["true"]),
    "execvp": lambda: os.execvp("true", ["true"]),
    "execl": lambda: os.execl("/bin/true", "true"),
    "execve": lambda: os.execve("/bin/true", ["true"], {}),
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
        "import _socket, os, socket, subprocess",
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
        "raw_connect": "_socket.socket().connect(BLACKHOLE)",
        "raw_sendto": "_socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b'x', BLACKHOLE)",
        "spawnv": "os.spawnv(os.P_WAIT, '/bin/true', ['true'])",
        "fork": "os.fork() or None",
        "execv": "os.execv('/bin/true', ['true'])",
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
    "raw_connect", "raw_sendto", "spawnv", "fork", "execv",
]


def test_a_caught_attempt_still_fails_the_test(pytester):
    pytester.makeconftest(CONFTEST)
    pytester.makepyfile(test_inner=_inner_module(CAUGHT))
    result = pytester.runpytest_inprocess("-p", "no:cacheprovider")
    # Every call "passes" (the error was swallowed) and then errors at teardown.
    result.assert_outcomes(passed=len(CAUGHT), errors=len(CAUGHT))
    for name in ("gethostbyname", "sendto", "sendmsg", "os.system", "os.posix_spawn",
                 "process 'true'", "os.spawnv", "os.fork", "os.execv"):
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


# --- real_uv tests may start exactly the resolved uv, with real_uv_env ---

REAL_UV = shutil.which("uv") or "uv"


@pytest.mark.real_uv
@pytest.mark.parametrize(
    ("call", "why"),
    [
        (lambda env: subprocess.run(["/bin/true"], env=env, check=False), "inject a fake runner"),
        (lambda env: subprocess.run([REAL_UV, "--version"], executable="/bin/true", env=env,
                                    check=False), "may not override the executable"),
        (lambda env: subprocess.run(f"{REAL_UV} --version", shell=True, env=env, check=False),
         "inject a fake runner"),  # the program is the whole shell string, not uv
        (lambda env: subprocess.run([REAL_UV, "--version"], env=dict(os.environ), check=False),
         "must pass env=real_uv_env"),
        (lambda env: subprocess.run([REAL_UV, "--version"], check=False),
         "must pass env=real_uv_env"),
        (lambda env: os.execv(REAL_UV, [REAL_UV, "--version"]), "must not start real processes"),
    ],
)
def test_real_uv_tests_may_only_run_uv_with_real_uv_env(offline_guard, real_uv_env, call, why):
    with pytest.raises(RuntimeError, match=why):
        call(real_uv_env)
    assert len(offline_guard.attempts) == 1
    offline_guard.attempts.clear()


@pytest.mark.real_uv
def test_real_uv_runs_with_real_uv_env(offline_guard, real_uv_env):
    proc = subprocess.run([REAL_UV, "--version"], env=real_uv_env, capture_output=True,
                          text=True, check=False)
    assert proc.returncode == 0 and proc.stdout.startswith("uv ")
    assert offline_guard.attempts == []
    assert real_uv_env["UV_OFFLINE"] == "1"
    assert not {"OPENROUTER_API_KEY", "ELEVENLABS_API_KEY"} & set(real_uv_env)
