# Spec 001 — Sign in with GitHub: backend implementation steps

_Status: steps only, nothing implemented · Repository: `dmc-268-api-t4` (FastAPI) ·
Frontend counterpart: [`dmc-268-ui-t4/docs/specs/001-github-auth.md`](../../dmc-268-ui-t4/docs/specs/001-github-auth.md) (implemented) ·
Backend design: `dmc-268-api-t4/docs/BACKEND_ARCHITECTURE.md`, "Authentication"_

These steps let a real GitHub account sign in to the console. Today the backend only
serves `GET /` and `GET /health`. Each step is test-first, and no test calls GitHub.

## 1. The agreed contract

Three rules keep the frontend and the backend in sync:

1. **Paths: the backend's.** Routes have no `/api` prefix: `/me`, `/auth/logout`, …. The
   SPA keeps calling `/api/...` (`VITE_API_BASE_URL=/api`). In development the Vite proxy
   removes the `/api` prefix; in production the reverse proxy does.
2. **Sign-in start: the frontend's.** The start URL is `/auth/github`, with no `/start`
   suffix. In the backend this is the generic route `/auth/{provider}` with `provider` =
   `github`, so the backend's provider abstraction stays intact.
3. **Cookie:** `__Host-session; HttpOnly; Secure; SameSite=Strict; Path=/`, as in Spec 001
   §8.

| Browser URL (public)           | Backend route                 | Method | Spec 001 |
| ------------------------------ | ----------------------------- | ------ | -------- |
| `/api/auth/github?return_to=…` | `/auth/{provider}` (`github`) | GET    | §5       |
| `/api/auth/github/callback?…`  | `/auth/{provider}/callback`   | GET    | §5       |
| `/api/me`                      | `/me`                         | GET    | §5       |
| `/api/auth/logout`             | `/auth/logout`                | POST   | §5       |

The rest of the contract is unchanged from Spec 001:

- The callback redirects to `/auth/callback?result=…&return_to=…`. `result` is one of
  `success`, `access_denied`, `state_mismatch` or `server_error`.
- `GET /me` returns the Spec 001 §5 shape: `user`, `organizations` with
  `role: "owner" | "member"`, and a nullable `current_organization_id`. A missing session
  gets `401 { "error": "no_session" }`.
- `POST /auth/logout` returns `204`, and is idempotent: it answers `204` without a session
  too.
- **CSRF:** every `POST`/`PUT`/`PATCH`/`DELETE` needs `X-Requested-With: fetch` and an
  `Origin` equal to the app's origin. Anything else gets `403 { "error": "csrf" }`.
- Authenticated responses carry `Cache-Control: no-store`.
- **Idle timeout:** 1 hour, configurable, sliding. Every authenticated request extends
  it, and the cookie's `Max-Age` is re-sent.
