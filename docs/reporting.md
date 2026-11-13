# Reporting a problem

Every install has one front door for fixes:

```bash
meridian report
```

It asks two questions — what went wrong, and what you would change — plus a
categorical `kind`, then shows exactly what will be sent and waits. Nothing
leaves the machine without `--send`.

## What is sent

| Field | Example |
|---|---|
| `problem` | "Setup exits with 'Port 8001 is in use' when another dev server is running." |
| `solution` | "Detect the listening process and offer to pick the next free port." |
| `kind` | `bug`, `missing-flow`, `architecture`, `docs`, `other` |
| `context` | `setup`, `start`, `update`, `import`, `ui`, `api`, `docs`, `other` |
| `version` | `0.4.4` |
| `channel` | `beta` |
| `os` / `arch` | `linux` / `x86_64` |
| `python` / `node` | `3.12.16` / `22.11.0` |
| `agent` | `hermes` |

That is the whole payload.

## What is never sent

The book, positions, cash, debt, theses, decisions, imports, snapshots, logs,
file paths, hostname, username, email address, IP address, API tokens, `.env`,
model keys, market-data keys, and broker credentials.

The CLI scrubs home paths, emails, bearer tokens, key-like strings, long hex
strings, and IP addresses before saving. The server redacts again, stores no
IP address, and uses the IP only for an in-memory rate limit.

## For agents

```bash
meridian report \
  --problem "Risk endpoint returns var_95 = null for a book with eight priced positions." \
  --solution "Return var_95 when coverage.resolved > 0; use the empty state only when coverage is zero." \
  --kind architecture \
  --context api \
  --send --yes --json
```

`kind` is the class of defect; `context` is where it happened. A route or
schema problem uses `--kind architecture`; a missing command uses
`--kind missing-flow`.

Rules:

- Two free-text fields plus a kind. No book data, no logs, no paths.
- Report a **product** defect or gap. Never a user's portfolio or a specific
  account.
- If `--send` fails, the report is saved; retry with
  `meridian report --send-all`.
- The response includes an `id`; give it to the operator.

`meridian report --dry-run` prints the exact payload and sends nothing.

## The loop

1. An agent runs `meridian qa --json` and exercises the CLI/API.
2. It hits a problem, or finds a gap that QA cannot see.
3. `meridian report` writes it down, and sends it if asked.
4. The central intake files a GitHub issue in `matthewbmerino/meridian-beta`
   with the problem, the recommended solution, the kind, and the environment.
5. The maintainer reproduces it, decides, and fixes it in the repo.
6. A release ships; `meridian update` delivers it.

GitHub is the build queue. There is no separate reports dashboard.

## The local queue

Reports are saved to `~/.meridian/reports/<id>.json` (mode 0600) before they
are sent, so an offline machine does not lose one.

```bash
meridian report --list
meridian report --show <id>
meridian report --send-all
meridian report --id <id> --send
```

## Why two fields

A problem and a recommended solution is enough to reproduce and decide. It is
also small enough to keep private: there is no field for a portfolio, a log, or
a screenshot, so none can be pasted by accident.
