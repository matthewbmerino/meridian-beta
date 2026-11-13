# The record: theses, decisions, reviews

The record is append-only. A thesis card can be replaced; a decision cannot be
edited, only closed or superseded.

## Thesis card

```http
PUT /api/theses/NVDA
Content-Type: application/json
```

```json
{
  "thesis": "Accelerated compute is supply-constrained at the top of the stack.",
  "conviction": 4,
  "invalidation_triggers": [
    "Hyperscaler capex guides down two quarters in a row",
    "Gross margin below 60% for two quarters"
  ],
  "target_price": 210.0,
  "compute_exposed": true,
  "theme": "Compute"
}
```

| Field | Type | Notes |
|---|---|---|
| `thesis` | string | the case, in the operator's words |
| `conviction` | 1–5 | 3 is neutral |
| `invalidation_triggers` | list[string] | what would prove it wrong |
| `target_price` | float or null | upside target, not a promise |
| `compute_exposed` | bool | whether the name is in the compute complex |
| `theme` | string or null | canonical theme; see `GET /api/themes/canonical` |

Review:

```http
POST /api/theses/NVDA/review
```

`GET /api/theses/reviews` lists what is due. A review is a timestamp, not a
new thesis.

## Decision

```http
POST /api/decisions
Content-Type: application/json
```

```json
{
  "type": "manual",
  "symbol": "NVDA",
  "action": "hold",
  "rationale": "Thesis intact; no add until the next capex guide.",
  "book": "alpha",
  "direction": "long",
  "size": 0,
  "conviction": 4,
  "horizon": "20d",
  "source": "hermes",
  "actor": "hermes"
}
```

| Field | Allowed |
|---|---|
| `type` | `sizing`, `thesis`, `manual` |
| `source` | `ui`, `executed`, `auto`, `hermes` |
| `actor` | `operator`, `hermes`, `scheduler`, `pipeline`, `system` |
| `horizon` | `1d`, `5d`, `20d`, `60d` |
| `status` | `open`, `pending`, `closed`, `void` (set later, not on create) |

`source` is provenance and is never guessed: an unknown value is rejected. If
you are the agent writing the entry, use `source: "hermes"` and
`actor: "hermes"`.

Close or supersede:

```http
PATCH /api/decisions/<id>/status
{"status": "closed", "reason": "target reached", "source": "ui"}
```

```http
POST /api/decisions/<id>/supersede
{"superseded_by": "<new-id>", "reason": "thesis changed"}
```

## Liability

```http
POST /api/liabilities
```

```json
{
  "kind": "installment",
  "name": "Mortgage",
  "balance": 412000.0,
  "apr": 6.25,
  "payment": 2540.0,
  "term_months": 360,
  "originated_at": "2024-06-01",
  "note": ""
}
```

`kind` is `installment` or `revolving`. A revolving row may carry
`credit_limit`; a loan must not. `payment`, `due_day`, `term_months`, and
`originated_at` are optional but make the payoff math real.

## Rules

- One thesis card per symbol. Upsert replaces it.
- A decision is immutable in content; lifecycle changes go through status or
  supersede.
- The record is the authority on what was agreed. If it disagrees with a
  conversation, the record wins.
