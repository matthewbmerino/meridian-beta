# Configuration

Everything lives in `~/.meridian` (override with `MERIDIAN_HOME`).

- `meridian.toml` — operator config, mode 0600.
- `.env` — secrets, mode 0600.
- `app/data` — the book, the record, and snapshots, mode 0700.
- `reports/` — local product reports, mode 0600; sent only with `--send`.
- `bin/` — a private `uv`, when the installer had to provision Python 3.12.

`meridian config list` prints the config. `meridian config edit` opens it in
`$EDITOR`. `meridian config edit --secrets` opens `.env`.

## meridian.toml

```toml
[book]
source = "demo"          # demo | csv | empty
csv = ""

[server]
api_port = 8001
web_port = 3000
bind = "127.0.0.1"
ui = true

[model]
provider = "none"        # none | ollama-cloud | ollama-local
model = ""
base_url = ""

[market_data]
provider = "yfinance"    # yfinance | massive | polygon | none

[broker]
provider = "csv"         # csv | schwab

[agent]
runtime = "hermes"       # hermes | claude-code | codex | none
skills_installed = true

[updates]
channel = "stable"
check_on_start = true
manifest_url = "https://meridian.matthewmerino.com/desk/manifest.json"

[reports]
endpoint = "https://meridian.matthewmerino.com/api/desk/report"
```

### Ports

This edition is headless: the API listens on `server.api_port` (8001) and
there is no web bundle. `server.web_port` is unused and may be left alone.

### Binding

`server.bind` defaults to `127.0.0.1`. Do not set `0.0.0.0` unless you put a
TLS reverse proxy in front of it; the login cookie is not marked `Secure` in
local mode, and the API is designed for one machine.

## .env

| Key | Used for |
|---|---|
| `MERIDIAN_API_TOKEN` | bearer token for the CLI and agents; generated at setup |
| `MERIDIAN_COOKIE_SECURE` | `0` for loopback HTTP; set `1` behind HTTPS |
| `OLLAMA_API_KEY` | model access (Ollama Cloud or a local Ollama) |
| `OLLAMA_BASE_URL` | `https://ollama.com/v1` or `http://localhost:11434/v1` |
| `MASSIVE_API_KEY` | live quotes and price history |
| `POLYGON_API_KEY` | ticker details and news |
| `FRED_API_KEY` | macro series |
| `PERPLEXITY_API_KEY` | optional research fallback |
| `SCHWAB_APP_KEY` / `SCHWAB_APP_SECRET` | optional Schwab sync |

Keys are read from the environment, then `~/.meridian/.env`, then the project
`.env`. Never commit `.env`.

## Models

`meridian setup` offers three choices:

- **ollama-cloud** — paste an API key. The app uses `deepseek-v4-flash` for
  chat and `deepseek-v4-pro` for research.
- **ollama-local** — point at a local Ollama server. The model names must exist
  on that server.
- **none** — the app, API, and record work; the AI surfaces return an explicit
  "no model configured" error.

Change later:

```bash
meridian config set model.provider ollama-local
meridian config set model.base_url http://localhost:11434/v1
meridian config edit --secrets     # add OLLAMA_API_KEY
meridian restart
```

## Market data

The desk ships with a free feed: **Yahoo Finance via yfinance**. It needs no
key and answers quotes, daily history, ticker search, and basic fundamentals.
Quotes are delayed and history is daily, which is enough for the risk math, the
STBR timing signal.

The feed is polite: batched quote requests, a 30-second quote cache, a
15-minute history cache, a one-second minimum between calls, and a five-minute
backoff if Yahoo rate-limits. A symbol Yahoo cannot answer is marked stale; it
is never given a fabricated price.

If you have a licensed feed, add Massive and it takes precedence:

```bash
meridian config set market_data.provider massive
meridian config edit --secrets     # add MASSIVE_API_KEY
meridian restart
```

Schwab, when configured, is tried first. A failed paid feed falls back to
Yahoo rather than to an empty book.

## Moving the install

The `meridian` command bakes the home path into the shim. To move:

```bash
meridian backup
mv ~/.meridian /new/path
# edit /new/path/.. and reinstall the shim, or run the installer with --home
```

For a clean move, reinstall at the new home and restore the backup.

## Permissions

`doctor` checks these:

- `~/.meridian/meridian.toml` — 0600
- `~/.meridian/.env` — 0600
- `~/.meridian/app/data` — 0700
- `~/.meridian/backups/*.tar.gz` — 0600

If a mode is wrong, `doctor` prints the `chmod` to run.
