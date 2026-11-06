"""The persona pool and its first-use claims (issue #5)."""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import random
import threading
import time
from collections import Counter
from pathlib import Path

import pytest

from onair import personas

#: Luong's profiles (issue #5, acceptance criterion 2): 9 names.
REAL_NAMES = ("default", "career", "forge", "freetokens", "french", "mail", "podcast",
              "skillsmith", "travel")
EDGE_LIST = Path(__file__).parent / "data" / "edge_voices.txt"


def edge_catalog() -> dict[str, str]:
    lines = EDGE_LIST.read_text(encoding="utf-8").splitlines()
    return dict(line.split() for line in lines if line and not line.startswith("#"))


# --------------------------------------------------------------------- pool


def test_the_pool_holds_at_least_nine_personas_with_unique_ids():
    assert len(personas.POOL) >= len(REAL_NAMES) == 9
    assert len({p.id for p in personas.POOL}) == len(personas.POOL)
    assert all(personas.find(p.name) is p and personas.find(p.id.upper()) is p
               for p in personas.POOL)


def test_heygen_voices_are_distinct_and_from_the_public_list():
    voices = [p.heygen for p in personas.POOL]
    assert len(set(voices)) == len(voices) == len(personas.HEYGEN_VOICES) == 10
    assert set(voices) == set(personas.HEYGEN_VOICES)


def test_fish_voices_are_distinct_and_only_the_documented_ones():
    with_fish = [p for p in personas.POOL if p.fish]
    ids = [p.fish_id for p in with_fish]
    assert len(set(ids)) == len(ids) == len(personas.FISH_VOICES) == 7
    assert all(len(i) == 32 and int(i, 16) >= 0 for i in ids)
    # The shortfall is explicit: these personas skip Fish (no shared or invented id).
    assert sorted(p.id for p in personas.POOL if p.fish is None) == ["bram", "sabrina", "weston"]
    assert all("fish" not in p.voices() for p in personas.POOL if p.fish is None)


@pytest.mark.parametrize("language", personas.LANGUAGES)
def test_edge_voices_are_distinct_per_language_and_exist(language):
    catalog = edge_catalog()
    voices = [p.edge[language] for p in personas.POOL]
    assert len(set(voices)) == len(voices)
    assert set(voices) <= set(catalog), set(voices) - set(catalog)
    native = [v for v in voices if v.startswith(f"{language}-")]
    if language == "vi":
        # Only 2 native Vietnamese voices exist; the rest are multilingual (the fallback).
        assert sorted(native) == sorted(v for v in catalog if v.startswith("vi-")) and \
            len(native) == 2
        assert all("Multilingual" in v for v in voices if v not in native)
    else:
        assert all(v.startswith(f"{language}-") for v in voices)


def test_the_catalog_counts_match_the_measurement():
    names = edge_catalog()
    counts = Counter(n.split("-")[0] for n in names if n.split("-")[0] in ("en", "fr", "vi"))
    assert counts == {"en": 47, "fr": 13, "vi": 2}


def test_every_persona_keeps_one_gender_on_edge():
    catalog = edge_catalog()
    want = {"woman": "Female", "man": "Male"}
    for persona in personas.POOL:
        for language in personas.LANGUAGES:
            assert catalog[persona.edge[language]] == want[persona.gender], (persona.id, language)


# ------------------------------------------------------------------ homes


def make_root(tmp_path: Path, names=REAL_NAMES) -> Path:
    """A Hermes root: the default profile at the root, the others in profiles/."""
    root = tmp_path / "root"
    for name in names:
        home = root if name == "default" else root / "profiles" / name
        (home / "plugin-data" / "onair").mkdir(parents=True, exist_ok=True)
    return root


def home_of(root: Path, name: str) -> Path:
    return root if name == "default" else root / "profiles" / name


def data_of(root: Path, name: str) -> Path:
    return home_of(root, name) / "plugin-data" / "onair"


