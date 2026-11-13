# Meridian Beta

A private, self-hosted portfolio desk for one operator and their agent.

> **Early beta.** This is the first build that works end to end, not a finished
> product. Every agent that operates the desk is expected to run `meridian qa`
> and file what is broken with `meridian report`.

Meridian Beta keeps the book, computes risk from the whole balance sheet, and
holds an append-only record of why each position is held. The agent reads the
same API the CLI reads, so a number cannot differ between them. It runs on your
machine, with your keys, and sends nothing anywhere except the model calls you
configure.

**This edition is headless.** There is no web dashboard and no Node.js
requirement. The operator's agent is the interface: `meridian` on the CLI and
the documented API underneath it. The dashboard belongs to the hosted product,
not this repository.

## Install

```bash
curl -fsSL https://meridian.matthewmerino.com/install.sh | bash
```

That creates `~/.meridian`, a private virtualenv, and a `meridian` command in
`~/.local/bin`. Then:

```bash
meridian setup
```

Three questions — starting book, model, agent runtime — and the desk is
running the API and CLI.

Requirements: macOS or Linux, Python 3.11+. When the system Python is 3.13/3.14
the installer provisions a managed 3.12. No Node, no dashboard.

Prices work on a fresh install with no keys: the desk uses Yahoo Finance for
delayed quotes and daily history. Add `MASSIVE_API_KEY` only if you have a
licensed feed.

## The agent loop

```bash
meridian qa --json          # API, auth, book, VaR, stress, record, data health
meridian report \
  --problem "What went wrong." \
  --solution "What you would change." \
  --kind architecture \
  --context api \
  --send --yes --json
```

`qa` is the architectural pass. It proves quotes and history end to end by
running VaR and stress on the book. There is no visual pass in this edition
because there is no dashboard.

Reports carry two free-text fields, a kind, and an environment fingerprint —
never the book, positions, logs, paths, or keys. The intake files each report as
a GitHub issue in this repository, which is the build queue; fixes ship through
pull requests and releases.

## What you get

- **The book.** Positions, cash, external balances, and debt in one place.
- **Risk.** Stress scenarios and VaR computed from the whole book, not one
  account at a time.
- **The record.** A thesis card per holding with invalidation triggers and a
  review date, and an append-only decision journal.
- **The agent.** Skills, a documented API, and a CLI an agent can drive.
- **The API.** Everything above is a route; the CLI is a thin wrapper over it.

## Repository layout

```
backend/        FastAPI server
analysis/       pure computation engine
agent/          AI research layer
data/           market feeds and persistence (code only; no stores)
meridian_cli/   the `meridian` command
skills/         Hermes / Claude Code / Codex skills
docs/           install, update, onboarding, CLI, QA, reporting
scripts/        release builder
fixtures/       synthetic demo book
```

## Build a release

```bash
make release   # dist/meridian-$(cat VERSION).tar.gz + manifest.json
```

The release tarball is the installer payload. There is no web bundle.

## Privacy

Reports are opt-in. The CLI scrubs home paths, emails, bearer tokens, key-like
strings, long hex strings, and IP addresses before saving; the server redacts
again, stores no IP address, and keeps reports in a 0600 file. A report that
contains user data is deleted, not triaged.

## License

No license is granted yet. Copyright © 2026 Matthew Merino. All rights reserved.
