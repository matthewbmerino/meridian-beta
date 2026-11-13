# Importing a book

The desk has no broker dependency. A positions CSV is enough to start.

## Supported formats

The importer recognises two broker exports by their contents:

- **Chase** — a positions table with `Ticker`, `Quantity`, `Price`, `Value`,
  `Unit Cost`, `Cost`, and optional P&L columns. A cash row is any row with a
  blank ticker and a value.
- **Schwab** — the standard "Positions" download, including the
  `Qty (Quantity)` / `Mkt Val (Market Value)` variants.

It also recognises a **realized activity** export (lots, proceeds, cost basis)
for the tax view. A file it cannot parse is sent to the document extractor,
which needs a model key.

## The generic template

If your broker exports something else, rename the columns to this shape. Only
`Ticker`, `Quantity`, `Price`, `Value`, `Unit Cost`, and `Cost` are required.

```csv
Ticker,Description,Quantity,Price,Value,Unit Cost,Cost,Today's Value Change,Price Change %,Unrealized G/L Amt.,Asset Class
NVDA,NVIDIA CORP,900,178.20,160380.00,118.00,106200.00,1200.00,0.67,54180.00,Equity
MSFT,MICROSOFT CORP,1700,468.50,796450.00,392.00,666400.00,-3400.00,-0.43,130050.00,Equity
,CASH,,,360000.00,,,,,,Cash
```

The blank-ticker row is cash. `Value` is the market value; `Cost` is the total
cost basis. If `Unit Cost` is absent, the importer derives it from `Cost`.

## Options

A ticker like `AAPL CALL 03/06/26 $275.00` is parsed as an option. Schwab's
`AAPL 03/20/26 275.0 C` form is parsed too.

A source label is inferred from the filename (`chase`, `schwab`, `fidelity`,
`vanguard`, `etrade`, `robinhood`, `merrill`, `interactive`, `webull`). If the
file carries a `Source` column, that value is used instead. The demo uses
`Source=Demo`.

## Import

```bash
meridian import ~/Downloads/positions.csv
```

Each import is listed with its position count and can be hidden or deleted
without touching the others.

## Prices

Imported prices are a snapshot. With `MASSIVE_API_KEY` set, the desk refreshes
them and marks each row `stale` when a quote is missing. Without it, every row
is stale by design: the desk is showing you the file, not the tape.

## Multiple accounts

Import one file per account. Each keeps its own source label, and the book sums
them. Cash is carried with the file that contains the cash row.

## After the first import

- Check `/api/accounts/summary` against the broker's total.
- Add liabilities on the Liabilities page; they are the other half of net
  worth.
- Write one thesis card and one decision so the record starts on day one. It
  cannot be backfilled.
