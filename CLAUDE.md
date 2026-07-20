# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

Multi-provider AI account-registration framework, plus a vendored copy of **Sub2API** that can consume the registered accounts.

- **`src/ai_signuper/`** — uv-managed Python package. Currently implements one provider (Grok / xAI) and four output sinks. Designed to grow more providers (OpenAI / Claude / etc.) by dropping a new `providers/<name>.py`.
- **`web/`** — a React 19 + Vite 7 (**npm**, not pnpm) single-page control console served by `web_server.py`. Lets you set round count / concurrency, watch a live event log, list captured accounts, and export them.
- **`./sub2api/`** — full clone of [Wei-Shaw/sub2api](https://github.com/Wei-Shaw/sub2api), an AI API gateway. Consumes upstream account credentials via its admin API and resells them as platform API keys. **Carries its own `.git/`** (a submodule per `.gitmodules`) — keep it as a nested clone; an accidental `git push` from inside it would leak captured credentials to the public upstream repo.
- **`turnstilePatch/`** — Chromium MV3 extension required by every provider that uses Cloudflare Turnstile (currently Grok). See its own README.

End-to-end flow per round (Grok provider):
1. DuckMail (自有域名邮箱 `@msdsj.cyou` / `@msdsj.asia`) provisions a disposable inbox (`mail_otp.py`).
2. DrissionPage drives `https://accounts.x.ai/sign-up?redirect=grok-com` in a visible Chromium.
3. Cloudflare Turnstile is solved using the bundled extension + iframe-internal `MouseEvent.prototype` patch.
4. The post-signup `sso` JWT cookie is captured.
5. **Credential enrichment** (`grok_api.py`, on by default): the `sso` cookie is used to call Grok/xAI billing, user, subscription, and task-usage APIs, assembling a **cockpit-tools-importable `GrokAccount` JSON** object (`result["full_credential"]`).
6. Optionally (`--oauth-exchange`), an xAI Authorization-Code + PKCE loopback flow (`grok_oauth_exchange.py`) upgrades the account to real `refresh_token` / `id_token`.
7. The result is pushed to the active sink(s) — `json` (cockpit-tools JSON, recommended), `txt` (raw `sso` line), and/or `sub2api` (admin API).

**Phase C** — making Sub2API actually *forward* live requests to Grok using these credentials — is still **not implemented**; the `extra.credential_kind` field on each Sub2API account is the hook for it. Note that credential *enrichment* (steps 5–6) IS now implemented; only the gateway-side forwarding is pending.

## Run / Develop

uv-managed: `pyproject.toml` + `.python-version` (3.13) + `uv.lock` at the repo root, `.venv/` is created on first sync. **Do not use pip** — there is no `requirements.txt`.

```bash
uv sync                                                        # creates .venv, installs as editable
uv run python -m ai_signuper grok --count 1                    # one round, default txt sink
uv run python -m ai_signuper grok --count 1 --sink json        # cockpit-tools GrokAccount JSON (recommended)
uv run python -m ai_signuper grok --count 1 --sink json+txt    # combine sinks with "+"
uv run python -m ai_signuper grok --count 0                    # infinite loop (Ctrl-C to stop)
uv run python -m ai_signuper grok --count 5 --sink sub2api     # batch into local Sub2API (needs .env)
uv run python -m ai_signuper grok --count 1 --sink json --oauth-exchange  # also fetch refresh_token via PKCE
```

Console scripts installed by `pyproject.toml`: `ai-signuper` (== `python -m ai_signuper`) and `ai-signuper-web` (the web console API, see below).

### CLI flags (`__main__.py`)
- `--count N` — rounds; `0` = infinite (default `0`).
- `--sink` — `txt` | `json` | `sub2api`, **combinable with `+`** (e.g. `json+txt`); parsed in `_make_sink`, wrapped in `MultiSink` when >1. Default `txt`.
- `--output` — txt sink path (default `output/sso.txt`).
- `--json-output` — json sink dir; one JSON file per account (default `output/credentials`).
- `--batch-size` — sub2api batch size (default `1`).
- `--oauth-exchange` — set `provider.enable_oauth_exchange`; off by default because it needs interactive web authorization and can stall the loop.
- `--headless` — run without a visible window. **Turnstile may detect and reject headless** — kept as an escape hatch, not the default.

### Web console
Local React SPA + a stdlib `ThreadingHTTPServer` (no framework). Backend `web_server.py` → console script `ai-signuper-web`, listens on `127.0.0.1:8765`.

```bash
# dev: two terminals
uv run ai-signuper-web            # API on :8765
cd web && npm install && npm run dev   # Vite on :5173, proxies /api → :8765

# prod: build once, Python serves the static bundle from web/dist/
cd web && npm run build && cd ..
uv run ai-signuper-web            # open http://127.0.0.1:8765
```

- **`web/` uses `npm`** (has `package-lock.json`). Do not confuse with `sub2api/frontend`, which uses `pnpm`.
- API endpoints: `GET /api/accounts`, `POST /api/register` (`{total, concurrency, oauth_exchange}`), `POST /api/register/stop`, `POST /api/accounts/export`.
- The register job runs multiple **concurrent visible browser sessions** in worker threads (requested concurrency clamped to `1..12`, then to `DEFAULT_MAX_CONCURRENCY = 8` to limit local load). This is the one place the "one browser at a time" assumption of the CLI does not hold.
- Console output still lands in the same files: `output/credentials/` (JSON) and `output/sso.txt` (fallback lines).

There is **no test suite, lint config, or CI** for the bot. Validation = run it and watch the visible browser(s). For the Sub2API subtree, see the dedicated section below — its toolchain (Go + pnpm + Postgres + Redis) is completely separate.

`requires-python` is pinned to `>=3.12,<3.14`: the upper bound originally encoded a Mail.tm TLS regression on 3.14+ (now using DuckMail, but the limit is retained for stability). Don't widen it without re-testing the mail step.

### Runtime gotchas
- **Python 3.14 was known-broken** for old Mail.tm TLS (now migrated to DuckMail). `runtime.ensure_stable_python_runtime()` auto re-execs into 3.12/3.13 on Windows (looks under `%LOCALAPPDATA%\Programs\Python`). On other OSes it only prints a warning; the uv `requires-python` upper bound is the real guardrail.
- **Chrome must be forced to Chinese.** All button matchers in `providers/grok.py` (使用邮箱注册 / 注册 / 确认邮箱 / 完成注册) are hardcoded Chinese strings; x.ai renders the page per browser UI language. `runtime.build_chromium_options(lang="zh-CN")` sets `--lang=zh-CN` and `intl.accept_languages` — do not remove those, or the very first `_click_email_signup_button` step times out with `未找到"使用邮箱注册"按钮`.
- The Chromium browser stays **visible by design** (headless is opt-in via `--headless` and discouraged). Turnstile requires real-feeling pointer movement.
- Browser is **fully restarted between rounds** (`session.restart()`) — do not refactor toward cookie/session reuse without explicit ask; it's a deliberate anti-detection choice.

## Architecture

### `src/ai_signuper/` layout

```
__main__.py            # CLI entry; selects provider + sink(s), runs the round loop, MultiSink combiner
web_server.py          # ai-signuper-web: stdlib HTTP API + static web/dist server + concurrent worker pool
runtime.py             # ChromiumOptions builder, DrissionBrowserSession, Python guard, wait_for_cookie, PROJECT_ROOT
mail_otp.py            # DuckMail (@msdsj.*) inbox provisioning + verification-code extraction (provider-agnostic)
grok_api.py            # sso cookie -> Grok/xAI API calls -> cockpit-tools GrokAccount JSON (build_cockpit_grok_credential, fetch_complete_credential)
grok_oauth_exchange.py # xAI Authorization Code + PKCE loopback (127.0.0.1:56121/callback) -> refresh_token/id_token
oauth_authorize.py     # standalone OAuth device-code helper for an already-registered account
providers/
  base.py              # Provider Protocol + RegistrationResult TypedDict + BrowserSession Protocol
  grok.py              # Grok signup state machine (open -> email -> OTP -> profile -> sso -> enrich)
sinks/
  base.py              # Sink Protocol (push, flush)
  txt_file.py          # Append raw sso credential per line to output/sso.txt
  json_credential.py   # Write full cockpit-tools GrokAccount JSON, one file per account, to output/credentials/
  sub2api.py           # POST batch to Sub2API admin /accounts/batch
```

Root-level helper scripts `extract_token.py` (spawn a browser, wait for manual Grok login, grab `sso`) and `extract_token_existing.py` (attach to a Chrome already running with `--remote-debugging-port=9222`) are standalone token-scrapers, separate from the main loop. `docs/grok-oauth-flow.md` is a research index for the OAuth path; `DuckMail接入说明.md` documents the mail migration.

### Provider state machine (`providers/grok.py`)

`GrokProvider.run_round(session)` is the orchestrator:

```
session.open_url(signup_url)
→ _click_email_signup_button(page)
→ _fill_email_and_submit(page)         # uses mail_otp.get_email_and_token
→ _fill_code_and_submit(session, ...)  # uses mail_otp.get_oai_code; survives PageDisconnectedError
→ _fill_profile_and_submit(session)    # solves Turnstile inline via _get_turnstile_token
→ wait_for_cookie(session, "sso")
→ (if fetch_full_credential) grok_api.fetch_complete_credential  # + grok_oauth_exchange when enable_oauth_exchange
→ result["full_credential"] = <cockpit-tools GrokAccount JSON>
```

Class attributes worth knowing: `fetch_full_credential = True` (enrich via `grok_api`) and `enable_oauth_exchange = False` (set by `--oauth-exchange` / the web console's `oauth_exchange` flag).

Critical implementation details — these are the most fragile parts of the codebase:

- **Every form interaction is `page.run_js(...)` inline JS, not Python `.input()`.** x.ai uses React-controlled inputs; the script writes via the native `HTMLInputElement.prototype` setter and clears `_valueTracker` before dispatching `beforeinput` / `input` / `change`. Python-side `.input()` silently desyncs React state — the submit button stays disabled forever. **Do not "simplify" by switching to DrissionPage's high-level input methods.**
- **OTP entry has two code paths in one JS block**: a single aggregate input (`data-input-otp="true"`) and a fallback to per-digit `maxLength=1` boxes. Different x.ai A/B variants ship different DOMs; both must remain.
- **`PageDisconnectedError` is expected**, not a bug. Clicking 确认邮箱 navigates and invalidates the old tab handle. `session.refresh_page()` re-grabs the live tab; `_has_profile_form(page)` is the success signal.
- **Turnstile solver in `_get_turnstile_token()`** reaches into the challenge iframe's shadow root and clicks the `<input>` checkbox. It also re-defines `MouseEvent.prototype.screenX/screenY` *inside the iframe's JS context* — the bundled extension patches the top frame, but the iframe needs its own patch.

### Credential enrichment & OAuth (`grok_api.py`, `grok_oauth_exchange.py`)

- `grok_api.build_cockpit_grok_credential(...)` / `fetch_complete_credential(...)` turn a bare `sso` JWT into the full `GrokAccount` shape cockpit-tools expects: basic info (`auth_mode`, `access_token`, `created_at`), identity (`user_id`, `principal_id`, names, `team_id`), plan/quota, and raw API responses (`billing_raw`, `user_raw`, `subscription_raw`, `task_usage_raw`). APIs called: `cli-chat-proxy.grok.com/v1/billing`, `/v1/user`, `grok.com/rest/subscriptions`, `grok.com/rest/tasks/usage`. On API/quota failure it still emits a valid OAuth-shaped account (possibly missing `refresh_token`/quota) — it never falls back to the `api_key` shape.
- `grok_oauth_exchange.exchange_sso_for_oauth_tokens(...)` runs the xAI Authorization-Code + PKCE flow: opens `auth.x.ai` in the *existing logged-in session*, auto-advances email login / OTP / consent, and listens on **`127.0.0.1:56121/callback`** for the code, then swaps it for tokens. **Cloudflare human verification during this flow is left to the user** on purpose — auto-submitting risks an `Invalid action`. OIDC client id `b1a00492-073a-47ea-816f-4c329264a828`.

### Sinks

`MultiSink` (in `__main__.py`) wraps multiple sinks; a `push`/`flush` failure in one is caught and logged, not fatal.

- **`txt_file.TxtFileSink`** — appends `result["credential"]` (raw `sso`) per line. Synchronous, no batching. Useful as a fallback line even when json/sub2api is primary.
- **`json_credential.JsonCredentialSink`** — writes one file per account (`grok_{timestamp}_{email_hash}.json`) using `result["full_credential"]` when present (else builds a minimal record). This is the cockpit-tools import format.
- **`sinks.sub2api.Sub2ApiSink`** — calls `POST {SUB2API_BASE_URL}/api/v1/admin/accounts/batch` with `x-api-key` header. Builds each entry as `{platform=<provider>, type="apikey", credentials.api_key=<credential>, extra.credential_kind="<provider>_sso_cookie", confirm_mixed_channel_risk=true}`. **`type="apikey"` is a deliberate hack** because Sub2API's `type` field is bound to `oneof=oauth setup-token apikey upstream bedrock` — there is no cookie type. The hack is safe today because Sub2API doesn't yet forward requests to Grok; Phase C will use `extra.credential_kind` to identify these accounts and route them to a separate grok-proxy. **On any failure (network or partial-success batch), failed entries are dumped per-line to `output/sso-failed.txt`** so a bad gateway run doesn't waste a registration.

### `turnstilePatch/`

Chromium MV3 extension. Loaded via `runtime.build_chromium_options(...).add_extension(TURNSTILE_EXTENSION_PATH)` at startup. Two files (`manifest.json`, `script.js`) inject at `document_start` in the `MAIN` world and overwrite `MouseEvent.prototype.screenX/screenY` with realistic random integers, defeating the Chromium CDP fingerprint described in [crbug 40280325](https://issues.chromium.org/issues/40280325). Treat as a vendored dependency.

## Adding a new provider

1. Create `src/ai_signuper/providers/<name>.py`. Implement a class that satisfies `providers.base.Provider`:
   ```python
   class FooProvider:
       name = "foo"                       # used as Sub2API platform field
       signup_url = "https://..."
       chrome_lang = "en-US"              # whatever locale your button matchers expect
       success_cookie_name = "session"

       def run_round(self, session) -> RegistrationResult: ...
   ```
2. Register it in `__main__.py`'s `PROVIDERS` dict.
3. If the page is React/SPA, **copy the JS-injection pattern from `providers/grok.py`** verbatim. Do not call DrissionPage `.input()` on controlled forms.
4. Reuse `mail_otp` for any email-OTP flow. The regex ladder in `_extract_code` already handles xAI / OpenAI / Chinese / generic 6-digit formats.
5. Credential enrichment (`grok_api`) and the `json` sink are Grok-specific — a new provider either fills `result["full_credential"]` itself or relies on `txt`/`sub2api`.
6. Update root `README.md` with a one-line note on the provider and any quirks (locale, special MFA handling, sink behavior).

## `./sub2api/` — vendored gateway

Cloned verbatim from `https://github.com/Wei-Shaw/sub2api.git`. **Authoritative dev docs are inside the subtree**: read `sub2api/README.md` (deployment) and `sub2api/DEV_GUIDE.md` (local dev, CI, pitfalls) before touching it. Notes below are only the cross-cutting parts.

- **Stack:** Go 1.25.7 + Gin + Ent ORM (backend), Vue 3.4 + Vite 5 + Pinia + Vitest (frontend), PostgreSQL 15+, Redis 7+.
- **Frontend uses `pnpm`, not `npm`.** CI runs `pnpm install --frozen-lockfile`; committing only `package.json` without `pnpm-lock.yaml` breaks the build. (Contrast: the top-level `web/` console uses `npm`.)
- **Local dev** (from `./sub2api/`):
  ```bash
  cd backend  && go test -tags=unit ./...
  cd backend  && go test -tags=integration ./...   # needs Postgres + Redis
  cd backend  && golangci-lint run ./...            # requires golangci-lint v2.7
  cd frontend && pnpm install && pnpm dev
  cd frontend && pnpm typecheck && pnpm test:run
  ```
- **Deployment is Docker Compose.** `sub2api/deploy/docker-compose.local.yml` is the production-grade variant (local-dir volumes); the script `sub2api/deploy/docker-deploy.sh` auto-generates `.env` with random `JWT_SECRET / TOTP_ENCRYPTION_KEY / POSTGRES_PASSWORD`.
- **Admin API surface relevant to the bot:**
  - Auth: `x-api-key: <admin-api-key>` header (mint at `/admin/settings → Admin API Key`). Implemented in `sub2api/backend/internal/server/middleware/admin_auth.go:47-55`.
  - Single create: `POST /api/v1/admin/accounts` (`account_handler.go:505`).
  - Batch create: `POST /api/v1/admin/accounts/batch` with `{"accounts": [CreateAccountRequest...]}` (`account_handler.go:1157`). Response: `{"success", "failed", "results"}`. Partial failures still return 200.
  - `CreateAccountRequest` fields: `name, platform, type (oneof oauth setup-token apikey upstream bedrock), credentials (JSONB), extra (JSONB), group_ids, expires_at, auto_pause_on_expired, confirm_mixed_channel_risk`.
- **Nginx note** (production): when Sub2API sits behind Nginx, add `underscores_in_headers on;` — Nginx drops `session_id` by default, breaking sticky-session routing across upstream accounts.
- **Never `git push` from inside `sub2api/`** — its `.git/` points at the upstream public repo; `output/` and any captured credential could leak.

## Sub2API integration (sink)

Wiring lives in `src/ai_signuper/sinks/sub2api.py`. It's a finished implementation, not a TODO. Two operational notes:

- **Account `type` is hacked to `apikey`.** Sub2API's `type` field has no cookie option; the `sso` JWT is stored under `credentials.api_key`. The Phase C grok-proxy will need to filter by `extra.credential_kind="grok_sso_cookie"` to identify these and avoid Sub2API's built-in apikey forwarding logic — which today is dormant for `platform="grok"` because no upstream forwarder exists.
- **Failed entries always go to `output/sso-failed.txt`** (not just dropped). On total batch failure (network / 500), every entry's credential is appended; on partial-success, only the entries reported as `success: false` in the response.

## Configuration (`.env`)

Root `.env` is gitignored (`.env.example` is the template). Keys:

- `DUCKMAIL_API_KEY` — required for the mail step; provisions `@msdsj.*` inboxes.
- `DUCKMAIL_DOMAIN` — inbox domain, default `@msdsj.cyou` (also supports `@msdsj.asia`).
- `SUB2API_BASE_URL` — Sub2API instance (local default `http://localhost:8080`).
- `SUB2API_ADMIN_API_KEY` — minted at Sub2API `/admin/settings`.
- `SUB2API_DEFAULT_GROUP_IDS` — optional, comma-separated group IDs for new accounts.

Also gitignored: `output/sso*.txt`, `web/dist/`, `web/node_modules/`.
