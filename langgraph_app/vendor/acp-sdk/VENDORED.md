# Vendored: `acp-sdk` 0.1.0

The Agent Production Control Plane SDK, copied in because it is not on PyPI and
the deployment build has no SSH access to the private repo it lives in.

| | |
|---|---|
| Upstream | `github.com/abhibhaw/autopilot`, `packages/python-sdk/` |
| Commit | `33aefa6fa985e7eef40597ecd1f7991f1672d01a` (tip of `v2` when copied) |
| Copied | 2026-09-27, with `git archive` — `src/` is byte-identical to upstream |
| Not copied | `tests/` — they read contracts from elsewhere in the autopilot repo |

## Local patches — `pyproject.toml` only

1. **OpenTelemetry pins widened** from `==1.44.0` to `>=1.42.1,<1.45`. Upstream's
   exact pin cannot co-install with `langgraph-api` 0.15.x, which requires
   `opentelemetry-sdk>=1.42.1,<1.43` — so the platform image would fail to build.
   The SDK's own 93-test suite (including its strict mypy check) was run from the
   upstream checkout against both 1.42.1 and 1.44.0 and passed on each.
2. **`[tool.ruff]` and `[tool.mypy]` removed.** They extend `../../pyproject.toml`,
   the autopilot repo root, which does not exist here.

Better long-term: widen the pin upstream, then this copy is byte-identical again.

## How it is installed

* **LangGraph Platform:** listed in `langgraph.json` `dependencies`, so the build
  installs it as its own local package before `langgraph_app` (it sorts first
  under `/deps/`).
* **uv:** `[tool.uv.sources]` in `langgraph_app/pyproject.toml` points
  `acp-sdk==0.1.0` here.
* **pip:** `requirements.txt` lists `./vendor/acp-sdk`.

## Updating

Re-run the `git archive` of `packages/python-sdk` at the new commit into this
directory, delete `tests/`, re-apply the patches above only if still needed, bump
the commit here, then `uv lock` in `langgraph_app/`. Do not edit `src/` by hand.
