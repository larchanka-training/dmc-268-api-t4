# Spec 002 — Account layout: backend implementation steps

_Status: steps only, nothing implemented · Repository: `dmc-268-api-t4` (FastAPI) ·
Builds on: [001-github-auth-backend.md](./001-github-auth-backend.md) (sign-in,
sessions), which is **implemented**; its Step 0 amendments below have been applied_

These steps serve the console's repository features from the backend:

- `GET /repositories` (the list);
- `GET /repositories/connect-url` (where to connect more);
- the GitHub App's **Setup URL**, which brings the user back after connecting.

Every step is test-first, and no test calls GitHub.

## 1. The agreed contract

The rules from `dmc-268-api-t4/docs/001-github-auth-backend.md` §1 apply unchanged:

- backend routes have no `/api` prefix, and the proxy removes it;
- the session cookie is `__Host-session`, `SameSite=Strict`;
- a missing session gets `401 {"error": "no_session"}`;
- authenticated responses carry `Cache-Control: no-store`.

| Browser URL (public)                  | Backend route               | Method | Spec 002 |
| ------------------------------------- | --------------------------- | ------ | -------- |
| `/api/repositories?cursor=`           | `/repositories`             | GET    | §5       |
| `/api/repositories/connect-url`       | `/repositories/connect-url` | GET    | §5, §6   |
| `/api/repositories/{id}/disconnect-url` | `/repositories/{id}/disconnect-url` | GET | §5, §6 |
| `/api/github/setup?installation_id=…` | `/github/setup`             | GET    | §5, §6   |

`/github/setup` is reached by **GitHub's redirect**, not by the SPA. It always ends on
`/repositories?connected=1`, where the frontend refreshes the list and shows the banner.

## 2. Two design rules this follows

Both come from the backend design and GitHub's docs, and they shape everything below.

