"""Compatibility interface for Classic ADX."""
import pandas as pd

from scanner.indicators.classic import calculate


def adx(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    return calculate(pd.DataFrame({"high": high, "low": low, "close": close}), "adx", length)["value"]
