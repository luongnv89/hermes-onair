"""Test harness: offline by default (AGENTS.md rule 5).

Exactly what is blocked for every test not marked ``live``:

- sockets: ``connect``, ``connect_ex``, and ``sendto``/``sendmsg`` with an
  address, to anything but loopback (IPv4-mapped IPv6 included) or a
  non-IP family (AF_UNIX). This holds for ``socket.socket`` and for raw
  ``_socket.socket``/``socket.SocketType`` objects created during the test
  (both names are swapped for a guarded subclass);
- DNS: ``socket.getaddrinfo``, ``gethostbyname``, ``gethostbyname_ex`` and
  ``gethostbyaddr`` for any name but ``localhost``/``*.localhost`` or a
  loopback address;
- processes: ``subprocess.Popen`` (so ``subprocess.run`` and friends),
  ``os.system``, ``os.posix_spawn``/``posix_spawnp``, ``os.spawn*``,
  ``os.fork``/``os.forkpty`` and ``os.exec*``.

Every blocked attempt is recorded and fails the test at teardown, even when
the code under test caught the error. Not covered: sockets created before the
test started, C extensions that call the OS directly (``_posixsubprocess``,
``ctypes``), and child processes' own network use; the code under test takes
an injected runner instead of spawning.

- ``OPENROUTER_API_KEY`` is removed from the environment (kept only for
  ``live`` tests, which read the real key from the environment).
- ``agent.tts_provider`` (Hermes) gets a stand-in when Hermes is not
  importable, so ``router.OnAirTTS`` can be built.
- Tests marked ``live`` are deselected by default (``-m 'not live'`` in
  pyproject.toml) and are the only ones exempt from these guards.
- Tests marked ``real_uv`` may start exactly one program, the ``uv`` found on
  PATH, through ``subprocess.Popen`` only, with no ``executable=`` override,
  no shell, and ``env`` equal to the test's ``real_uv_env`` (offline uv, no
  paid keys); every other start is blocked as above. The socket guards still
  apply to them.

The repo root is the plugin package; it is imported here as ``onair`` the way
Hermes loads it (``spec_from_file_location`` with the root as its search path).
"""

from __future__ import annotations

import _socket
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


def _hermes_tts_base() -> None:
    """Hermes's ``agent.tts_provider`` when importable, else a stand-in with its shape.

    ``router.OnAirTTS`` subclasses ``agent.tts_provider.TTSProvider``. Hermes
    is not installed where the tests run, so this mirrors the parts the
    plugin relies on (``hermes-agent/agent/tts_provider.py``): an ABC with an
    abstract ``name`` and ``synthesize``, catalog defaults, ``is_available``.
    """
    try:
        import agent.tts_provider  # noqa: F401
        return
    except ImportError:
        pass
    import abc
    import types

    class TTSProvider(abc.ABC):
        streams_pcm = False
        stream_sample_rate = None

        @property
        @abc.abstractmethod
        def name(self) -> str: ...

        @property
        def display_name(self) -> str:
            return self.name.title()

        def is_available(self) -> bool:
            return True

        def list_models(self):
            return []

        def default_model(self):
            models = self.list_models()
            return models[0].get("id") if models else None

        def list_voices(self):
            return []

        def default_voice(self):
            voices = self.list_voices()
            return voices[0].get("id") if voices else None

        @abc.abstractmethod
        def synthesize(self, text, output_path, *, voice=None, model=None, speed=None,
                       format="mp3", **extra): ...

    package = types.ModuleType("agent")
    package.__path__ = []
    module = types.ModuleType("agent.tts_provider")
    module.TTSProvider = TTSProvider
    module.STAND_IN = True
    package.tts_provider = module
    sys.modules["agent"] = package
    sys.modules["agent.tts_provider"] = module


_hermes_tts_base()
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
        self.allowed_env: dict[str, str] | None = None

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

    # `_socket.socket` is an immutable C type, so swap the names it is reached by.
    class GuardedRawSocket(_socket.socket):
        pass

    GuardedRawSocket.connect = connect
    GuardedRawSocket.connect_ex = connect_ex
    GuardedRawSocket.sendto = sendto
    GuardedRawSocket.sendmsg = sendmsg
    monkeypatch.setattr(_socket, "socket", GuardedRawSocket)
    monkeypatch.setattr(socket, "SocketType", GuardedRawSocket)
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
        monkeypatch.setattr(socket, name, dns(name, getattr(socket, name)))

    # --- processes ---
    real_popen = subprocess.Popen
    if request.node.get_closest_marker("real_uv"):
        uv = shutil.which("uv")
        if uv is None:
            pytest.skip("uv is not on PATH")
        guard.allowed_programs.add(uv)
        guard.allowed_env = request.getfixturevalue("real_uv_env")

    class GuardedPopen(real_popen):
        def __init__(self, *args, **kwargs):
            program = _program(args, kwargs)
            why = None
            if program not in guard.allowed_programs:
                why = "tests must not start real processes; inject a fake runner"
            elif len(args) > 1 or kwargs.get("executable") not in (None, program):
                why = "a real_uv test may not override the executable"
            elif kwargs.get("shell"):
                why = "a real_uv test may not use a shell"
            elif kwargs.get("env") != guard.allowed_env:
                why = "a real_uv test must pass env=real_uv_env"
            if why:
                raise guard.block(f"process {program!r}", RuntimeError,
                                  f"{why} (attempted: {program!r})")
            super().__init__(*args, **kwargs)

    def no_system(command):
        raise guard.block(f"os.system {command!r}", RuntimeError,
                          f"tests must not start real processes (os.system {command!r})")

    def no_spawn(name):
        def spawn(*args, **kwargs):
            what = f"os.{name} {args[0]!r}" if args else f"os.{name}"
            raise guard.block(what, RuntimeError, f"tests must not start real processes ({what})")

        return spawn

    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)
    monkeypatch.setattr(os, "system", no_system)
    starters = ("posix_spawn", "posix_spawnp", "fork", "forkpty")
    starters += tuple(n for n in dir(os) if n.startswith(("spawn", "exec")))
    for name in starters:
        if callable(getattr(os, name, None)):
            monkeypatch.setattr(os, name, no_spawn(name))

    yield guard
    if guard.attempts:
        tried = "; ".join(guard.attempts)
        pytest.fail(f"the test tried a blocked network or process call: {tried}", pytrace=False)


@pytest.fixture
def real_uv_env(tmp_path):
    """An environment that keeps a real ``uv venv`` offline and self-contained."""
    from onair import adapter

    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("UV_") and k not in adapter.SCRUBBED_ENV
    }
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
def no_api_key(request, monkeypatch):
    # A live test reads the real key from the environment; nothing else sees it.
    if not request.node.get_closest_marker("live"):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
