from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from analysis.krx_facts_from_screen import read_screen, selection_adv60


def test_selection_adv60_includes_selection_day_and_zero_fills_halt():
    dates = pd.bdate_range("2026-01-01", periods=61)
    values = pd.DataFrame({"000001": 1.0, "000002": 2.0}, index=dates)
    values.loc[dates[-2], "000001"] = np.nan
    values.loc[dates[-1], "000001"] = 61.0
    listing = pd.Series(pd.Timestamp("2020-01-01"),
                        index=["000001", "000002"])
    adv = selection_adv60(values, dates[-1], listing)
    assert adv["000001"] == pytest.approx((58 + 0 + 61) / 60)
    assert adv["000002"] == pytest.approx(2.0)


def test_selection_adv60_requires_sixty_market_days():
    dates = pd.bdate_range("2026-01-01", periods=59)
    values = pd.DataFrame({"000001": 1.0}, index=dates)
    listing = pd.Series(pd.Timestamp("2020-01-01"), index=["000001"])
    with pytest.raises(SystemExit, match="60 required"):
        selection_adv60(values, dates[-1], listing)


def test_read_screen_recomputes_full_market_rank(tmp_path):
    path = tmp_path / "krx.csv"
    pd.DataFrame({
        "종목코드": ["000001", "000002"],
        "종목명": ["A", "B"],
        "종가": [1000, 2000],
        "시가총액": [10000, 20000],
    }).to_csv(path, index=False, encoding="cp949")
    result = read_screen(str(path))
    assert result.loc["000002", "mcap_rank"] == 1
    assert result.loc["000001", "mcap_rank"] == 2