- `return_to` is checked by the same rules as the frontend's `sanitizeReturnTo`: it must
  start with `/`, must not start with `//` or `/\`, has no control characters, and must
  not point at `/login` or `/auth/*`.

## 2. Before coding: the GitHub App (done by a person)

Use the review service's existing GitHub App (backend design: "Reuse the existing App's
user-to-server flow — no separate OAuth App").

1. **Callback URL:** in the app's settings → "Identifying and authorizing users", add one
   per environment, as the browser sees them:
   - development: `http://localhost:5173/api/auth/github/callback`;
   - production: `https://<console host>/api/auth/github/callback`.

   Check that GitHub saves the localhost URL. If it refuses, use
   `http://127.0.0.1:5173/...` and open the console on that address.

2. **Leave "Expire user authorization tokens" on.** The token is used once, at sign-in.
3. **Permissions:** Organization permissions → **Members: Read-only**, needed to read the
   user's role in an organisation. GitHub Apps have no OAuth scopes, so the backend
   design's "`read:user`" does not apply.
4. **Credentials:** copy the **Client ID** and generate a **Client secret**. Both go into
   the backend's `0600` env file, as the design prescribes for secrets; never into a
   repository.
5. **Install the app** on at least one organisation, or on your own account, so the
   console has something to show after sign-in.

## 3. Configuration

New settings, read once at startup with `pydantic-settings`. Missing required values stop
the app with a clear error.

| Variable                  | Example (development)                            | Notes                                                 |
| ------------------------- | ------------------------------------------------ | ----------------------------------------------------- |
| `GITHUB_CLIENT_ID`        | `Iv23li…`                                        | required                                              |
| `GITHUB_CLIENT_SECRET`    | —                                                | required; env file only                               |
| `GITHUB_CALLBACK_URL`     | `http://localhost:5173/api/auth/github/callback` | the **public** URL registered on GitHub (with `/api`) |
| `APP_ORIGIN`              | `http://localhost:5173`                          | used for the CSRF `Origin` check                      |
| `SESSION_TTL_SECONDS`     | `3600`                                           | idle timeout                                          |
| `OAUTH_STATE_TTL_SECONDS` | `600`                                            | how long a sign-in attempt stays valid                |

`GITHUB_CALLBACK_URL` is explicit rather than derived: the backend never sees the `/api`
prefix, but GitHub compares `redirect_uri` with the registered URL character for
character.

## 4. Implementation steps

The files follow the backend's "Planned code layout". The database does not exist yet, so
sessions and sign-in attempts get **in-memory** adapters behind ports. The PostgreSQL
adapter in `adapters/db/auth.py` replaces them later, without touching the domain or the
API.

### Step 0 — Tooling

- Add the dependencies:
  - runtime: `httpx` (GitHub calls), `pydantic-settings`;
  - test: `pytest`, `pytest-asyncio`.
- FastAPI's `TestClient` drives the API in tests.
- GitHub responses in tests come from `httpx.MockTransport` with recorded JSON fixtures in
  `tests/fixtures/github/`. No test opens a network connection.
- Add `pytest` to the README.

### Step 1 — Domain (`domain/tenancy.py`, `domain/auth.py`), pure Python

Tests first: `tests/domain/test_return_to.py`, `test_sign_in.py`, `test_session.py`.

- `User` (`id`, `login`, `name`, `avatar_url`, `is_platform_admin`), `Organization`
  (`id`, `login`, `name`, `avatar_url`, `role`) and `Session`.
  - `Session` holds the hashed id, the user, the organisations, the current organisation
    id, `created_at`, `last_seen_at` and `expires_at`.
  - `is_platform_admin` is `False` until staff accounts exist.
- `sanitize_return_to(value) -> str | None`: the same rules and the same test cases as the
  frontend's `return-to.test.ts`, so both sides refuse exactly the same inputs.
- `SignInResult`: the four outcomes.
- **Session rules, all taking an injected `Clock`:**
  - `is_expired(now)`;
  - `touch(now, ttl)`, which slides the expiry forward;
  - `current_organization`: the first organisation by `login`, or `None`.

### Step 2 — Ports (`domain/ports.py`)

| Port                 | Methods                                                                                                                        |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `IdentityProvider`   | `authorize_url(state, code_challenge) -> str`; `exchange_code(code, code_verifier) -> Token`; `load_profile(token) -> Profile` |
| `SessionStore`       | `create(session)`, `get(id_hash)`, `save(session)`, `delete(id_hash)`                                                          |
| `SignInAttemptStore` | `put(attempt_id, state, code_verifier, return_to, expires_at)`; `take(attempt_id)`, which is **single-use**                    |
| `Clock`              | `now()`                                                                                                                        |

`Profile` holds the user and their organisations with roles, already mapped to the
domain. The token never leaves the adapter call that uses it.

### Step 3 — GitHub adapter (`adapters/identity/github.py`)

Tests first: `tests/adapters/test_github_identity.py`, against recorded fixtures. Include
error responses and a user whose `name` is `null`.

1. `authorize_url`: `https://github.com/login/oauth/authorize` with `client_id`,
   `redirect_uri` (`GITHUB_CALLBACK_URL`), `state`, `code_challenge` and
   `code_challenge_method=S256`.
2. `exchange_code`:
   - `POST https://github.com/login/oauth/access_token` with `client_id`,
     `client_secret`, `code`, `redirect_uri` and `code_verifier`, plus
     `Accept: application/json`;
   - the response has `access_token`, `expires_in`, `refresh_token`, `token_type`;
   - any error field or non-200 status raises `IdentityProviderError`.
3. `load_profile`, using the user token:
   - `GET https://api.github.com/user`. `name` falls back to `login` when it is `null`, and
     `avatar_url` is kept as is (the frontend drops non-https values).
   - `GET https://api.github.com/user/installations`: the installations of this app that
     the user can access. Each `account` becomes an organisation candidate.
   - For `account.type == "Organization"`:
     `GET https://api.github.com/user/memberships/orgs/{login}`. `role: "admin"` maps to
     `owner` and `"member"` to `member`; `state: "pending"` leaves the organisation out.
   - For `account.type == "User"` with `account.id == user.id` (the app installed on the
     user's own account): an organisation entry with role `owner`. This follows the backend
     design's account-match rule ("Installation account id equals the authenticated user
     id").
   - Organisation `id` is the GitHub account id as a string; `name` falls back to `login`.
4. The token is discarded after `load_profile`, as the backend design requires: never
   stored, never logged.

### Step 4 — In-memory stores (`adapters/memory/auth.py`)

Tests first: one conformance suite in `tests/ports/test_session_store.py` and
`test_sign_in_attempt_store.py`. The later PostgreSQL adapter must pass the same suite.

- Sessions are keyed by `sha256(cookie value)`; the raw value is never stored (ER model,
  `SESSIONS`).
- `take()` removes the attempt, so a replayed `state` finds nothing.
- Expired entries are ignored on read and purged on write.

### Step 5 — Request plumbing (`api/deps.py`)

Tests first: `tests/api/test_deps.py`.

- **`current_session` dependency:**
  - reads `__Host-session`, hashes it and loads the session;
  - a missing or expired session raises `401 {"error": "no_session"}`;
  - otherwise it calls `touch()` and re-sends the cookie with `Max-Age=SESSION_TTL_SECONDS`
    (the sliding idle timeout).
- **CSRF middleware:** for unsafe methods, require `X-Requested-With: fetch` and
  `Origin == APP_ORIGIN`; otherwise `403 {"error": "csrf"}`. `GET` and `HEAD` pass.
- **`Cache-Control: no-store`** on every response that used `current_session`, and on the
  auth routes.
- **The session cookie**, set by one helper: `__Host-session=<token_urlsafe(32)>;
HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=<ttl>`. The `__Host-` prefix forbids
  a `Domain` attribute.

### Step 6 — Auth routes (`api/auth.py`)

Tests first: `tests/api/test_auth.py`, with a fake `IdentityProvider` and a fixed `Clock`.

**`GET /auth/{provider}`.** `provider` must be `github`; anything else is `404`.

1. Sanitise `return_to`; an unsafe value becomes "none".
2. Create a random `state`, a PKCE `code_verifier` (43–128 characters) and its S256
   `code_challenge`.
3. Store the attempt with `OAUTH_STATE_TTL_SECONDS`, under a random attempt id.
4. Set the attempt cookie: `__Host-oauth=<attempt id>; HttpOnly; Secure; SameSite=Lax;
Path=/; Max-Age=600`. `Lax` is required here: it must come back on GitHub's cross-site
   redirect.
5. `302` to `authorize_url(...)`.

**`GET /auth/{provider}/callback`**, in this order:

1. Read and clear `__Host-oauth`, and `take()` the attempt. A missing cookie, a missing
   attempt or a `state` mismatch gives `result=state_mismatch`.
2. GitHub sent `error=access_denied` gives `result=access_denied`.
3. Exchange the code and load the profile. Any `IdentityProviderError` or timeout gives
   `result=server_error`.
4. Create a session with a **new** random id (never reuse an existing cookie: no session
   fixation), and set `__Host-session`.
5. `302` to `/auth/callback?result=…`, with `return_to` added when the attempt had one.
   The `Location` is relative, so it stays on the console's origin.
6. Every outcome sends `Referrer-Policy: no-referrer`.

**`POST /auth/logout`:** delete the session if there is one, expire `__Host-session`
(`Max-Age=0`) and return `204`. It is idempotent.

**`GET /me`:** uses `current_session`, and returns the Spec 001 §5 JSON built from the
session. The organisations are the snapshot taken at sign-in; a new sign-in refreshes them.

**Tests that must exist** (the backend design's "each is a takeover path" list, plus the
contract):

- **State:** a missing, stale (past its TTL), replayed or mismatched `state` → `state_mismatch`, and no
  session cookie is set;
- **Denied:** `access_denied` → `access_denied`;
- **Exchange failure:** → `server_error`;
- **Success:**
  - redirects to `/auth/callback?result=success&return_to=%2Fruns`;
  - the session cookie has exactly the §1 attributes;
  - an unsafe `return_to` (`//evil.example`) is dropped;
- **`/me`:**
  - the response matches the Spec 001 §5 shape, including `current_organization_id: null`
    when there are no organisations;
  - it answers `401` once the idle timeout has passed;
  - it extends the expiry when used within the timeout;
- **Logout:**
  - `204` with and without a session;
  - the old cookie value is rejected afterwards;
  - `403` without `X-Requested-With`, and `403` with a foreign `Origin`;
- **Logging:** no log record contains the `code`, the `state` or a token (a captured-logs
  assertion).

### Step 7 — App factory (`api/main.py`)

- `create_app(settings, identity_provider, session_store, attempt_store, clock)`. The
  production wiring builds the real adapters; tests pass fakes.
- The existing `/` and `/health` keep working.
- The root `main.py` becomes `from api.main import app`, so `uvicorn main:app` still
  works.
- **Access-log hygiene:** uvicorn's access log prints query strings, which on the callback
  contain the authorization `code`. Install a log filter that drops the query string from
  `/auth/*/callback` lines (backend design: "the authorization code and token never appear
  in a log line").

### Step 8 — Frontend sync (`dmc-268-ui-t4`)

Needed because of rule 1 in §1; done as part of this work, not before it.

- `vite.config.ts`: the `/api` proxy gets `rewrite: (path) => path.replace(/^\/api/, "")`.
- README, "Running against a real backend": the backend runs on `http://localhost:8000`,
  and the proxy removes the `/api` prefix.