def claim(root: Path, name: str, ctx_name: str | None = None) -> personas.Claim:
    home = home_of(root, name)
    label = personas.profile_label(home, ctx_name or name)
    return personas.claim(data_of(root, name), label, home)


def stored(root: Path, name: str) -> str:
    return json.loads((data_of(root, name) / personas.CLAIM_FILE).read_text())["persona"]


# ----------------------------------------------------- acceptance criterion


@pytest.mark.parametrize("order", ["as listed", "reversed"])
def test_nine_real_profiles_get_nine_distinct_stable_personas(tmp_path, order):
    root = make_root(tmp_path)
    names = REAL_NAMES if order == "as listed" else tuple(reversed(REAL_NAMES))
    first = {name: claim(root, name) for name in names}
    assert all(c.status == "claimed" for c in first.values())
    ids = {name: c.persona.id for name, c in first.items()}
    assert len(set(ids.values())) == 9, ids
    # Read again: the same personas, from the stored claims.
    again = {name: claim(root, name) for name in names}
    assert {n: c.persona.id for n, c in again.items()} == ids
    assert all(c.status == "stored" for c in again.values())
    # "Restart": a fresh module (no state survives), claims read from disk only.
    fresh = importlib.reload(personas)
    try:
        after = {n: fresh.claim(data_of(root, n), n, home_of(root, n)).persona.id for n in names}
    finally:
        importlib.reload(personas)
    assert after == ids == {name: stored(root, name) for name in names}
    # 7 Fish voices for 9 profiles: at most 2 go without Fish (review r1, nit 4).
    assert sum(c.persona.fish is None for c in first.values()) == 2


def test_no_claim_order_leaves_more_than_two_real_profiles_without_fish():
    # All 9! orders are checked in the PR; a seeded sample keeps this test fast.
    rng = random.Random(5)  # noqa: S311 - a seeded sample of claim orders, not crypto
    for _ in range(500):
        order = rng.sample(REAL_NAMES, len(REAL_NAMES))
        held: dict[str, list[str]] = {}
        for name in order:
            held.setdefault(personas.probe(name, held).id, []).append(name)
        assert len(held) == 9
        assert sum(personas.BY_ID[i].fish is None for i in held) <= 2


def order_from(profile):
    start = personas.hash_index(profile)
    return [personas.POOL[(start + i) % len(personas.POOL)] for i in range(len(personas.POOL))]


def test_a_new_profile_never_moves_an_existing_claim(tmp_path):
    root = make_root(tmp_path)
    before = {name: claim(root, name).persona.id for name in REAL_NAMES}
    (data_of(root, "newcomer")).mkdir(parents=True)
    newcomer = claim(root, "newcomer").persona.id
    assert newcomer not in before.values()  # the 10th persona
    assert {name: claim(root, name).persona.id for name in REAL_NAMES} == before


# --------------------------------------------------------- collision probe


def test_a_plain_hash_collides_on_the_real_names():
    slots = Counter(personas.hash_index(n) for n in REAL_NAMES)
    assert max(slots.values()) > 1  # why the claim exists


def test_profiles_with_the_same_hash_probe_to_the_next_free_persona(tmp_path):
    same = [n for n in REAL_NAMES if personas.hash_index(n) == personas.hash_index("forge")]
    assert len(same) >= 2  # forge, freetokens and french share a slot
    root = make_root(tmp_path, same)
    got = [claim(root, name).persona for name in same]
    want = [p for p in order_from("forge") if p.fish][:len(same)]  # Fish-capable first
    assert got == want and len({p.id for p in got}) == len(same)


def test_the_probe_starts_at_the_hash_and_skips_held_personas():
    first, second = [p for p in order_from("mail") if p.fish][:2]
    assert first is personas.POOL[personas.hash_index("mail")]  # mail's start has Fish
    assert personas.probe("mail", {}) is first
    assert personas.probe("mail", {first.id: ["forge"]}) is second