1. **No `repositories` table.** The backend design keeps forge state out of the database
   ("stores our tenancy, not mirrored forge state … There is still no `repositories`
   table"). The list is therefore read **live from GitHub** with the installation token,
   and kept in a short cache. At the design's scale ("under 100 repositories") one GitHub
   request returns everything.
2. **Never trust the Setup URL's parameters.** GitHub warns: "Bad actors can hit this URL
   with a spoofed installation_id … you should not rely on the validity of the
   installation_id parameter", and recommends checking the installation with the user's
   access token. The backend discards that token after sign-in, so `/github/setup` sends
   the user through sign-in again. GitHub skips the consent screen for a user who already
   authorised the app. The sign-in callback then reads `/user/installations` with a fresh
   user token, which **verifies** the installation and **refreshes** the session's
   organisations in one go. `installation_id` and `setup_action` are never read.

## 3. Contract change: `connected_at` becomes nullable

Spec 002 §5 requires `connected_at` on every repository. GitHub does not record when a
repository was added to an installation, and rule 1 forbids keeping our own copy. So:

- `connected_at` becomes **nullable**; the backend sends `null` for every repository.
- The frontend shows "Connected {date}" only when it is not null (Step 7).
- If the backend ever records the moment, for example from the `installation_repositories`
  webhook, it can fill the field without another contract change.

`last_run_at` stays as specified. It is the newest review job for that repository, and
`null` until the review jobs exist, which they do not yet.

## 4. Before coding: the GitHub App (done by a person)

In the same app as Task 1:

1. **Setup URL**, one per environment, as the browser sees it:
   - development: `http://localhost:5173/api/github/setup`;
   - production: `https://<console host>/api/github/setup`.
2. **Turn "Redirect on update" on.** Without it, GitHub only returns after the first
   installation, not after the user changes which repositories the app may access.
3. **Leave "Request user authorization (OAuth) during installation" off.** When it is on,
   GitHub ignores the Setup URL and sends the user to the callback URL instead.
4. **Repository permissions → Metadata: Read-only** (GitHub requires it anyway), needed to
   list the installation's repositories. Members: Read-only stays from Task 1.
5. **Private key:** generate one and store it the way the backend design prescribes for
   signing keys: systemd `LoadCredential` or a `0600` env file, never a repository. Note
   the **App ID** and the app's **slug** (the last part of `https://github.com/apps/<slug>`).

## 5. Configuration

| Variable                         | Example                      | Notes                                             |
| -------------------------------- | ---------------------------- | ------------------------------------------------- |
| `GITHUB_APP_ID`                  | `123456`                     | required; the `iss` of the App JWT                |
| `GITHUB_APP_PRIVATE_KEY_PATH`    | `/run/credentials/…/app.pem` | required; a file, never the key itself in the env |
| `GITHUB_APP_SLUG`                | `review-agent`               | required; for the install URL                     |
| `REPOSITORIES_CACHE_TTL_SECONDS` | `60`                         | how long a fetched list is reused                 |

## 6. Implementation steps

The files follow the backend's "Planned code layout". As in Task 1, nothing needs the
database yet.

### Step 0 — Amend the Task 1 sign-in (do this first)

Task 1 is **implemented and tested**, so these are changes to working code rather than
edits to an unstarted plan. **All four are now applied**; the table records what changed
and where, so the rest of this spec can assume them.

| Task 1 step             | Amendment | Landed in |
| --- | --- | --- |
| Step 1 (domain) | `Organization` gains `installation_id: int` and `account_type`. Both internal: **`GET /me` does not return them** | `domain/tenancy.py` — `account_type` is a domain `AccountType` enum (`organization`/`user`), not GitHub's capitalised strings, so the adapter maps onto it |
| Step 3 (GitHub adapter) | `load_profile` keeps `installation.id` and `installation.account.type` for every organisation | `adapters/identity/github.py` — an installation with a non-numeric id, or an account type that is neither, is skipped rather than guessed at |
| Step 6 (callback) | Delete any session the request already carries before creating the new one | `api/auth.py` — one browser, one session, so signing out ends it |
| Step 6 (tests) | `/me` carries neither field; a second sign-in leaves exactly one session | `tests/api/test_auth.py`, `tests/adapters/test_github_identity.py` |

### Step 1 — Domain (`domain/repositories.py`), pure Python

Tests first: `tests/domain/test_repositories.py`.

- `Repository`: `id`, `owner`, `name`, `private`, `default_branch`, `html_url`,
  `connected_at` (always `None` for now) and `last_run_at`.
- `sort_by_full_name(repos)`: case-insensitive, matching the frontend's `sortByFullName`
  test cases.
- `page(repos, cursor, size=10) -> (items, next_cursor, total_count)`:
  - the cursor is the offset as a string;
  - a malformed or negative cursor → `ValueError`, which the API turns into `400`;
  - `total_count` counts the whole sorted list.
- `connect_url(organization | None, app_slug) -> str`:
  - organisation account → `https://github.com/organizations/{login}/settings/installations/{installation_id}`;
  - personal account → `https://github.com/settings/installations/{installation_id}`;
  - no organisation → `https://github.com/apps/{app_slug}/installations/new`.

### Step 2 — Ports (`domain/ports.py`)

| Port                  | Methods                                                                                    |
| --------------------- | ------------------------------------------------------------------------------------------ |
| `InstallationGateway` | `list_repositories(installation_id) -> list[Repository]`                                   |
| `RepositoriesCache`   | `get(installation_id)`, `put(installation_id, repos, expires_at)`, `drop(installation_id)` |

`last_run_at` comes from a `ReviewJobs` port once the jobs exist; until then the service
sets `None`.

### Step 3 — GitHub App authentication (`adapters/github/app_auth.py`)

Tests first: `tests/adapters/test_github_app_auth.py`, with recorded fixtures and a fixed
clock.

- **App JWT:** RS256, signed with the private key; `iss` = `GITHUB_APP_ID`, `iat` = now −
  60 s, `exp` = now + 9 min. This needs `PyJWT[crypto]`.
- **Installation token:** `POST https://api.github.com/app/installations/{id}/access_tokens`
  with the JWT. Cache it per installation until 5 minutes before its `expires_at`.
- **Errors:**
  - `404` (uninstalled) and `403` (suspended) → `InstallationGone`;
  - other failures → `ForgeUnavailable`.
- Neither the JWT nor the token is ever logged.

### Step 4 — Repository listing (`adapters/github/installation.py`)

Tests first: `tests/adapters/test_github_installation.py`, with recorded pages, including
a second page and a repository with `default_branch` missing.

- `GET https://api.github.com/installation/repositories?per_page=100&page=N` with the
  installation token. Follow pages until all `total_count` items are read, with a hard cap
  of 10 pages (1,000 repositories, ten times the design's scale).
- Mapping: `id` = `str(repo.id)`, `owner` = `repo.owner.login`, `name`, `private`,
  `default_branch` (`"main"` when absent), `html_url`, `connected_at` = `None`.

### Step 4b — In-memory repositories cache (`adapters/memory/repositories.py`)

Tests first: one conformance suite in `tests/ports/test_repositories_cache.py`, so the
Redis or PostgreSQL cache that replaces it later passes the same tests — exactly as
`tests/ports/test_session_store.py` already does for `SessionStore`.

Step 2 declares `RepositoriesCache` but no step built it; without this one the service in
Step 5 has nothing to call.

- Keyed by `installation_id`; takes an injected `Clock`, like the Task 1 stores.
- `get` returns `None` for a missing **or expired** entry, and drops the expired one.
- `put` overwrites and purges other expired entries.
- `drop` is silent for a key that is not there, so `/github/setup` need not check.

### Step 5 — The repositories service (`domain/service.py`, or `domain/repositories.py`)

Tests first: `tests/domain/test_repositories_service.py`, with fake ports.

- `list_page(session, cursor)`:
  1. No current organisation → empty page (`items: []`, `next_cursor: null`,
     `total_count: 0`), without asking GitHub.
  2. Otherwise take the cached list for the organisation's `installation_id`, or fetch it,
     sort it and cache it for `REPOSITORIES_CACHE_TTL_SECONDS`.
  3. Return `page(...)`.
- `InstallationGone` → drop the cache and return an empty page. The console then shows
  "no repositories" rather than an error, and the next sign-in refreshes the organisations.
- `ForgeUnavailable` → propagate; the API answers `502`.

### Step 6 — Routes (`api/repositories.py`, `api/github_setup.py`)

Tests first: `tests/api/test_repositories.py` and `tests/api/test_github_setup.py`, with
fakes and `TestClient`.

**`GET /repositories?cursor=`** (`current_session`):

- `200 { items, next_cursor, total_count }` in the Spec 002 §5 shape (`connected_at:
null`);
- `400 {"error": "bad_cursor"}` for a malformed cursor;
- `502 {"error": "forge_unavailable"}` when GitHub fails.

**`GET /repositories/connect-url`** (`current_session`):

- returns `200 { "url": connect_url(current organisation, GITHUB_APP_SLUG) }`;
- members get the URL too: GitHub refuses non-owners on its side, and the console already
  disables the button for them (Spec 002 §6);
- reading the URL changes nothing, so it stays a `GET` and needs no CSRF header.

**`GET /repositories/{id}/disconnect-url`** (`current_session`):

- `200 { "url": installation_settings_url(current organisation) }`;
- `404 {"error": "not_found"}` when the repository is not in the installation,
  when there is no current organisation, or when the installation is gone;
- the URL is **the same page `connect-url` returns**: GitHub has no page for a
  single repository of an installation. Per repository anyway, so the backend can
  confirm the repository is still connected, can link more precisely if GitHub
  ever allows it, and so the mock knows which one to remove (frontend spec 002 §5).
- answered from the cached list, so it costs no extra forge call. The list can be
  one TTL stale, so a repository removed moments ago may still return `200`; that
  costs only a trip to a settings page where it has already gone.

**`GET /github/setup`** (no session needed):

- drops the repositories cache for every installation in the current session, if there is
  one;
- `302` to `/api/auth/github?return_to=%2Frepositories%3Fconnected%3D1` (the public sign-in
  start);
- never reads `installation_id`, `setup_action` or any other parameter, and logs none of
  them;
- `Referrer-Policy: no-referrer`.

**Tests that must exist:**

- **List:** sorted and paged exactly as the frontend's mock is (10 per page, cursor
  `"10"`, `total_count` over all pages); `connected_at` is null; the empty page without an
  organisation, with GitHub not called; `InstallationGone` → empty page; `ForgeUnavailable`
  → `502`; a bad cursor → `400`; `401` without a session.
- **The cache:** a second request within the TTL does not call GitHub; after
  `/github/setup` it does.
- **Connect URL:** organisation, personal-account and no-organisation URLs; `401` without
  a session.
- **Setup URL:**
  - it redirects to sign-in with that exact `return_to`;
  - a spoofed `installation_id` of someone else's installation changes nothing (no
    session data, no cache, no response difference);
  - it works without a session (the user then simply signs in).
