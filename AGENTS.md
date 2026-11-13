# Meridian Desk — agent brief

This repository is the free, self-hosted edition of Meridian: a private
portfolio desk for one operator and their agent. It is an early beta and it is
**headless** — API, CLI, and skills, no web dashboard.

## Architecture

```
data/          market feeds, macro feeds, persistence, caches
analysis/      pure computation engine
backend/       FastAPI server
agent/         AI research layer
meridian_cli/  the `meridian` command
skills/        agent skills shipped to Hermes / Claude Code / Codex
```

Data flows `data/ → analysis/ → backend/ → meridian_cli/`. Nothing in `data/`
imports upstack.

## Quality bar

- **Correctness over speed.** A wrong number displayed confidently is worse
  than no number. If you cannot verify it, mark it unverified.
- **Degrade gracefully.** Stale feeds grey dependent metrics; insufficient data
  says so explicitly. Never fabricate or silently interpolate.
- **Actionable density over decoration.** Every response should change a
  decision or answer a question.
- **Provenance.** When you state a figure, include the as-of and the route.

## QA and reporting (every agent)

Any agent that controls or tests an installed desk is responsible for finding
what is broken and reporting it. Silence is the failure mode.

1. Run `meridian qa --json` before answering, after setup, and after an update.
   It checks the API, auth, positions, summary, VaR, stress, liabilities,
   theses, decisions, and data health.
2. Exercise the CLI and API directly: setup twice, import the demo fixture,
   backup/restore on the demo book, and a report dry-run.
3. File every defect or gap with `meridian report --kind bug|missing-flow|architecture|docs`,
   one problem, one recommended solution, `--send --yes --json`. The intake
   files it as a GitHub issue in this repository.
4. Never patch the installed app in place. Fixes go through this repository and
   a release.
5. A report that contains book data, positions, logs, paths, hostnames, emails,
   or keys is deleted, not triaged.

## Build and release

```bash
make release   # tarball + manifest in dist/
python3 -m pytest tests -q
```

`dist/` is a build artifact and is ignored. The release script reads `VERSION`
and `CHANNEL`; every early build is `beta`.

## Never

- Commit a store, `.env`, token, or a real book.
- Ship a build that fabricates a price or a return.
- Add an order path. Meridian informs; the operator decides.
- Add a dashboard to this repository. The hosted product owns that surface.