- `.env.local` for a real sign-in: `VITE_ENABLE_MOCKS=false`,
  `API_PROXY_TARGET=http://localhost:8000`.
- No change to the auth module: the paths it calls (`/api/auth/github`, `/api/me`,
  `/api/auth/logout`) are the public ones in §1.

### Step 9 — Documentation sync (`dmc-268-api-t4/docs/BACKEND_ARCHITECTURE.md`)

Update the "Authentication" section:

- the login table: start `GET /auth/{provider}` (no `/start`), callback unchanged;
- the cookie row: `__Host-session`, `SameSite=Strict`; and the `SameSite=Lax` attempt
  cookie `__Host-oauth`;
- the `GET /me` row: the Spec 001 §5 shape (organisations with roles, the current
  organisation);
- the GitHub "Scopes" row: no scopes for GitHub Apps; **Members: Read-only** permission
  instead;
- a note that a reverse proxy serves the API under `/api` and removes the prefix.

### Step 10 — Manual end-to-end check

Use Chrome or Firefox. They accept `Secure` cookies on `http://localhost`; Safari does
not.

1. Backend: `uvicorn main:app --reload`, with the §3 env file.
2. Frontend: `pnpm dev`, with the `.env.local` from Step 8.
3. Walk through Spec 001 §10:
   - `/runs` → `/login?returnTo=/runs` → GitHub → back on `/runs`;
   - `/login` directly → `/auth/success` with your organisations;
   - DevTools → Application: `__Host-session` is `HttpOnly`, `Secure` and `Strict`, and it
     is absent from `document.cookie`;
   - **Cancel** on GitHub → "Sign-in was cancelled";
   - with two tabs open, sign out in one → both end on `/login`;
   - set `SESSION_TTL_SECONDS=60`, wait a minute, click a link → `/login` with your path
     kept.
4. Expected gap: Runs and Repositories show "Could not load…" until their endpoints exist.

## 5. Out of scope

- Storing users, identities and sessions in PostgreSQL (the in-memory adapters are
  replaced once the database exists).
- Linking a second login, GitLab, and API keys.
- Switching organisations.
- The runs and repositories endpoints.
- Refreshing the organisation snapshot during a session.

## 6. Differences from the backend design, and how they are settled

| Topic           | Backend design (before)              | Settled                                                                |
| --------------- | ------------------------------------ | ---------------------------------------------------------------------- |
| Path prefix     | none                                 | none in the backend; the proxy removes `/api` (Step 8)                 |
| Sign-in start   | `/auth/{provider}/start`             | `/auth/{provider}`, i.e. `/auth/github` (the frontend's path)          |
| Session cookie  | `HttpOnly Secure SameSite=Lax`       | `__Host-session`, `SameSite=Strict`; `Lax` only for the attempt cookie |
| `GET /me`       | user, identities, reachable accounts | Spec 001 §5 shape                                                      |
| GitHub scopes   | `read:user`                          | none (GitHub App); Members: Read-only permission                       |
| Session storage | PostgreSQL `sessions`                | in-memory adapter first, behind `SessionStore`                         |
