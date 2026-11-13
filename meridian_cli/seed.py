"""Seed a new desk: import positions, add demo debt, write a first thesis.

All writes go through the running API, so every store is created with the
operator's own session secret and integrity sidecars. Nothing is copied from
another instance.
"""

from __future__ import annotations

from pathlib import Path

from . import home, net, ui

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def demo_csv() -> Path:
    return FIXTURES / "demo-positions.csv"


def import_csv(cfg: dict, path: Path) -> dict:
    path = Path(path).expanduser()
    if not path.exists():
        raise FileNotFoundError(str(path))
    result = net.upload_positions(cfg, path)
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "import failed")
    return result


DEMO_LIABILITIES = [
    {
        "kind": "installment", "name": "Mortgage", "balance": 412000.0,
        "apr": 6.25, "payment": 2540.0, "term_months": 360,
        "originated_at": "2024-06-01", "note": "Demo liability — not a real loan.",
    },
    {
        "kind": "installment", "name": "Auto loan", "balance": 38600.0,
        "apr": 1.99, "payment": 720.0, "term_months": 60,
        "originated_at": "2025-03-01", "note": "Demo liability — 1.99% promo APR.",
    },
    {
        "kind": "installment", "name": "Student loan", "balance": 90000.0,
        "apr": 5.50, "payment": 620.0, "term_months": 120,
        "originated_at": "2019-09-01", "note": "Demo liability.",
    },
    {
        "kind": "revolving", "name": "Sapphire Reserve", "balance": 8400.0,
        "apr": 22.99, "payment": 250.0, "credit_limit": 30000.0,
        "note": "Demo liability.",
    },
    {
        "kind": "revolving", "name": "Amex Gold", "balance": 3600.0,
        "apr": 0.0, "payment": 200.0, "credit_limit": 20000.0,
        "note": "Demo liability — paid in full monthly.",
    },
]

DEMO_THESES = [
    (
        "NVDA",
        {
            "thesis": "Accelerated compute is still supply-constrained at the top of the stack. "
                      "The position is a bet on data-centre capex, not on consumer GPUs.",
            "conviction": 4,
            "invalidation_triggers": [
                "Hyperscaler capex guides down two quarters in a row",
                "Gross margin falls below 60% for two consecutive quarters",
            ],
            "target_price": 210.0,
            "compute_exposed": True,
            "theme": "Compute",
        },
    ),
    (
        "MSFT",
        {
            "thesis": "Azure growth plus Copilot seat expansion keeps the multiple supported "
                      "while the rest of the book pays for the drawdown risk.",
            "conviction": 3,
            "invalidation_triggers": ["Azure constant-currency growth below 20%"],
            "target_price": 520.0,
            "theme": "Software",
        },
    ),
]


def seed_demo(cfg: dict) -> dict:
    summary: dict = {"positions": 0, "liabilities": 0, "theses": 0, "decisions": 0}

    result = import_csv(cfg, demo_csv())
    summary["positions"] = int(result.get("count") or 0)
    ui.ok(f"Imported {summary['positions']} demo positions")

    for liability in DEMO_LIABILITIES:
        try:
            net.post(cfg, "/api/liabilities", liability)
            summary["liabilities"] += 1
        except Exception as exc:
            ui.warn(f"Could not add demo liability {liability['name']}: {exc}")
    if summary["liabilities"]:
        ui.ok(f"Added {summary['liabilities']} demo liabilities")

    for symbol, payload in DEMO_THESES:
        try:
            net.put(cfg, f"/api/theses/{symbol}", payload)
            summary["theses"] += 1
        except Exception as exc:
            ui.warn(f"Could not write demo thesis {symbol}: {exc}")
    if summary["theses"]:
        ui.ok(f"Wrote {summary['theses']} demo theses with review dates")

    try:
        net.post(cfg, "/api/decisions", {
            "type": "manual",
            "symbol": "NVDA",
            "action": "hold",
            "rationale": "Demo entry: thesis intact; no add until the next capex guide.",
            "conviction": 4,
            "horizon": "20d",
            "source": "ui",
            "actor": "operator",
        })
        summary["decisions"] += 1
        ui.ok("Wrote one demo decision to the record")
    except Exception as exc:
        ui.warn(f"Could not write demo decision: {exc}")

    return summary


def seed_from_csv(cfg: dict, path: Path) -> dict:
    result = import_csv(cfg, path)
    ui.ok(f"Imported {result.get('count', 0)} positions from {Path(path).name}")
    return {"positions": int(result.get("count") or 0)}
