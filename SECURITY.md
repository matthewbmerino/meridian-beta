# Security Policy

## Privacy model

Meridian is a **single-operator** deployment. All data — positions, NAV history,
theses, decisions, imports, watchlist, prefs, alerts, chats, reports — is the
operator's own and is private to this instance.

- **Authentication** — one operator password (bcrypt-hashed, 12 rounds at
  `data/.auth_credentials` or `MERIDIAN_AUTH_PASSWORD`), exchanged for a signed
  **HttpOnly + Secure + SameSite=Strict** login cookie. Every `/api/*` route
  except `/api/auth`, `/api/health` and `/api/broker/schwab/callback` requires
  the cookie (fail-closed). The cookie binds a digest of the current credential,
  so **rotating the password revokes every outstanding cookie immediately**;
  without that a stolen cookie lived out its full 14-day TTL with no eviction
  path, because the signing secret is shared with the at-rest keys.
- **At-rest encryption** — all PII stores (snapshot DB, theses, decisions,
  imports, prefs, watchlist, alerts, chats, reports, findings, tax ledger) are
  encrypted with AES-256-GCM (`data/crypto.py`). Every write gets a fresh
  random salt (per-write, not per-file) and a fresh nonce, so a (key, nonce)
  pair cannot repeat; no key material is stored with the data. A `.hmac`
  sidecar is written on every write and **verified on every read** — a store
  whose sidecar does not match is refused rather than served, and deleting the
  sidecar disables that check, which is why `data/` is owner-only.
- **Filesystem** — `data/` is `700/600` (dirs/files), owned by the service user.
  The app re-applies `700` to the directory at startup and logs a warning if it
  cannot; `scripts/deploy.sh` additionally forces `700/600` across the tree.
  API keys live in `/etc/meridian/env` (root:root 600) in prod.
- **No data is deleted automatically** — this is the operator's own persistent
  record, not visitor data.

## Reporting a vulnerability

This is a private, single-operator project. If you are the operator and
believe you've found a security issue:

1. **Do not** open a public issue.
2. Contact the operator directly (the repo owner) with a description of the
   issue, the affected endpoint/surface, and a minimal reproduction.

## Security-relevant surfaces

- **Auth gate** — `data/auth.py`, `backend/auth.py` (`require_auth`), the
  `AuthGuardMiddleware` in `backend/main.py`. The API is fail-closed: no
  password configured → 403. Never serve a private route without the gate.
- **Session revocation** — a login cookie is a bearer credential. It binds a
  digest of the current password, so `set_password` evicts every outstanding
  cookie; there is no session store to walk, and rotating the shared secret
  would re-key every encrypted store instead. Logout clears the browser copy
  only — rotate the password to end a leaked cookie everywhere.
- **Agent tool authority** — `agent/tools.py#execute_tool` dispatches by name
  with no per-call authorization; `WRITE_TOOLS` marks the record-mutating six,
  and the agent loop refuses them for the rest of a turn once
  `fetch_page`/`search_web`/`hermes_research` has returned third-party content.
  Every write-tool call is recorded through `data/audit.py`.
- **Quote stream** — `/ws/quotes` authenticates on the handshake (operator
  cookie or service bearer) and caps a subscribe list at 200 symbols, because
  each subscription spends operator-credentialed vendor quota.
- **Login brute-force** — `/api/auth/login` is rate-limited (nginx `login`
  zone) and server-side exponential backoff (`data/auth.py`).
- **At-rest encryption** — `data/crypto.py`. PII stores must read/write through
  the encrypted helpers; never write plaintext to disk.
- **CSV imports** — `data/chase.py`, `data/realized.py` parse untrusted uploads;
  they must never execute or write outside `data/imports/`.
- **API keys** — `/etc/meridian/env` (prod, root-only 600) or `.env` (dev,
  600). Never committed.
- **CORS** — `MERIDIAN_CORS_ORIGINS` restricts browser origins; the backend
  binds to `127.0.0.1` behind nginx (HTTPS + HSTS).
- **Markdown rendering** — LLM-authored report content is sanitized
  (DOMPurify allowlist) before `dangerouslySetInnerHTML`.

## Reporting a data-quality issue

Wrong numbers are a security issue in a portfolio tool. If a metric is
incorrect, report it with the formula-level context (see
`GET /api/meta/methodology/{metric}`) so it can be traced to the source.