def test_the_probe_prefers_a_free_persona_with_a_fish_voice():
    start = personas.POOL[personas.hash_index("forge")]
    assert start.fish is None  # forge hashes to a persona without Fish …
    want = next(p for p in order_from("forge") if p.fish)
    assert personas.probe("forge", {}) is want  # … and skips to the next one with Fish
    fish_held = {p.id: ["x"] for p in personas.POOL if p.fish}
    assert personas.probe("forge", fish_held) is start  # only then a free one without Fish


def test_the_probe_is_deterministic():
    held = {"bob": ["a"], "maya": ["b"]}
    assert len({personas.probe("forge", dict(held)).id for _ in range(20)}) == 1


def test_a_full_pool_shares_the_least_held_persona():
    held = {p.id: ["a"] for p in personas.POOL}
    first_fish = next(p for p in order_from("eleven") if p.fish)
    assert personas.probe("eleven", held) is first_fish  # equally held: Fish first
    pick = order_from("eleven")[3]
    held = {p.id: ["a", "b"] for p in personas.POOL} | {pick.id: ["a"]}
    assert personas.probe("eleven", held) is pick  # the least held wins, Fish or not


# -------------------------------------------------------------------- race


def racing(monkeypatch, names, *, lock: bool):
    """Make two claims scan, then pause, so both would pick the same persona."""
    scan = personas.held_by_siblings
    barrier = threading.Barrier(len(names), timeout=5)

    def slow_scan(home):
        result = scan(home)
        if not lock:
            barrier.wait()  # both have scanned before either writes
        time.sleep(0.05)
        return result

    monkeypatch.setattr(personas, "held_by_siblings", slow_scan)
    if not lock:
        monkeypatch.setattr(personas, "root_lock",
                            lambda home, timeout=0: contextlib.nullcontext(personas.NO_LOCK))


def run_together(root, names):
    results, errors = {}, []

    def work(name):
        try:
            results[name] = claim(root, name).persona.id
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not errors
    return results


SAME_SLOT = ("forge", "freetokens")


def test_two_profiles_claiming_at_once_get_distinct_personas(tmp_path, monkeypatch):
    assert personas.hash_index(SAME_SLOT[0]) == personas.hash_index(SAME_SLOT[1])
    root = make_root(tmp_path, SAME_SLOT)
    racing(monkeypatch, SAME_SLOT, lock=True)
    got = run_together(root, SAME_SLOT)
    assert len(set(got.values())) == 2, got
    assert {n: stored(root, n) for n in SAME_SLOT} == got


def test_without_the_lock_the_same_race_collides(tmp_path, monkeypatch):
    # The race test above is sensitive: the lock is what keeps the claims apart.
    root = make_root(tmp_path, SAME_SLOT)
    racing(monkeypatch, SAME_SLOT, lock=False)
    got = run_together(root, SAME_SLOT)
    assert len(set(got.values())) == 1, got


def test_the_root_lock_excludes_a_second_holder(tmp_path):
    root = make_root(tmp_path, ("forge", "mail"))
    with personas.root_lock(home_of(root, "forge")) as held:
        assert held == personas.LOCKED
        with personas.root_lock(home_of(root, "mail"), timeout=0.2) as second:
            assert second == personas.TIMED_OUT  # same root: someone else holds it
    with personas.root_lock(home_of(root, "mail"), timeout=0.2) as again:
        assert again == personas.LOCKED
    with personas.root_lock(None) as none:
        assert none == personas.NO_LOCK
    assert sorted(p.name for p in root.iterdir()) == ["profiles"]  # no lock file written


def test_a_claim_rechecks_its_own_file_inside_the_lock(tmp_path, monkeypatch):
    root = make_root(tmp_path, ("forge",))
    real = personas.root_lock

    @contextlib.contextmanager
    def lock_then_other_process_wins(home, timeout=personas.LOCK_TIMEOUT_S):
        with real(home, timeout) as held:
            personas.write_record(data_of(root, "forge") / personas.CLAIM_FILE,
                                  {"persona": "theo", "profile": "forge"})
            yield held

    monkeypatch.setattr(personas, "root_lock", lock_then_other_process_wins)
    got = claim(root, "forge")
    assert (got.persona.id, got.status) == ("theo", "stored")


