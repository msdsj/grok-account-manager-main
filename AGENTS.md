# Repository Guidelines
 
## Project Structure & Module Organization

This repository contains the MSDSJ `grok-account-manager` Python automation
package with a React control panel. Python backend source lives in
`serve/grok_account_manager/` with four layers: `api/` (FastAPI routers and
application services), `core/` (database, audit, browser, paths), `gateway/`
(shared model catalog/pool/auth/audit), and `providers/{grok,gpt,kiro}/` (each
business board owns registration, models, and its gateway adapter). `sinks/`
contains output targets such as TXT, JSON credentials, and Sub2API. The React
app lives in `web/src/`, with Vite configuration in `web/vite.config.ts`.
Browser patch assets are in `extensions/turnstile_patch/`. Runtime output is
written under `output/` and should not be treated as source.

## Build, Test, and Development Commands

- `uv sync`: install Python dependencies from `pyproject.toml` and `uv.lock`.
- `./scripts/start.sh`: one-command startup (deps, frontend build, FastAPI).
- `./scripts/update.sh`: pull code, sync dependencies, rebuild frontend.
- `./scripts/verify.sh`: compile checks, full test suite, frontend build, and
  startup smoke checks against `/healthz`, `/readyz`, `/v1/models`.
- `uv run grok-account-manager grok --count 1 --sink json+txt`: run one local
  registration round and write JSON plus TXT outputs.
- `uv run grok-account-manager-api`: start the FastAPI backend on
  `127.0.0.1:43187`; the legacy `grok-account-manager-web` alias points here too.
- `cd web && npm install`: install frontend dependencies.
- `cd web && npm run dev`: start the React dev server on `127.0.0.1:43188`.
- `cd web && npm run build`: run TypeScript checks and produce the Vite build.

## Coding Style & Naming Conventions

Use Python 3.12 or 3.13; the project explicitly excludes 3.14. Follow existing
Python style: 4-space indentation, type hints for public interfaces, small
provider/sink classes, and clear snake_case names. Keep provider modules inside
their business board, for example `providers/grok/registration.py` and
`providers/kiro/gateway/adapter.py`. Frontend code uses strict TypeScript, React
function components, PascalCase component names, and camelCase state/helpers.
Keep UI text consistent with the existing Chinese interface.

## Testing Guidelines

Place Python tests under `tests/` mirroring `serve/grok_account_manager/`, and
name files `test_<module>.py`. Before submitting backend changes run
`uv run python -m unittest discover -s tests`; for frontend changes run
`cd web && npm run build`. Gateway adapters have offline integration tests with
mock upstream servers in `tests/test_gateway_integration.py`.

## Commit & Pull Request Guidelines

This checkout does not include Git history, so no existing commit convention can
be inferred. Use concise, imperative commits with an optional scope, for example
`providers: add retry around Grok quota fetch`. Pull requests should describe
the behavior change, list verification commands, link related issues when
available, and include screenshots for visible `web/src/` UI changes.

## Security & Configuration Tips

Never commit `.env` or generated credentials from `output/`. Use `.env.example`
for documented configuration names, especially `SUB2API_BASE_URL` and
`SUB2API_ADMIN_API_KEY`. Treat browser cookies, SSO tokens, and JSON credential
exports as secrets.
