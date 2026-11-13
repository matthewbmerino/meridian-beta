# CLI

The short version. `skills/meridian-desk/references/operations.md` has every
flag.

## Setup and running

```bash
meridian setup
meridian setup --plan --json
meridian start
meridian stop
meridian restart
meridian status
meridian status --json
meridian doctor
meridian doctor --json
meridian qa
meridian qa --json
meridian open
meridian logs api -f
```

## The book

```bash
meridian import ~/Downloads/positions.csv
meridian api GET /api/accounts/summary
meridian api GET /api/accounts/positions
meridian api POST /api/analysis/stress --body '{}'
```

`meridian api` is the general HTTP wrapper: it adds the bearer token and prints
JSON. The agent uses it for every read and write.

## The record

```bash
meridian api GET /api/theses
meridian api GET /api/theses/reviews
meridian api PUT /api/theses/NVDA --body '{"thesis":"…","conviction":4}'
meridian api POST /api/decisions --body '{"type":"manual","symbol":"NVDA","action":"hold","source":"hermes","actor":"hermes"}'
```

## Agent

```bash
meridian agent status
meridian agent install hermes
meridian agent install claude-code
meridian agent install codex
meridian agent prompt hermes
```

## Config

```bash
meridian config list
meridian config get model.provider
meridian config set model.provider ollama-local
meridian config edit
meridian config edit --secrets
meridian config path
```

## Updates and backups

```bash
meridian update --check
meridian update
meridian update --yes
meridian update --rollback
meridian backup
meridian restore ~/.meridian/backups/meridian-backup-20261113-093000.tar.gz
```

## Reports

```bash
meridian report                 # interactive: problem + recommended solution
meridian report --problem "…" --solution "…" --kind architecture --context api --send --yes --json
meridian report --dry-run       # print the payload, send nothing
meridian report --list
meridian report --show <id>
meridian report --send-all
```

The report is two fields plus version, OS, arch, Python, Node, and agent. It
never includes the book, positions, theses, decisions, logs, paths, or keys.
See `docs/reporting.md`.

## Account

```bash
meridian passwd
meridian passwd --stdin
meridian version
meridian uninstall
meridian uninstall --purge
```

## JSON contract

Every command that an agent needs returns `{ "ok": true, ... }` on success and
`{ "ok": false, "error": "…" }` on failure. Human-facing text goes to stdout;
errors go to stderr. Exit code 1 means a check failed or the command could not
complete.