# ------------------------------------------------------- profile resolution


@pytest.mark.parametrize(
    ("home", "ctx", "want"),
    [
        ("/x/profiles/forge", "custom", "forge"),  # outside the real root: home dir name
        ("/x/profiles/forge", "forge", "forge"),
        ("/x/profiles/forge", None, "forge"),
        ("/root/.hermes", "default", "default"),
        ("/tmp/somewhere", "custom", "somewhere"),
        ("/tmp/somewhere", "", "somewhere"),
        ("/tmp/somewhere", 42, "somewhere"),
    ],
)
def test_profile_label(home, ctx, want):
    assert personas.profile_label(Path(home), ctx) == want


def test_an_odd_data_dir_is_named_by_itself():
    assert personas.profile_label(None, "custom", Path("/data/onair")) == "onair"
    assert personas.profile_label(None, "custom") == "custom"
    assert personas.profile_label(None, "work") == "work"


def test_custom_profile_claims_under_its_home_dir_name(tmp_path):
    root = make_root(tmp_path, ("forge",))
    got = personas.safe_claim(data_of(root, "forge"), lambda: "custom", home_of(root, "forge"))
    assert got.profile == "forge"
    assert json.loads((data_of(root, "forge") / "persona.json").read_text())["profile"] == "forge"
    assert got.persona is personas.probe("forge", {})


def test_safe_claim_survives_a_broken_profile_name_and_a_broken_claim(tmp_path, monkeypatch):
    root = make_root(tmp_path, ("forge",))

    def boom():
        raise RuntimeError("no profile")

    assert personas.safe_claim(data_of(root, "forge"), boom, home_of(root, "forge")).profile \
        == "forge"
    monkeypatch.setattr(personas, "claim", lambda *a: (_ for _ in ()).throw(OSError("x")))
    assert personas.safe_claim(data_of(root, "forge"), boom, home_of(root, "forge")) is None


# ------------------------------------------------------------ claim files


def test_the_claim_is_stored_atomically_with_no_leftovers(tmp_path):
    root = make_root(tmp_path, ("forge",))
    got = claim(root, "forge")
    record = json.loads((data_of(root, "forge") / "persona.json").read_text())
    assert record["persona"] == got.persona.id and record["profile"] == "forge"
    assert record["version"] == 1 and record["how"] == "first-use" and record["claimed_at"]
    assert sorted(p.name for p in data_of(root, "forge").iterdir()) == ["persona.json"]


