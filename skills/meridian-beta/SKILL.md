---
name: meridian-beta
description: Use when answering questions about the operator's portfolio, risk, debt, theses, decision record, QA, or reporting through Meridian Beta.
version: 0.4.5
author: Meridian
---

# Meridian Beta

A private portfolio desk on this machine. The app, the API, and you read the
same book. Your job is to answer from that book, never from memory, and to
leave a record when the operator makes a decision.

This is an **early beta**. Using the desk and testing the desk are the same
job: run `meridian qa`, exercise the CLI and API, and report what is broken or
missing. This edition is headless — there is no dashboard. Do not patch the
installed app in place; reports are the path to a release.

## When to use

- "What do I own / what is it worth / what did I pay?"
- "What is my risk / concentration / drawdown / exposure?"
- "What do I owe / what does the debt cost?"
- "Why do I own this / what would change my mind?"
- "What did I decide, and when is the review?"
- Anything that needs the book as context for research.

## State probes (run these first)

```bash
meridian status --json
meridian doctor --json
```

If the API is down, `meridian start`. If `doctor` reports a failure, apply its
`hint` before answering portfolio questions.

## How to call the API

Use the CLI wrapper — it injects the bearer token and returns JSON:

```bash
meridian api GET /api/accounts/summary
meridian api GET /api/accounts/positions
meridian api POST /api/analysis/stress --body '{}'
```

Never read or print `~/.meridian/.env`. Never call the API without the wrapper
unless the operator asked you to.

## The reading map

| Question | Call | Notes |
|---|---|---|
| What is the book worth? | `GET /api/accounts/summary` | `net_liquidation`, `cash_balance`, `buying_power` |
| What do I own? | `GET /api/accounts/positions` | one row per position; `stale` means the price is not live |
| What is the risk? | `POST /api/analysis/stress` with `{}` | whole-book scenarios + Monte Carlo; needs price history |
| Portfolio VaR | `POST /api/analysis/var` `{"symbols":[…],"weights":[…],"confidence":0.95}` | weights are market value / total market value, not / NAV |
| What do I owe? | `GET /api/liabilities` | balances, APR, payment, payoff |
| Is the debt cheap? | `GET /api/liabilities/analysis` | weighted APR vs the risk-free rate, avalanche order |
| Why do I own it? | `GET /api/theses` | thesis, conviction, invalidation triggers, next review |
| What did I decide? | `GET /api/decisions?limit=50` | append-only; `status` is open/pending/closed/void |
| Is the data fresh? | `GET /api/meta/data-health` | per-feed status and as-of |
| What is this metric? | `GET /api/meta/methodology/{metric}` | formula and denominator |

## Numbers have denominators

- **Weight** is percent of total market value, unless the payload says `nav`.
- **VaR** is quoted as a percent of the position set passed in; say which set.
- **Net worth** is `net_liquidation − total debt`; it is computed in the
  liabilities router, not in the account summary.
- **STBR** is `price / SMA(140 bars)`; it is a per-name timing signal, not a
  portfolio metric.
- A `stale: true` price is an import price, not a live quote. Say so when it
  matters; never present it as current.

## Writing: the record and the ledgers

Writes are deliberately narrow. There is no order tool and no execution path.

| Write | Call | Rule |
|---|---|---|
| Thesis | `PUT /api/theses/{symbol}` | one card per symbol; include invalidation triggers |
| Thesis review | `POST /api/theses/{symbol}/review` | records that the review happened |
| Decision | `POST /api/decisions` | append-only; include the rationale and the source |
| Manual external P&L | `POST /api/imports/manual-external` | outside accounts |
| Liability | `POST /api/liabilities` | debt only |
| Import | `meridian import file.csv` | positions CSV |

Before writing a decision, confirm the symbol, the action, and the rationale
with the operator. The record is append-only: a wrong entry is superseded, not
edited. If the operator asks you to "fix" an entry, log a new one with
`superseded_by` and a reason.

See `references/record.md` for the exact bodies.

## The morning brief

The brief is a routine, not an endpoint. If the operator wants one:

1. Read `summary`, `positions`, `liabilities/analysis`, `theses/reviews`, and
   `meta/data-health`.
2. Write four short paragraphs: what moved, what the risk is, what is due for
   review, what is stale.
3. Send it on the operator's channel. If you are Hermes, a cron job is the
   right mechanism; the prompt should carry the freshness rule: "never let a
   carried-forward close read as today's."
4. Do not include a recommendation. The desk informs; the operator decides.

## Guardrails

- **No fabrication.** If a number is missing, say "not available" and name the
  missing feed. Never interpolate a price, a return, or a weight.
- **No advice.** Meridian is analytics and record-keeping. Do not tell the
  operator to buy or sell.
- **No orders.** `preview_trade` (when present) sizes an order; nothing sends.
- **One source.** The app and you read the same API. If the operator quotes a
  number that disagrees, pull it again; do not reconcile by guessing.
- **Provenance.** When you state a figure, include the as-of and the route
  (`GET /api/accounts/positions`, as of 14:32 ET).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Connection refused` | `meridian start`, then `meridian status` |
| `401` / `403` | `meridian doctor`; the token or password is wrong |
| `stale` everywhere | no live feed: add `MASSIVE_API_KEY` or accept import prices |
| Risk endpoints empty | no price history yet; they fill as snapshots accumulate |
| `no positions` | `meridian import positions.csv` |

## QA: the architectural pass

`doctor` says whether the install is healthy; `qa` says whether the product
works end to end. Run it before answering, after setup, and after an update.

```bash
meridian qa --json
```

It checks the API, auth, positions, summary, VaR (quotes **and** history),
stress, liabilities, theses, decisions, and data health. Exit 1 means a hard
failure; `warn` is a state the desk can run in. A failed check is reported, not
patched.

## When something is wrong

This is the standard path for improving the product. Do not patch the installed
app in place; report the problem so the fix ships in a release.

1. Run `meridian qa --json` and `meridian doctor --json`; apply the `hint` for
   every failure.
2. If the same failure survives two attempts, or you find a missing flow or an
   architectural gap, report it:

```bash
meridian report \
  --problem "What went wrong, in one or two sentences." \
  --solution "What you would change to fix it." \
  --kind bug \
  --context setup \
  --send --yes --json
```

`--kind` is one of `bug`, `missing-flow`, `architecture`, `docs`, `other`.

3. The report is **two free-text fields plus a kind** and a version, channel,
   OS, arch, Python, Node, and agent runtime. Never include the book, positions,
   theses, decisions, imports, logs, file paths, emails, hostnames, or keys. The
   CLI scrubs, but do not rely on it: write the problem as if it were public.
4. Give the operator the report id from the response.

`meridian report --dry-run` prints the exact payload without sending. Use it
when the problem text is hard to sanitize.

A good report names the failing step and the smallest change that would fix it.
A report that contains user data is deleted, not triaged.

## References

- `references/api.md` — verified routes and bodies.
- `references/record.md` — thesis, decision, and review schemas.
- `references/qa.md` — the architectural checklist.
- `references/operations.md` — every CLI command.
