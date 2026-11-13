# Troubleshooting

Start here:

```bash
meridian doctor
meridian status
meridian logs api -n 60
```

## Install

| Symptom | Cause | Fix |
|---|---|---|
| `python3 not found` | no Python on PATH | the installer fetches uv and provisions 3.12; if it cannot, install `uv` or Python 3.12 |
| `Python 3.13/3.14` on Arch/Omarchy | system Python is newer than the pinned stack | the installer provisions 3.12 automatically; if the uv fetch fails, `sudo pacman -S uv` and re-run |
| `checksum mismatch` | truncated download or wrong manifest | delete `~/.meridian/downloads/*`, retry; do not install the file |
| `Meridian is already installed` | `~/.meridian/app` exists | `meridian update`, or `--force` after a backup |
| `~/.local/bin is not on PATH` | shell path | `export PATH="$HOME/.local/bin:$PATH"` in your shell rc |

## Start

| Symptom | Cause | Fix |
|---|---|---|
| `Port 8001 is in use` | another process | `lsof -nP -iTCP:8001 -sTCP:LISTEN`; stop the other process |
| `API did not become healthy` | import error, bad env | `meridian logs api -n 80` |
| `401` / `403` on a read | token or password | `meridian doctor`; re-run setup or `meridian passwd` |

## The book

| Symptom | Cause | Fix |
|---|---|---|
| no positions | nothing imported | `meridian import positions.csv` |
| every price is stale | Yahoo did not answer, or a rate limit is in backoff | wait five minutes; check network; or add `MASSIVE_API_KEY` |
| import says "Could not parse file" | unrecognised CSV | export a Chase or Schwab positions CSV; see `docs/imports.md` |
| cash is missing | cash row not present | add a row with a blank ticker and a `Value` |

## Agent

| Symptom | Cause | Fix |
|---|---|---|
| skills missing | runtime path changed | `meridian agent install hermes` |
| agent cannot reach the API | API stopped | `meridian start` |
| agent prints a token | it read `.env` | stop it; rotate with `meridian passwd` and a new `MERIDIAN_API_TOKEN` |

## Update

| Symptom | Cause | Fix |
|---|---|---|
| `Could not reach the update manifest` | offline, or URL wrong | `meridian update --check --url <url>` |
| `Checksum mismatch` | bad download | retry; if it repeats, report the URL |
| `No module named pip` during update | uv-created venv has no pip | 0.4.0 and later use uv; on an older build run `~/.meridian/venv/bin/python -m ensurepip --upgrade` and retry, or reinstall with `--force` |
| update health check failed | new version cannot start | `meridian update --rollback`; `meridian logs api` |

## Data

| Symptom | Cause | Fix |
|---|---|---|
| `data/ is mode 0755` | permissions | `chmod 700 ~/.meridian/app/data` |
| restore failed | archive damaged | try the previous backup; do not edit the archive |
| forgot the password | — | `meridian passwd` (local machine, no old password needed) |

## Reset

```bash
meridian stop
meridian backup
mv ~/.meridian/app/data ~/.meridian/app/data.old
meridian start          # creates a fresh, empty store
```

The old stores remain on disk until you delete them.

## If none of this helps

Report it. The report is two fields and a minimal environment line:

```bash
meridian report
```

It shows the exact payload before sending and never includes the book,
positions, theses, decisions, logs, paths, or keys. This is the path to a fix in
the next release, not a support ticket. See `docs/reporting.md`.
