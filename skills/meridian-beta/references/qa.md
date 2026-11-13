# QA: the architectural pass

Meridian Beta is headless. `meridian qa` is the whole test of the installed
product: it proves the API, the book, the risk math, and the record end to end.

```bash
meridian qa --json
```

Exit code is 1 on a hard failure; `warn` is a state the desk can run in (empty
book, optional feed missing).

| Check | What it proves |
|---|---|
| `api` | `/api/health` answers |
| `auth` | password set and the CLI token accepted |
| `positions` | the book reads; prices are present and `stale` is tracked |
| `summary` | `net_liquidation` and `cash_balance` are numbers |
| `var` | quotes **and** history work end to end (VaR on the top 20 positions) |
| `stress` | whole-book scenarios and a Monte Carlo VaR |
| `liabilities` / `theses` | the ledgers read |
| `decisions` | the append-only record reads |
| `data health` | the feed report answers |

The JSON is:

```json
{"ok": true, "version": "0.4.5", "channel": "beta",
 "checks": [{"name": "var", "status": "ok", "detail": "8 symbols · VaR95 -2.01%", "ms": 412}],
 "failures": 0, "warnings": 1}
```

Run it before answering, after setup, and after every update. If it fails,
`meridian doctor` narrows the cause.

## What counts as a defect

| Kind | Examples |
|---|---|
| `bug` | a route 500s; a command exits non-zero without a message; an update rolls back |
| `missing-flow` | no way to edit a thesis; setup cannot pick a port |
| `architecture` | a response shape disagrees with the skill; auth is bypassable; backup loses data |
| `docs` | a command in the docs does not exist or needs a flag it omits |

## Reporting a finding

```bash
meridian report \
  --problem "Risk endpoint returns var_95 = null for a book with eight priced positions." \
  --solution "Return var_95 when coverage.resolved > 0; use the empty state only when coverage is zero." \
  --kind architecture \
  --context api \
  --send --yes --json
```

The central intake files it as a GitHub issue in this repository. Rules:

- One problem, one recommended solution. No book values, no logs, no paths.
- Include the route and the expected vs actual shape; that is architectural
  evidence, not portfolio data.
- The response carries an `id`; give it to the operator.
- If the send fails, the report is local: `meridian report --send-all`.

## Destructive checks (demo book only)

On a machine seeded with the synthetic demo book, an agent may also verify:

- `meridian setup` twice: the second run keeps the existing book.
- `meridian backup` then `meridian restore <file>`: positions survive.
- `meridian import fixtures/demo-positions.csv` twice: no duplicate rows.
- `meridian update --rollback`: the previous version starts and the book is
  intact.

Never run these against a real book. If a destructive check fails, report it
with `--kind architecture` and the exact sequence.
