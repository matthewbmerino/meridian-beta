# CLI reference

Every command supports `--json` where it makes sense, so an agent can drive it.
The human output is for the operator; the JSON is the contract.

## Onboarding

```bash
meridian setup                 # interactive: book, model, agent
meridian setup --plan --json
meridian setup --non-interactive --book demo --model none --agent hermes --json
meridian setup --non-interactive --book csv --csv ~/Downloads/positions.csv --json
```

Flags: `--book demo|csv|empty`, `--csv PATH`, `--model none|ollama-cloud|ollama-local`,
`--ollama-key`, `--ollama-base`, `--agent hermes|claude-code|codex|none`,
`--json`. This edition is headless; there is no dashboard flag.

`--password-stdin` reads one line from stdin; empty means generate. Without it,
non-interactive setup generates a password and returns it once in JSON.

## Running

```bash
meridian start                 # API
meridian stop
meridian restart
meridian status                # human summary
meridian status --json
meridian status --updates      # also check the manifest
meridian open                  # open the API docs
meridian logs api -n 50
```

## Health

```bash
meridian doctor                # friendly checks
meridian doctor --json         # {ok, checks[], failures, warnings}
meridian doctor --updates
meridian qa                    # end-to-end: API, book, VaR, stress, record
meridian qa --json
```

Exit code is 1 when any check fails. A warning is not a failure.

## The book

```bash
meridian import ~/Downloads/positions.csv
meridian api GET /api/accounts/summary
meridian api GET /api/accounts/positions
meridian api POST /api/analysis/stress --body '{}'
```

`meridian api` is the general escape hatch: it adds
`Authorization: Bearer <token>` and prints JSON.

## The record

```bash
meridian api GET /api/theses
meridian api PUT /api/theses/NVDA --body '{"thesis":"…","conviction":4,"invalidation_triggers":["…"]}'
meridian api POST /api/decisions --body '{"type":"manual","symbol":"NVDA","action":"hold","source":"hermes","actor":"hermes"}'
```

See `skills/meridian-beta/references/record.md` for every field.

## Agent skills

```bash
meridian agent status
meridian agent install hermes
meridian agent install claude-code
meridian agent install codex
meridian agent prompt hermes
```

Skills are copied to `~/.hermes/skills/`, `~/.claude/skills/`, or
`~/.codex/skills/`. Codex also gets a managed block in `~/.codex/AGENTS.md`.

## Configuration

```bash
meridian config list
meridian config get model.provider
meridian config set updates.check_on_start false
meridian config edit            # $EDITOR on meridian.toml
meridian config edit --secrets  # $EDITOR on .env
meridian config path
```

## Updates

```bash
meridian update --check
meridian update                # asks, backs up, verifies, swaps, health-checks
meridian update --yes
meridian update --rollback
```

The update is a verified tarball swap. Data, config, and secrets are preserved.
If the new version fails its health check, the CLI restores the previous tree.

## Backup

```bash
meridian backup
meridian backup --json
meridian restore ~/.meridian/backups/meridian-backup-20261113-093000.tar.gz
```

The backup contains the key that decrypts it. Treat the archive as the book
itself.

## Other

```bash
meridian passwd                # change the desk password
meridian passwd --stdin        # read one line (agents)
meridian version
meridian uninstall             # stops, backs up, removes the app; keeps backups
meridian uninstall --purge     # removes ~/.meridian entirely
```

## Reports

```bash
meridian report                 # interactive: problem + recommended solution
meridian report --problem "…" --solution "…" --kind architecture --context api --send --yes --json
meridian report --dry-run
meridian report --list
meridian report --show <id>
meridian report --send-all
```

Two free-text fields plus a `kind` (`bug`, `missing-flow`, `architecture`,
`docs`, `other`) and version/channel/OS/arch/Python/Node/agent.
No book, positions, theses, decisions, logs, paths, or keys.
`docs/reporting.md` is the policy.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | a check failed, or the command could not complete |
| 130 | interrupted (Ctrl-C) |
