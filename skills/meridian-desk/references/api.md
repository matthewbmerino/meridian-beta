# Verified API routes

All routes are under `http://127.0.0.1:8001`. Use `meridian api`; it injects
the bearer token from `~/.meridian/.env`.

```bash
meridian api GET /api/accounts/positions
meridian api POST /api/analysis/stress --body '{}'
```

## Reads

| Method | Route | Returns |
|---|---|---|
| GET | `/api/health` | liveness; no auth |
| GET | `/api/accounts/summary` | `net_liquidation`, `cash_balance`, `buying_power`, totals |
| GET | `/api/accounts/positions` | one row per position, with `stale` and `source` |
| GET | `/api/accounts/external` | manual external balances and P&L |
| GET | `/api/liabilities` | each debt: balance, APR, payment, payoff, `is_debt` |
| GET | `/api/liabilities/analysis` | weighted APR vs risk-free, avalanche order, net worth |
| POST | `/api/analysis/stress` | scenarios + Monte Carlo for the whole book |
| POST | `/api/analysis/var` | VaR/CVaR for a symbol/weight set |
| GET | `/api/analysis/composites` | market-state composites (weather, regime, breadth) |
| GET | `/api/analysis/attribution` | contribution by position |
| GET | `/api/meta/data-health` | per-feed status and as-of |
| GET | `/api/meta/methodology/{metric}` | formula and denominator for a metric |
| GET | `/api/theses` | every thesis card |
| GET | `/api/theses/{symbol}` | one thesis card |
| GET | `/api/theses/reviews` | cards due or overdue for review |
| GET | `/api/decisions?limit=50` | the append-only decision record |
| GET | `/api/decisions/timeline?bucket=week` | record density over time |
| GET | `/api/imports/list` | imported position/activity files |
| GET | `/api/snapshots/portfolio` | portfolio snapshots over time |

### Bodies

Whole-book stress (no body is required; it reads the book):

```json
{"symbols": null, "simulations": 5000, "horizon": 5}
```

VaR (weights are market value / total market value):

```json
{"symbols": ["NVDA", "MSFT"], "weights": [0.6, 0.4], "confidence": 0.95}
```

## Writes

| Method | Route | Body |
|---|---|---|
| PUT | `/api/theses/{symbol}` | thesis card (see `record.md`) |
| POST | `/api/theses/{symbol}/review` | `{}` — marks the review as done |
| POST | `/api/decisions` | decision (see `record.md`) |
| PATCH | `/api/decisions/{id}/status` | `{"status": "closed", "reason": "…", "source": "ui"}` |
| POST | `/api/decisions/{id}/supersede` | `{"superseded_by": "<id>", "reason": "…"}` |
| POST | `/api/liabilities` | liability (see `record.md`) |
| PATCH | `/api/liabilities/{id}` | same shape as create |
| DELETE | `/api/liabilities/{id}` | — |
| POST | `/api/imports/upload` | multipart `file` (CSV/XLSX/PDF) |
| POST | `/api/imports/manual-external` | manual external P&L entry |

## Freshness

`GET /api/accounts/positions` marks each row `stale: true` when the live feed
did not price it. `GET /api/meta/data-health` is the feed-level view. A stale
price is an import price; do not call it live.
