"""Compatibility interface for Classic ATR."""
import pandas as pd

from scanner.indicators.classic import calculate


def wilder_atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int) -> pd.Series:
    return calculate(pd.DataFrame({"high": high, "low": low, "close": close}), "atr", length)["value"]
