# Onboarding

`meridian setup` is the only onboarding path. It asks three questions, writes
the config, sets a password, starts the API, imports the book, and installs the
agent skills. There is no dashboard.

## The three questions

| Question | Values | Default |
|---|---|---|
| Book | demo / csv / empty | demo |
| Model | ollama-cloud / ollama-local / none | none |
| Agent | hermes / claude-code / codex / none | hermes |

`demo` is eight synthetic positions, debt, and two theses. `csv` needs an
absolute path. `empty` starts with no book.

`none` for the model still gives a working API and CLI; only the AI surfaces
stay off. `none` for the agent skips the skill install.

## Non-interactive

```bash
meridian setup --non-interactive \
  --book demo \
  --model none \
  --agent hermes \
  --json
```

The CLI generates a strong password and returns it once. It does not accept a
password on the command line.

If `model=ollama-cloud`, put the key in the environment of the setup process;
never paste it into chat or argv:

```bash
OLLAMA_API_KEY='…' meridian setup --non-interactive --book demo \
  --model ollama-cloud --agent hermes --json
```

## The result

```json
{
  "ok": true,
  "version": "0.4.2",
  "channel": "beta",
  "api": "http://127.0.0.1:8001",
  "book": "demo",
  "positions": 8,
  "password": "amber-atlas-lumen-7319",
  "password_shown_once": true,
  "agent": "hermes",
  "skills": ["/Users/you/.hermes/skills/meridian-beta"],
  "next": ["meridian status", "meridian doctor", "meridian qa", "meridian update --check"]
}
```

Tell the operator the password once: "This is shown once. Change it with
`meridian passwd`."

## After setup

```bash
meridian status
meridian doctor
meridian qa
meridian backup
```

`qa` is the end-to-end pass: API, auth, positions, summary, VaR, stress, the
record, and data health. Any defect goes to `meridian report --kind …`, never
an in-place patch. See `docs/qa.md`.

## What setup writes

- `~/.meridian/meridian.toml` — the answers.
- `~/.meridian/.env` — the API token (and model key, if given).
- `~/.meridian/app/data/.auth_credentials` — a bcrypt hash, never the password.
- `~/.meridian/app/data/.session_secret` — created on first API start.
- The demo stores, under `~/.meridian/app/data`.

Nothing is sent anywhere. The model key is used only when an AI surface is
invoked.
