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
- Setup only writes to its own sidecar: a real directory (not a symlink)
  directly inside the data dir, marked with `.onair-sidecar` right after uv
  creates it. Only a marked venv is ever recreated (`uv venv --clear`).
  Anything else at that path is left untouched, and setup asks you to move it
  aside. Setup never deletes anything itself.
- A sidecar made by v0.1.0 has no marker. Setup adopts it, by writing the
  marker, only when its venv records a DropCast install from the DropCast
  repository (PEP 610 `direct_url.json`). `--check` never writes; it reports
  such a sidecar as adoptable. A v0.1.0 venv whose install never completed
  can't be told apart from a foreign one, so move it aside and run setup again.

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

- Tests marked `real_uv` may start the real `uv` (only it), offline, and are
  skipped when uv is absent.
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