- **End to end, with fakes:** setup → sign-in → callback with a profile that now includes
  the new installation → `/auth/callback?result=success&return_to=%2Frepositories%3Fconnected%3D1`,
  and `GET /me` lists the new organisation.

### Step 7 — Frontend sync (`dmc-268-ui-t4`)

Needed because of §3. Done as part of this work, test-first like everything else.

- **Spec 002 §5:** `connected_at` nullable, with the reason; the Setup URL and "Redirect
  on update" in §6.
- **The repositories adapter:**
  - `connected_at: z.iso.datetime().nullable()`;
  - `Repository.connectedAt: string | null`;
  - a mapper test for `null`.
- **The repositories page:** "Connected {date}" only when `connectedAt` is not null.
- **Mock fixtures:** keep dates on some repositories and `null` on others, so both
  renderings stay exercised.
- **No other change:** the frontend already follows the URL from `connect-url` and handles
  `?connected=1`.

### Step 8 — Documentation sync (`dmc-268-api-t4/docs`)

- `BACKEND_ARCHITECTURE.md`, "Surface": add `GET /repositories`,
  `GET /repositories/connect-url` and `GET /github/setup`. Also note that repository lists
  are read live from GitHub, which keeps "no `repositories` table" true.
- `BACKEND_ARCHITECTURE.md`, "User flow" step 2 ("Install"): the Setup URL re-runs sign-in
  instead of trusting `installation_id`; claiming unclaimed installations still happens at
  sign-in.
