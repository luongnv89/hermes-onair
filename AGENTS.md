# AGENTS.md — hermes-onair

On Air: a Hermes plugin that gives every agent a free voice and a show.
Python 3.11+, stdlib-only host side; tests with pytest, lint with ruff.
Build plan: `docs/plans/onair.md`. Workflow config: `.idd.yml`.

## Hard rules

These are not negotiable. A change that breaks one is wrong, even if tests pass.

1. **No paid engines or models.** Call an OpenRouter model only when its id ends
   in `:free` or every listed price is zero; check against the public model
   list before each call. When the price check fails or no list is available,
   skip that engine. No paid runners, services, or CI minutes either.
2. **The API key comes only from the environment.** Read `OPENROUTER_API_KEY`
   from env. Never put it in a file, config, log, print, error message,
   fixture, or commit.
3. **edge-tts output is private.** It is for private use only. Never publish it,
   upload it, put it in the plugin catalog, or use it in a demo or release asset.
4. **DropCast is reached only through `adapter.py`.** No other module imports
   or calls DropCast. Use its CLI JSON contract. DropCast's Python internals
   are not public, so if you must call them, do it only inside `adapter.py`.
   DropCast stays pinned to commit `821715c`
   (`git+ssh://git@github.com/luongnv89/dropcast@821715c`). Don't bump the pin
   unless an issue asks for it.
5. **Tests never touch the network.** Block non-loopback sockets in
   `conftest.py`. Fake the HTTP layer. Mark a live smoke test `live` and keep
   it off by default.

## Commands

```sh
ruff check .
pytest -q          # offline; `pytest -m live` only by hand
```

## Conventions

- Run Hermes only in an isolated `HERMES_HOME` outside `~/.hermes`.
- Branches: `{type}/{issue}-{slug}`. PRs are squash-merged. The PR title and
  body become the commit.
