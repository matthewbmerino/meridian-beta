---
name: meridian-setup
description: Use when installing or configuring Meridian Beta. Asks the three onboarding questions, runs the setup, and verifies the install.
version: 0.4.4
author: Meridian
---

# Set up Meridian Beta

You are installing a private portfolio desk for one operator on this machine.
The CLI does the work; your job is to ask three questions, run one command, and
prove the desk is healthy. Do not invent answers, passwords, or numbers.

## Preconditions

Run:

```bash
meridian version --json
```

If `meridian` is not found, install it (the operator asked for this):

```bash
curl -fsSL https://meridian.matthewmerino.com/install.sh | bash -s -- --no-setup
```

Then re-check `meridian version --json`. If the install reports that
`~/.local/bin` is not on `PATH`, use the absolute path it prints for every
command below.

## 1. Read the questionnaire from the CLI

```bash
meridian setup --plan --json
```

The output has `questions[]`, each with `id`, `prompt`, `options[]`, and
`default`. Use the exact labels. If the CLI changes the questions, the CLI is
the source of truth — do not hardcode this file's memory of them.

## 2. Ask, one question at a time

Ask in this order, and wait for an answer before asking the next:

1. **book** — demo, a broker CSV, or empty. Recommend `demo` for a first run.
2. **model** — `ollama-cloud`, `ollama-local`, or `none`. Explain that `none`
   still gives a working app and API; the AI features stay off.
3. **agent** — Hermes, Claude Code, Codex, or none. Pick the runtime the
   operator is actually using.

This edition is headless; there is no dashboard question and no Node.js.

If `book=csv`, ask for the absolute path and confirm it exists before running
anything:

```bash
test -f /absolute/path/positions.csv && echo ok
```

If `model=ollama-cloud`, ask for the key but do **not** echo it back and do not
put it in the command line. Put it in the environment for the setup process:

```bash
OLLAMA_API_KEY='…' meridian setup --non-interactive …
```

## 3. Run the setup once

Compose the flags from the answers:

```bash
meridian setup --non-interactive \
  --book demo \
  --model none \
  --agent hermes \
  --json
```

- `book=csv`: add `--csv /absolute/path/positions.csv`.
- `model=ollama-cloud`: prefix with `OLLAMA_API_KEY='…'`.
- `model=ollama-local`: add `--ollama-base http://localhost:11434/v1` if the
  local server is not on the default port.
- Do not pass `--password-stdin`. The CLI generates a strong password and
  returns it in the JSON.

The command may take 60–120 seconds: it creates the password, starts the API,
and seeds the book.

## 4. Read the result

The JSON has:

```json
{
  "ok": true,
  "version": "0.4.4",
  "channel": "beta",
  "api": "http://127.0.0.1:8001",
  "book": "demo",
  "positions": 8,
  "password": "four-words-1234",
  "password_shown_once": true,
  "next": ["meridian status", "meridian doctor", "meridian update --check"]
}
```

If `ok` is false, show the `error` and stop. Do not retry blindly.

Tell the operator the API URL and the password **once**, with:
"This is shown once. Change it with `meridian passwd`."

## 5. Verify — do not assume

```bash
meridian doctor --json
meridian qa --json
meridian status --json
```

- `doctor.ok` must be true. For every check with `status: "fail"`, apply the
  `hint`, re-run, and report what changed. A warning is allowed and should be
  explained in one line.
- `qa.ok` must be true. It proves the API, auth, positions, summary, VaR,
  stress, record, and data health end to end. A `warn` is allowed; a `fail` is
  a report, not a retry loop.
- `status.ok` must be true and `status.api.healthy` must be true.
- `status.book.positions` should match the chosen book (8 for the demo).

Then read the book and summarize it, using only the returned numbers:

```bash
meridian api GET /api/accounts/summary
meridian api GET /api/accounts/positions
```

## 6. Confirm the agent skills

```bash
meridian agent status --json
```

Every skill must show `"installed": true`. If one is missing:

```bash
meridian agent install hermes
```

(Use the runtime the operator picked.)

## 7. Hand over

Finish with three lines, no more:

- The desk is at `<api>`; the password was shown once.
- The record is empty until they write a thesis: `meridian api GET /api/theses`.
- Next: `meridian backup`, then `meridian update --check`; every agent session
  starts with `meridian qa --json`.

## Failure table

| Symptom | Do this |
|---|---|
| `Port 8001 is in use` | `meridian stop`; if it persists, `lsof -nP -iTCP:8001 -sTCP:LISTEN` and report the process. Do not kill an unknown process. |
| `Could not set the password` | Check `~/.meridian/venv/bin/python` exists; reinstall if not. |
| `API did not become healthy` | `meridian logs api -n 40` and report the last lines. |
| A check fails twice after applying its hint | Report it: `meridian report --problem "…" --solution "…" --kind bug --context setup --send --yes --json`. Two free-text fields plus a kind; no book data. |
| `Checksum mismatch` on update | Stop; do not run the tarball. Report the URL and the expected/actual prefixes. |

## Never

- Never invent a portfolio number, a thesis, or a decision.
- Never run `meridian uninstall` without explicit consent.
- Never print the API token from `~/.meridian/.env`.
- Never run setup twice to "fix" something; diagnose with `doctor` first.
