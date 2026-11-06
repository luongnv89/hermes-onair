# hermes-onair
On Air: free voices and two-host audio shows for Hermes Agent (Hermes plugin)

> Status: early scaffold (v0.1.0). This release ships the plugin skeleton and
> `hermes onair setup`; the voice router, personas and `/onair` come next.

## Install

On Air is a directory plugin: the repo root holds `plugin.yaml` and
`__init__.py`. The host side uses only the Python standard library
(`python_runtime: external`), so Hermes installs nothing for it.

```sh
git clone git@github.com:luongnv89/hermes-onair.git
mkdir -p "${HERMES_HOME:-$HOME/.hermes}/plugins"
ln -s "$PWD/hermes-onair" "${HERMES_HOME:-$HOME/.hermes}/plugins/onair"
hermes plugins enable onair
```

## Setup

On Air drives [DropCast](https://github.com/luongnv89/dropcast) in a sidecar
virtualenv, kept apart from Hermes's own environment. It needs
[uv](https://docs.astral.sh/uv/) and SSH access to the DropCast repo.

```sh
hermes onair setup           # create the sidecar and install the pinned DropCast
hermes onair setup --check   # report the sidecar's state; exit 0 when ready
```

- The sidecar lives in `$HERMES_HOME/plugin-data/onair/sidecar`.
- DropCast is installed over `git+ssh` at the commit pinned in `adapter.py`
  (`DROPCAST_PIN`). Setup is safe to run again: a sidecar already at the pin is
  left alone, and one at another commit is reinstalled.
- When uv is missing or the repository cannot be fetched, setup says so and
  exits non-zero.
- Setup only writes to an absent path or its own sidecar: a real directory
  (not a symlink) directly inside the data dir, made by uv (`uv = …` in
  `pyvenv.cfg`), where uv installed DropCast from the DropCast repository at
  the pinned commit (PEP 610 `direct_url.json`). It also needs the
  `.onair-sidecar` marker setup writes right after `uv venv`. A marker alone
  is never enough. Only such a venv is ever recreated (`uv venv --clear`).
  Anything else is left untouched, and setup asks you to move it aside.
  Setup never deletes anything itself.
- A sidecar made by v0.1.0 has no marker. Setup adopts it, by writing the
  marker, when it meets every rule above and has a Python. `--check` never
  writes; it reports such a sidecar as adoptable.
- Move the sidecar aside (`mv …/sidecar …/sidecar.old`) and run setup again
  when setup refuses it: after an install that never finished (for example,
  the repository was unreachable), or after a pin change, since the old
  sidecar no longer matches the pin.

Settings (`plugins.entries.onair.settings` in `config.yaml`):

| Key | Default | Meaning |
|-----|---------|---------|
| `uv_bin` | `""` | Path or name of uv; empty looks it up on `PATH`. |
| `sidecar_python` | `">=3.11"` | Python request for `uv venv --python`. |

## Develop

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
ruff check .
pytest -q            # offline: network, DNS and new processes are blocked
pytest -m live       # by hand only: tests that reach real services
```

- Tests marked `real_uv` may start only the `uv` on `PATH`, through
  `subprocess`, with `env=real_uv_env` (offline). They are skipped when uv is
  absent.
- Read `AGENTS.md` first: its hard rules (free engines only, the key only from
  the environment, edge-tts output for private use, DropCast only through
  `adapter.py`, an offline test run) override everything else.
- Try the plugin in an isolated `HERMES_HOME` outside `~/.hermes`, for example
  `HERMES_HOME=/tmp/onair-home`. Homes inside `~/.hermes` share the real
  install, and `hermes plugins validate --install-deps` would rebuild it.
- Before a PR, also run `hermes plugins validate .` and
  `hermes plugins doctor . --ci` in that isolated home.
- CI runs ruff and the offline tests on Python 3.11 and 3.13
  (`.github/workflows/ci.yml`).