def test_a_failed_write_leaves_the_old_claim_and_no_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "persona.json"
    personas.write_record(path, {"persona": "bob"})

    def fail(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(personas.os, "replace", fail)
    with pytest.raises(OSError):
        personas.write_record(path, {"persona": "alice"})
    assert json.loads(path.read_text()) == {"persona": "bob"}
    assert [p.name for p in tmp_path.iterdir()] == ["persona.json"]


def test_an_unwritable_claim_still_speaks_unstored(tmp_path, monkeypatch):
    root = make_root(tmp_path, ("forge",))
    monkeypatch.setattr(personas, "write_record",
                        lambda *a: (_ for _ in ()).throw(PermissionError(13, "denied")))
    got = claim(root, "forge")
    assert got.status == "unstored" and got.persona is personas.probe("forge", {})


@pytest.mark.parametrize(("content", "reason"), [
    (b"", "not valid JSON"), (b"{", "not valid JSON"), (b"\xff\xfe", "not valid JSON"),
    (b"[]", "no persona name in it"), (b'{"persona": 3}', "no persona name in it"),
    (b'{"persona": "zed"}', "persona 'zed' is not in the pool"),
])
def test_a_bad_own_claim_is_replaced_with_a_warning(tmp_path, caplog, content, reason):
    root = make_root(tmp_path, ("forge",))
    path = data_of(root, "forge") / "persona.json"
    path.write_bytes(content)
    caplog.set_level("WARNING", logger="onair")
    got = claim(root, "forge")
    assert got.status == "claimed" and stored(root, "forge") == got.persona.id
    assert f"profile forge: ignoring its claim file {path} ({reason})" in caplog.text


def test_an_unreadable_own_claim_is_named_in_the_warning(tmp_path, caplog):
    root = make_root(tmp_path, ("forge",))
    path = data_of(root, "forge") / "persona.json"
    os.mkfifo(path)
    caplog.set_level("WARNING", logger="onair")
    claim(root, "forge")  # the FIFO is replaced by a regular claim file
    assert "(not a regular file)" in caplog.text and path.is_file()


def test_a_missing_own_claim_is_not_a_warning(tmp_path, caplog):
    root = make_root(tmp_path, ("forge",))
    caplog.set_level("WARNING", logger="onair")
    assert claim(root, "forge").status == "claimed"
    assert caplog.text == ""


def test_inspect_record_reasons(tmp_path):
    assert personas.inspect_record(tmp_path / "none.json") == (None, None)
    big = tmp_path / "big.json"
    big.write_text(json.dumps({"persona": "bob", "pad": "x" * personas.CLAIM_MAX_BYTES}))
    assert personas.inspect_record(big) == (None, f"larger than {personas.CLAIM_MAX_BYTES} bytes")
    good = tmp_path / "good.json"
    good.write_text('{"persona": "bob"}')
    assert personas.inspect_record(good) == ({"persona": "bob"}, None)


def test_sibling_claim_files_are_only_read(tmp_path):
    root = make_root(tmp_path, ("default", "mail", "forge"))
    claim(root, "default")
    claim(root, "mail")
    files = {n: data_of(root, n) / "persona.json" for n in ("default", "mail")}
    before = {n: (p.read_bytes(), p.stat().st_mtime_ns) for n, p in files.items()}
    claim(root, "forge")
    personas.set_claim(data_of(root, "forge"), "forge", home_of(root, "forge"),
                       stored(root, "mail"), force=True)
    assert {n: (p.read_bytes(), p.stat().st_mtime_ns) for n, p in files.items()} == before


def test_odd_sibling_files_are_ignored_without_blocking(tmp_path):
    root = make_root(tmp_path, ("forge", "mail", "podcast", "travel"))
    os.mkfifo(data_of(root, "mail") / "persona.json")  # opening it must not block
    (data_of(root, "podcast") / "persona.json").write_text("not json")
    target = data_of(root, "forge") / "persona.json"
    personas.write_record(target, {"persona": "maya", "profile": "forge"})
    (data_of(root, "travel") / "persona.json").symlink_to(target)  # never followed
    assert personas.held_by_siblings(home_of(root, "travel")) == {"maya": ["forge"]}
    big = {"persona": "bob", "pad": "x" * personas.CLAIM_MAX_BYTES}
    (data_of(root, "podcast") / "persona.json").write_text(json.dumps(big))
    assert "bob" not in personas.held_by_siblings(home_of(root, "travel"))


def test_siblings_are_the_root_and_its_profiles_only(tmp_path):
    root = make_root(tmp_path, ("default", "forge", "mail"))
    (root / "profiles" / ".hidden" / "plugin-data" / "onair").mkdir(parents=True)
    (root / "profiles" / "link").symlink_to(root / "profiles" / "mail")
    (root / "profiles" / "file").write_text("x")
    assert personas.sibling_homes(home_of(root, "forge")) == [root, root / "profiles" / "mail"]
    assert personas.sibling_homes(root) == [root / "profiles" / "forge", root / "profiles" / "mail"]
    assert personas.sibling_homes(None) == []


def test_a_claim_without_a_home_has_no_siblings_and_no_lock(tmp_path):
    data = tmp_path / "data" / "onair"
    got = personas.claim(data, "solo", None)
    assert got.status == "claimed" and (data / "persona.json").is_file()


# ---------------------------------------------------------------------- set


def test_set_changes_only_this_profiles_claim(tmp_path):
    root = make_root(tmp_path, ("forge", "mail"))
    claim(root, "mail")
    free = next(p for p in personas.POOL if p.id != stored(root, "mail"))
    got = personas.set_claim(data_of(root, "forge"), "forge", home_of(root, "forge"),
                             free.name.upper())
    assert got.persona is free and stored(root, "forge") == free.id
    record = json.loads((data_of(root, "forge") / "persona.json").read_text())
    assert record["how"] == "set"
    assert claim(root, "forge").persona is free  # stable afterwards


def test_set_refuses_a_persona_another_profile_holds(tmp_path):
    root = make_root(tmp_path, ("forge", "mail"))
    mine = claim(root, "forge").persona.id
    theirs = claim(root, "mail").persona.id
    with pytest.raises(personas.ClaimRefused, match=r"claimed by mail.*--force"):
        personas.set_claim(data_of(root, "forge"), "forge", home_of(root, "forge"), theirs)
    assert stored(root, "forge") == mine


def test_set_force_shares_the_persona(tmp_path):
    root = make_root(tmp_path, ("forge", "mail"))
    claim(root, "forge")
    theirs = claim(root, "mail").persona.id
    got = personas.set_claim(data_of(root, "forge"), "forge", home_of(root, "forge"), theirs,
                             force=True)
    assert got.persona.id == theirs == stored(root, "forge") == stored(root, "mail")
    assert json.loads((data_of(root, "forge") / "persona.json").read_text())["how"] \
        == "set --force"


def test_set_refuses_an_unknown_name(tmp_path):
    root = make_root(tmp_path, ("forge",))
    with pytest.raises(personas.ClaimRefused, match="choose one of: bob, alice"):
        personas.set_claim(data_of(root, "forge"), "forge", home_of(root, "forge"), "zed")
    assert not (data_of(root, "forge") / "persona.json").exists()


# ------------------------------------------------ copied and moved claims (review)


def test_a_cloned_profile_claims_its_own_persona(tmp_path):
    # `hermes profile create forge2 --clone-all` copies forge's plugin-data.
    root = make_root(tmp_path, ("forge", "forge2", "mail"))
    source = claim(root, "forge").persona.id
    claim(root, "mail")
    copied = (data_of(root, "forge") / "persona.json").read_bytes()
    (data_of(root, "forge2") / "persona.json").write_bytes(copied)
    # forge2's copy names forge, not forge2: an un-adopted copy holds nothing.
    assert personas.held_by_siblings(home_of(root, "mail"))[source] == ["forge"]
    got = claim(root, "forge2")
    assert got.status == "claimed" and got.persona.id not in (source, stored(root, "mail"))
    record = json.loads((data_of(root, "forge2") / "persona.json").read_text())
    assert record["profile"] == "forge2" and record["how"] == "first-use"
    assert (data_of(root, "forge") / "persona.json").read_bytes() == copied  # never written
    assert claim(root, "forge2").persona is got.persona  # stable afterwards
    assert personas.held_by_siblings(home_of(root, "mail"))[source] == ["forge"]


def test_a_renamed_profile_keeps_its_persona(tmp_path):
    root = make_root(tmp_path, ("forge", "mail"))
    before = claim(root, "forge").persona
    claim(root, "mail")
    (root / "profiles" / "forge").rename(root / "profiles" / "anvil")
    got = claim(root, "anvil")
    assert got.persona is before and got.how == "adopted"
    record = json.loads((data_of(root, "anvil") / "persona.json").read_text())
    assert record["profile"] == "anvil" and record["persona"] == before.id
    assert claim(root, "anvil").status == "stored"


def test_a_copied_claim_is_not_listed_as_this_profiles(tmp_path):
    root = make_root(tmp_path, ("forge", "forge2"))
    source = claim(root, "forge").persona.id
    (data_of(root, "forge2") / "persona.json").write_bytes(
        (data_of(root, "forge") / "persona.json").read_bytes())
    held = personas.all_claims(home_of(root, "forge2"), data_of(root, "forge2"), "forge2")
    assert held == {source: ["forge"]}


# ------------------------------------------------- review r1, nit 1: lock timeout


def test_a_lock_timeout_speaks_unstored_and_the_next_call_claims(tmp_path, monkeypatch,
                                                                caplog):
    root = make_root(tmp_path, ("forge", "mail"))
    caplog.set_level("WARNING", logger="onair")
    real = personas.root_lock
    monkeypatch.setattr(personas, "root_lock", lambda home, timeout=0: real(home, 0.1))
    with real(home_of(root, "mail")) as held:  # another claim holds the root's lock
        assert held == personas.LOCKED
        got = claim(root, "forge")
    assert got.status == "unstored"
    assert not (data_of(root, "forge") / "persona.json").exists()  # nothing written
    assert "lock timed out; speaking as" in caplog.text and "not stored" in caplog.text
    again = claim(root, "forge")  # the lock is free now
    assert again.status == "claimed" and stored(root, "forge") == again.persona.id


@pytest.mark.parametrize("state", [personas.NO_LOCK, personas.LOCKED])
def test_a_claim_is_stored_when_no_lock_can_exist_or_it_is_held(tmp_path, monkeypatch, state):
    root = make_root(tmp_path, ("forge",))
    monkeypatch.setattr(personas, "root_lock",
                        lambda home, timeout=0: contextlib.nullcontext(state))
    assert claim(root, "forge").status == "claimed"
    assert (data_of(root, "forge") / "persona.json").is_file()


# ------------------------------------- review r1, nit 3: un-adopted copies hold nothing


def clone(root, source, target):
    """What `hermes profile create <target> --clone-all` does to the claim file."""
    data_of(root, target).mkdir(parents=True, exist_ok=True)
    (data_of(root, target) / "persona.json").write_bytes(
        (data_of(root, source) / "persona.json").read_bytes())


def test_a_rename_with_an_unspoken_clone_keeps_the_voice(tmp_path):
    root = make_root(tmp_path, ("forge", "mail"))
    voice = claim(root, "forge").persona
    claim(root, "mail")
    clone(root, "forge", "forgeclone")  # not spoken yet
    (root / "profiles" / "forge").rename(root / "profiles" / "anvil")
    renamed = claim(root, "anvil")
    assert renamed.persona is voice and renamed.how == "adopted"
    cloned = claim(root, "forgeclone")  # the clone speaks later: anvil holds the voice now
    assert cloned.persona is not voice and cloned.how == "first-use"
    assert len({voice.id, cloned.persona.id, stored(root, "mail")}) == 3


def test_a_clone_still_claims_its_own_persona(tmp_path):
    root = make_root(tmp_path, ("forge", "mail"))
    voice = claim(root, "forge").persona
    clone(root, "forge", "forgeclone")
    got = claim(root, "forgeclone")
    assert got.persona is not voice and stored(root, "forge") == voice.id


def test_an_unspoken_clone_neither_blocks_set_nor_shows_in_the_list(tmp_path):
    root = make_root(tmp_path, ("forge", "mail"))
    voice = claim(root, "forge").persona
    claim(root, "mail")
    clone(root, "mail", "mailclone")  # a copy of mail's claim, not spoken yet
    mails = stored(root, "mail")
    held = personas.all_claims(home_of(root, "forge"), data_of(root, "forge"), "forge")
    assert held == {voice.id: ["forge"], mails: ["mail"]}
    with pytest.raises(personas.ClaimRefused, match=r"claimed by mail;"):
        personas.set_claim(data_of(root, "forge"), "forge", home_of(root, "forge"), mails)


def test_the_roots_record_always_counts(tmp_path):
    root = make_root(tmp_path, ("default", "forge"))
    personas.write_record(data_of(root, "default") / "persona.json",
                          {"persona": "theo", "profile": "onair-root"})
    assert personas.held_by_siblings(home_of(root, "forge")) == {"theo": ["onair-root"]}