- `WORKFLOW_DESIGN.md`, lifecycle events: `installation_repositories.added` / `removed`
  could drop the repositories cache. Optional, since the cache expires within a minute
  anyway.

### Step 9 — Manual end-to-end check

Only a person can do this one: it needs three GitHub accounts in different roles and a
real App installation. What the suite already covers is listed last, so the time goes on
the parts it cannot reach.

Three App settings to check by eye on the App's own settings page:

- **Setup URL (after installation)** = `{APP_ORIGIN}/api/github/setup`
- **Redirect on update** ticked — without it, scenario 3 never fires
- **Callback URL** equal to `GITHUB_CALLBACK_URL`, character for character

Then start both servers (`001-github-auth-backend.md` Step 10) in Chrome or Firefox;
Safari rejects `Secure` cookies on `http://localhost`.

| # | Do this | Expect | If it looks wrong |
| - | ------- | ------ | ----------------- |
| 1 | Sign in as an organisation **owner** | `/repositories` and the sidebar list the installation's repositories in name order with the right count; never-reviewed ones say "Not reviewed yet" | An empty list usually means the session predates the installation — sign out and in. With several installations the console shows only the first by login, `min()` over the login string, so an uppercase login wins |
| 2 | **Connect repository** | GitHub's installation settings for that organisation. Add a repository and save | A 404 means `GITHUB_APP_SLUG` does not match the App's real slug |
| 3 | Let GitHub redirect you back | `/api/github/setup` → sign-in without a prompt → `/repositories` with the "Your repository list is up to date" banner and the new repository. Reload: no banner | Staying on GitHub means **Redirect on update** is off. The banner is `?connected=1`, which the console strips after reading |
| 4 | Sign in as a **member** of the same organisation | The list shows; **Connect** is disabled with the owners-only hint | Everyone looking like a member means the Members permission is missing, or was granted after this installation and needs accepting |
| 5 | Sign in with an account that has **no** installation | "Install the review app" → GitHub's install page → after installing, back on `/repositories` with the new organisation in the account menu | Landing on `/repositories` empty instead means the Setup URL is unset, so GitHub sent the user to its own page |

Waiting up to `REPOSITORIES_CACHE_TTL_SECONDS` (default 60) for a change is the cache, not
a bug; scenario 3 exists so the user never has to.

Two traps worth knowing, both met while building this: `uvicorn --reload` does not re-read
the env file, because it re-imports Python inside the environment it was launched with, so
a new variable needs a full restart; and the organisation list is a snapshot taken at
sign-in, which is the whole reason `/api/github/setup` routes through sign-in again.

**Already automated, so not worth repeating by hand** — the backend half of scenarios 1
and 3–5:

| Scenario | Test |
| -------- | ---- |
| 1 | `tests/api/test_repositories.py::test_the_list_is_sorted_and_paged_like_the_console` |
| 3 | `tests/api/test_github_setup.py::test_signing_in_after_setup_lands_on_the_repositories_page` |
| 4 | `tests/api/test_repositories.py::test_connect_url_is_given_to_members_too`, and the console's `permissions.test.ts` |
| 5 | `tests/api/test_repositories.py::test_connect_url_points_at_the_install_page_without_an_organisation` |

What is left to a human is therefore narrow, and it is the part fakes cannot judge: that
the App's own settings are right, that GitHub's live responses map onto our model, and
that a browser accepts the `__Host-` cookies.

## 7. Out of scope

- Webhook processing (`installation`, `installation_repositories`): HMAC verification and
  the job cancellation rules belong to the review pipeline. The cache's short expiry keeps
  the list fresh without them.
- Claiming installations that arrived before any sign-in (the backend design's "unclaimed"
  flow).
- *Performing* a disconnection from the console. Removing a repository from an
  installation is a user-to-server call, and the user token is discarded after
  sign-in, so the console links out to GitHub rather than writing forge state. The
  `disconnect-url` endpoint only says *where to go*, and whether there is anything
  there to remove.
- `last_run_at` values, which need the review jobs.
- Database persistence of any of this.

## 8. Decisions

They are recorded in
[`dmc-268-ui-t4/docs/specs/decisions.md`](../../dmc-268-ui-t4/docs/specs/decisions.md),
section "Backend steps — Task 2", with the alternatives considered. There is no
`decisions.md` in this repository; the earlier relative link pointed at a file that does
not exist here.
