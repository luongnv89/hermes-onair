"""A distinct voice per profile: the persona pool and its first-use claims.

A **persona** is a named character with one voice per engine (Fish, HeyGen,
edge-tts) and, for edge-tts, per language (en, fr, vi). Fish and HeyGen
voices speak every language, so they hold one voice each.

**The pool** (:data:`POOL`) has 10 personas, one per HeyGen voice:

- **HeyGen**: the 10 voices that ``heygen/heygen-voice-1-turbo:free`` lists
  in OpenRouter's public model list (``supported_voices``; DropCast at the
  pinned commit, ``adapter.DROPCAST_PIN``: ``tests/fixtures/openrouter/models.json``,
  with reference clips in ``src/dropcast/data/voices/heygen/``). Every
  persona has its own.
- **Fish**: only the 7 Fish Official voices that DropCast records with their
  terms and the owner's approval (``src/dropcast/data/voices/manifest.json``,
  issue #116). No id is invented, so 3 personas (Bram, Sabrina, Weston)
  have **no Fish voice**. Fallback: for them the router skips Fish, so
  English goes to HeyGen in their own HeyGen voice, and the persona stays
  distinct instead of sharing a Fish voice.
- **edge-tts**: a distinct voice per language from ``edge-tts
  --list-voices`` (47 en, 13 fr, 2 vi). Vietnamese has only 2 native voices
  (``vi-VN-NamMinhNeural``, ``vi-VN-HoaiMyNeural``). Fallback: the other 8
  personas speak Vietnamese with Microsoft *multilingual* voices, as
  DropCast's guests do. Other languages use the router's defaults.

Every voice is a stock voice of a free engine; nothing is cloned. Each
persona keeps one gender on every engine (DropCast's casting, and a pitch
measurement for the 2 HeyGen voices DropCast does not cast).

**Claims.** The first time a profile speaks (or runs ``/onair voice``), it
claims a persona. The probe walks the pool from ``sha256(profile) %
len(POOL)`` and takes the first free persona **that has a Fish voice**;
only when none of those is free does it take a free one without Fish. So
with 7 Fish voices, 9 profiles leave at most 2 without Fish. A plain hash
collides on 3 of 8 real names; the claim does not. The claim is written atomically (a temp
file and ``os.replace``) to that profile's own
``plugin-data/onair/persona.json`` and read back on every later call, so it
survives restarts and later profiles never move it.

**Sharing.** Claims are shared by **scanning the sibling profiles' claim
files**, read-only (the claim file itself is never followed). Siblings are
the homes of the same Hermes root: the root itself (the ``default``
profile) and every ``<root>/profiles/<name>``. No other profile's file is
ever written.

**Races.** Two profiles claiming at once would both see the same free
persona. A claim therefore runs under an exclusive ``flock`` on the root
*directory* (no lock file is written anywhere). Inside the lock the profile
re-reads its own claim, scans, and writes. If the lock cannot be had within
:data:`LOCK_TIMEOUT_S`, the probed persona is used for this call only and
**not stored** (``status="unstored"``, with a warning), so the next call
claims again under the lock and an unlocked race can never write a
duplicate for good. Where no lock can exist at all (no ``fcntl``, no root
to open), the claim is stored unlocked and the log says so.

Once every persona is held, a new profile takes the least-shared one in its
probe order: with 10 personas, an 11th profile shares a voice.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import logging
import os
import secrets
import stat
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows; the plugin targets linux and macOS
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger("onair")

CLAIM_FILE = "persona.json"
CLAIM_VERSION = 1
CLAIM_MAX_BYTES = 64 * 1024
LOCK_TIMEOUT_S = 10.0
_LOCK_POLL_S = 0.05

LANGUAGES = ("en", "fr", "vi")

#: Fish Official voices with documented terms and approval (DropCast at the pin,
#: src/dropcast/data/voices/manifest.json): name -> voice id.
FISH_VOICES: dict[str, str] = {
    "Ethan": "536d3a5e000945adb7038665781a4aca",
    "Hannah": "9a9cf47702da476aa4629e2506d4a857",
    "Laura": "e3cd384158934cc9a01029cd7d278634",
    "Adrian": "bf322df2096a46f18c579d0baa36f41d",
    "Sarah": "933563129e564b19a115bedd57b7406a",
    "Jordan": "79d0bd3e4e5444b18f7b6d89b5927bf1",
    "Selene": "b347db033a6549378b48d00acb0d06cd",
}

#: ``supported_voices`` of heygen/heygen-voice-1-turbo:free in OpenRouter's public list.
HEYGEN_VOICES = (
    "nadine", "tabitha", "bram", "gareth", "noelle",
    "meredith", "sabrina", "lucian", "weston", "vincent",
)


@dataclass(frozen=True)
class Persona:
    """One character: a voice per engine, and per language for edge-tts."""

    id: str
    name: str
    gender: str  # "woman" or "man"
    fish: str | None  # a Fish voice name (key of FISH_VOICES), or None: skip Fish
    heygen: str
    edge: Mapping[str, str]  # language -> edge voice, for LANGUAGES

    @property
    def fish_id(self) -> str | None:
        return FISH_VOICES[self.fish] if self.fish else None

    def voices(self) -> dict[str, dict[str, str]]:
        """``{engine: {language or "*": voice}}``, the shape of the ``voices`` setting.

        An engine the persona has no voice for is absent (the router skips it).
        """
        table: dict[str, dict[str, str]] = {
            "heygen": {"*": self.heygen},
            "edge": dict(self.edge),
        }
        if self.fish_id:
            table["fish"] = {"*": self.fish_id}
        return table


def _edge(en: str, fr: str, vi: str) -> dict[str, str]:
    return {"en": en, "fr": fr, "vi": vi}


#: The pool. Order matters: the probe walks it from the profile's hash.
#: Bob, Alice and the guests keep DropCast's starter casting (voices.py
#: STARTER_VOICES); Nadine and Bram its story voices; Sabrina and Weston are
#: the two HeyGen voices DropCast does not cast.
POOL: tuple[Persona, ...] = (
    Persona("bob", "Bob", "man", "Ethan", "lucian",
            _edge("en-US-AndrewNeural", "fr-FR-HenriNeural", "vi-VN-NamMinhNeural")),
    Persona("alice", "Alice", "woman", "Hannah", "tabitha",
            _edge("en-US-AvaNeural", "fr-FR-DeniseNeural", "vi-VN-HoaiMyNeural")),
    Persona("maya", "Maya", "woman", "Laura", "meredith",
            _edge("en-GB-SoniaNeural", "fr-CH-ArianeNeural", "pt-BR-ThalitaMultilingualNeural")),
    Persona("sam", "Sam", "man", "Adrian", "gareth",
            _edge("en-US-GuyNeural", "fr-BE-GerardNeural", "en-US-BrianMultilingualNeural")),
    Persona("lena", "Lena", "woman", "Sarah", "noelle",
            _edge("en-US-EmmaNeural", "fr-CA-SylvieNeural", "de-DE-SeraphinaMultilingualNeural")),
    Persona("theo", "Theo", "man", "Jordan", "vincent",
            _edge("en-GB-RyanNeural", "fr-CA-ThierryNeural", "en-AU-WilliamMultilingualNeural")),
    Persona("nadine", "Nadine", "woman", "Selene", "nadine",
            _edge("en-US-AriaNeural", "fr-FR-VivienneMultilingualNeural",
                  "fr-FR-VivienneMultilingualNeural")),
    Persona("bram", "Bram", "man", None, "bram",
            _edge("en-US-ChristopherNeural", "fr-FR-RemyMultilingualNeural",
                  "fr-FR-RemyMultilingualNeural")),
    Persona("sabrina", "Sabrina", "woman", None, "sabrina",
            _edge("en-US-JennyNeural", "fr-FR-EloiseNeural", "en-US-AvaMultilingualNeural")),
    Persona("weston", "Weston", "man", None, "weston",
            _edge("en-US-EricNeural", "fr-CA-AntoineNeural", "de-DE-FlorianMultilingualNeural")),
)

BY_ID: dict[str, Persona] = {p.id: p for p in POOL}


def find(name: str) -> Persona | None:
    """The persona called *name* (its id or display name, any case), or None."""
    return BY_ID.get(name.strip().lower()) if isinstance(name, str) else None


def hash_index(profile: str) -> int:
    """``sha256(profile) % len(POOL)``: where a profile's probe starts."""
    digest = hashlib.sha256(profile.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % len(POOL)


def probe(profile: str, held: Mapping[str, Any]) -> Persona:
    """The persona a profile claims, walking the pool from its hash.

    *held* maps persona id -> its holders (any sized collection). Ranked by
    fewest holders, then a Fish voice before none, then probe order: the
    first free Fish-capable persona; else the first free one without Fish;
    once every persona is held, the least-held one is shared. Deterministic:
    the same *profile* and *held* always give the same persona.
    """
    start = hash_index(profile)
    order = [POOL[(start + i) % len(POOL)] for i in range(len(POOL))]
    # min keeps the first of equal keys, so ties stay in probe order.
    return min(order, key=lambda p: (len(held.get(p.id) or ()), p.fish is None))


# ------------------------------------------------------------------ profiles


def profile_label(home: Path | None, ctx_name: Any = None, fallback: Path | None = None) -> str:
    """The profile name for a call: *home*'s ``profiles/<name>``, else ``ctx.profile_name``.

    ``ctx.profile_name`` (live, from ``HERMES_HOME`` and any per-task override)
    is ``"custom"`` for a home outside ``<root>/profiles/<name>`` of the real
    root; then the home directory's name is used. *home* is the active home
    behind the plugin data dir; *fallback* names an odd-shaped data dir.
    """
    if home is not None and home.parent.name == "profiles" and not home.name.startswith("."):
        return home.name
    name = ctx_name.strip() if isinstance(ctx_name, str) else ""
    if name and name != "custom":
        return name
    for path in (home, fallback):
        if path is not None and path.name:
            return path.name
    return "custom"


def root_of(home: Path) -> Path:
    """The Hermes root that *home* belongs to (the ``default`` profile's home)."""
    return home.parent.parent if home.parent.name == "profiles" else home


def profile_homes(root: Path) -> list[Path]:
    """The root and every ``<root>/profiles/<name>``.

    Dot-names are skipped, and so is a ``profiles/<name>`` entry that is itself
    a symlink. Directories below a profile home (``plugin-data``, ``onair``)
    are not checked: only the claim file itself is never followed
    (:func:`inspect_record`).
    """
    homes = [root]
    with contextlib.suppress(OSError):
        for entry in sorted((root / "profiles").iterdir()):
            if entry.name.startswith(".") or entry.is_symlink() or not entry.is_dir():
                continue
            homes.append(entry)
    return homes


def _same(a: Path, b: Path) -> bool:
    with contextlib.suppress(OSError):
        return os.path.samefile(a, b)
    return Path(os.path.abspath(a)) == Path(os.path.abspath(b))


def sibling_homes(home: Path | None) -> list[Path]:
    """The other profile homes of *home*'s root."""
    if home is None:
        return []
    return [h for h in profile_homes(root_of(home)) if not _same(h, home)]


def claim_path_of(home: Path) -> Path:
    return home / "plugin-data" / "onair" / CLAIM_FILE


# -------------------------------------------------------------------- claims


@dataclass(frozen=True)
class Claim:
    profile: str
    persona: Persona
    path: Path
    status: str  # "stored", "claimed", "set", or "unstored" (could not be written)
    claimed_at: str = ""
    how: str = ""  # the record's "how": "first-use", "set" or "set --force"


def _stored(profile: str, path: Path, record: Mapping[str, Any]) -> Claim:
    how = record.get("how")
    return Claim(profile, BY_ID[record["persona"]], path, "stored",
                 str(record.get("claimed_at") or ""), how if isinstance(how, str) else "")


def read_record(path: Path) -> dict[str, Any] | None:
    """A claim file's record, or None when missing, unreadable, odd or not in the pool."""
    return inspect_record(path)[0]


def inspect_record(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """``(record, None)``, ``(None, None)`` when there is no file, or ``(None, reason)``.

    The claim file itself is never followed (``O_NOFOLLOW`` on its last
    component: a symlink there is rejected); directories on the way to it
    may be symlinks and are followed. Opened without blocking (a FIFO is not
    a file), and read only when it is a small regular file. Only a persona id
    is ever taken from it.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"cannot open it: {exc.strerror or type(exc).__name__}"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None, "not a regular file"
        if info.st_size > CLAIM_MAX_BYTES:
            return None, f"larger than {CLAIM_MAX_BYTES} bytes"
        data = os.read(fd, CLAIM_MAX_BYTES + 1)
    except OSError as exc:
        return None, f"cannot read it: {exc.strerror or type(exc).__name__}"
    finally:
        os.close(fd)
    try:
        record = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "not valid JSON"
    if not isinstance(record, dict) or not isinstance(record.get("persona"), str):
        return None, "no persona name in it"
    if record["persona"] not in BY_ID:
        return None, f"persona {record['persona']!r} is not in the pool"
    return record, None


def held_by_siblings(home: Path | None) -> dict[str, list[str]]:
    """persona id -> the sibling profiles holding it (their claim files, read-only).

    A ``profiles/<name>`` sibling whose record names another profile holds
    nothing: it is a copy no one has adopted yet (a ``--clone-all`` clone or a
    renamed profile that has not spoken since). The source of a clone still
    holds the persona through its own record, so the clone claims a new one;
    a renamed profile adopts its persona when it next speaks. The root's
    record is always counted, under its recorded name.
    """
    held: dict[str, list[str]] = {}
    root = root_of(home) if home is not None else None
    for sibling in sibling_homes(home):
        record = read_record(claim_path_of(sibling))
        if record is None:
            continue
        is_root = root is not None and _same(sibling, root)
        if not is_root and not _owned(record, sibling.name):
            continue  # an un-adopted copy
        held.setdefault(record["persona"], []).append(_label_of(sibling, root, record))
    return held


def _label_of(sibling: Path, root: Path | None, record: Mapping[str, Any]) -> str:
    """A sibling's name: its ``profiles/<name>`` directory, else the root's recorded name."""
    if root is None or not _same(sibling, root):
        return sibling.name
    name = record.get("profile")
    return name.strip() if isinstance(name, str) and name.strip() else "default"


def _owned(record: Mapping[str, Any], profile: str) -> bool:
    """True when *record* was written for *profile* (a record with no name counts)."""
    name = record.get("profile")
    return not isinstance(name, str) or not name.strip() or name.strip() == profile


def all_claims(home: Path | None, data_dir: Path, profile: str) -> dict[str, list[str]]:
    """persona id -> every holder of this root, this profile included (read-only)."""
    held = held_by_siblings(home)
    mine = read_record(Path(data_dir) / CLAIM_FILE)
    if mine is not None and (_owned(mine, profile) or not held.get(mine["persona"])):
        held.setdefault(mine["persona"], []).append(profile)
    return held


LOCKED = "locked"
NO_LOCK = "none"  # no lock can exist here: no fcntl, no home, no root to open, flock refused
TIMED_OUT = "timeout"  # another holder kept the lock past the timeout


@contextlib.contextmanager
def root_lock(home: Path | None, timeout: float = LOCK_TIMEOUT_S) -> Iterator[str]:
    """Hold an exclusive ``flock`` on *home*'s root directory.

    Yields :data:`LOCKED`, :data:`TIMED_OUT` (held by someone else past
    *timeout*) or :data:`NO_LOCK` (no lock is possible here). Nothing is
    written: the lock is on the directory itself.
    """
    if fcntl is None or home is None:
        yield NO_LOCK
        return
    root = root_of(home)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(root, flags)
    except OSError as exc:
        log.warning("persona claim without a lock: cannot open %s (%s)", root, exc.strerror)
        yield NO_LOCK
        return
    state = NO_LOCK
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                state = LOCKED
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    log.warning("persona claim lock: %s stayed locked %.1f s", root, timeout)
                    state = TIMED_OUT
                    break
                time.sleep(_LOCK_POLL_S)
            except OSError as exc:
                log.warning("persona claim without a lock: flock failed (%s)", exc.strerror)
                break
        yield state
    finally:
        if state == LOCKED:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat()


def write_record(path: Path, record: Mapping[str, Any]) -> None:
    """Write *record* to *path* atomically: a new temp file beside it, fsync, ``os.replace``.

    Raises OSError; the temp file is removed and *path* untouched on failure.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, flags | getattr(os, "O_CLOEXEC", 0), 0o644)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _record(profile: str, persona: Persona, how: str) -> dict[str, Any]:
    return {"version": CLAIM_VERSION, "profile": profile, "persona": persona.id,
            "claimed_at": _now(), "how": how}


def claim(data_dir: Path, profile: str, home: Path | None) -> Claim:
    """This profile's persona: its stored claim, or a new first-use claim.

    *data_dir* is this profile's ``plugin-data/onair``; *home* the profile's
    home (None for an odd-shaped data dir: no siblings, no lock). A claim
    that cannot be written is still returned (``status="unstored"``) so
    speech goes on; the next call tries again.

    A stored record names its profile. One written for another name was
    copied or moved here: a renamed profile keeps its persona while no
    sibling holds it (the record is re-labelled); a clone made with
    ``hermes profile create --clone-all``, whose source still holds the
    persona, claims a new one instead of sharing it.
    """
    path = Path(data_dir) / CLAIM_FILE
    record = read_record(path)
    if record is not None and _owned(record, profile):
        return _stored(profile, path, record)
    with root_lock(home) as lock:
        record, rejected = inspect_record(path)  # another process may have won meanwhile
        if record is not None and _owned(record, profile):
            return _stored(profile, path, record)
        if rejected:
            log.warning("profile %s: ignoring its claim file %s (%s); claiming a persona again",
                        profile, path, rejected)
        held = held_by_siblings(home)
        if record is not None and not held.get(record["persona"]):
            persona, how = BY_ID[record["persona"]], "adopted"  # renamed: keep the voice
        else:
            if record is not None:
                log.info("profile %s: the claim file names %s, whose persona %s is held by %s;"
                         " claiming a new one", profile, record.get("profile"),
                         record["persona"], ", ".join(held[record["persona"]]))
            persona, how = probe(profile, held), "first-use"
        if lock == TIMED_OUT:  # a racing claim may be mid-write: never store a guess
            log.warning("profile %s: persona claim lock timed out; speaking as %s for this "
                        "call only, not stored (the next call claims again)", profile, persona.id)
            return Claim(profile, persona, path, "unstored")
        new = _record(profile, persona, how)
        try:
            write_record(path, new)
        except OSError as exc:
            log.warning("cannot store the persona claim in %s (%s); using %s unstored",
                        path, exc.strerror or type(exc).__name__, persona.id)
            return Claim(profile, persona, path, "unstored")
    log.info("profile %s claimed persona %s (%s)", profile, persona.id, how)
    return Claim(profile, persona, path, "claimed", new["claimed_at"], new["how"])


class ClaimRefused(Exception):
    """``set`` was refused; the message says why and what to do."""


def set_claim(
    data_dir: Path, profile: str, home: Path | None, name: str, *, force: bool = False
) -> Claim:
    """Change this profile's claim to the persona *name*.

    Refused (:class:`ClaimRefused`) for an unknown name, or for a persona a
    sibling holds unless *force*: then both profiles share it. Only this
    profile's claim file is written.
    """
    persona = find(name)
    if persona is None:
        known = ", ".join(p.id for p in POOL)
        raise ClaimRefused(f"no persona called {name!r}; choose one of: {known}")
    path = Path(data_dir) / CLAIM_FILE
    with root_lock(home):
        holders = held_by_siblings(home).get(persona.id, [])
        if holders and not force:
            raise ClaimRefused(
                f"{persona.name} is claimed by {', '.join(holders)}; "
                f"pick a free persona (/onair voice list) or add --force to share it"
            )
        new = _record(profile, persona, "set --force" if force else "set")
        write_record(path, new)
    log.info("profile %s set persona %s%s", profile, persona.id, " (forced)" if force else "")
    return Claim(profile, persona, path, "set", new["claimed_at"], new["how"])


def safe_claim(
    data_dir: Path, ctx_name: Callable[[], Any] | None, home: Path | None
) -> Claim | None:
    """:func:`claim` for the speech path: never raises (None means "no persona")."""
    try:
        name = ctx_name() if ctx_name is not None else None
    except Exception:
        name = None
    try:
        return claim(data_dir, profile_label(home, name, Path(data_dir)), home)
    except Exception as exc:  # a persona problem must never stop speech
        log.warning("no persona for this call (%s): default voices", type(exc).__name__)
        return None
