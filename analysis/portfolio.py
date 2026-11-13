"""Portfolio-level analysis: allocation, P&L, concentration metrics."""



def compute_allocation(positions_df):
    """Add weight (%) column based on absolute market value."""
    if positions_df.empty:
        return positions_df
    df = positions_df.copy()
    total = df["market_value"].abs().sum()
    df["weight"] = (df["market_value"].abs() / total * 100) if total else 0
    return df


def herfindahl_index(positions_df):
    """Compute Herfindahl-Hirschman Index for position concentration.

    HHI ranges from 0 (perfectly diversified) to 10000 (single holding).
    """
    if positions_df.empty:
        return 0
    total = positions_df["market_value"].abs().sum()
    if total == 0:
        return 0
    weights = positions_df["market_value"].abs() / total * 100
    return float((weights ** 2).sum())
