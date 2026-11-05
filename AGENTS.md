# AGENTS.md — hermes-onair

On Air: a Hermes plugin that gives every agent a free voice and a show.
Python 3.11+, stdlib-only host side; tests with pytest, lint with ruff.
Build plan: `docs/plans/onair.md`. Workflow config: `.idd.yml`.

## Hard rules

These are not negotiable. A change that breaks one is wrong, even if tests pass.

1. **No paid engines or models.** Call an OpenRouter model only if it appears
   in the public model list (a copy up to 24 h old is fine) and either its id
   ends in `:free` or every listed price is zero. A non-zero listed price means
   paid, whatever the id. If there is no list and no cache, or the check fails,
   skip OpenRouter. This rule covers every model the plugin itself calls,
   including calls made through `ctx.llm`. No paid runners, services, or CI
   minutes either.
2. **The API key comes only from the environment.** Read `OPENROUTER_API_KEY`
   from env. Never put it in a file, config, log, print, error message,
   fixture, or commit.
3. **edge-tts output is for private use only.** Delivering it to the requesting
   user's own chat or session is fine. Never publish it: no public post, plugin
   catalog entry, demo, or release asset.
4. **DropCast is reached only through `adapter.py`.** No other module imports
   or calls DropCast. Use its CLI JSON contract. DropCast's Python internals
   are not public, so if you must call them, do it only inside `adapter.py`.
   DropCast stays pinned to commit `821715c`
   (`git+ssh://git@github.com/luongnv89/dropcast@821715c`). Don't bump the pin
   unless an issue asks for it. Installing the pinned sidecar
   (`hermes onair setup`) is allowed outside `adapter.py`; keep the pin string
   in one place.
5. **The default test run never touches the network.** Block non-loopback
   sockets in `conftest.py` and fake the HTTP layer. Only tests marked `live`
   may use the network; they are deselected by default (`-m 'not live'`) and
   are the only tests exempt from the socket block.

## Commands

```sh
ruff check .
pytest -q          # offline; `pytest -m live` only by hand
```

## Conventions

- Run Hermes only in an isolated `HERMES_HOME` outside `~/.hermes`.
- Branches: `{type}/{issue}-{slug}`. PRs are squash-merged. The PR title and
  body become the commit.
