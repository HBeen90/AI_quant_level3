import pandas as pd

from analysis.match_krx_screens import score_screen


def test_score_screen_finds_unique_date():
    dates = [pd.Timestamp("2026-05-29"), pd.Timestamp("2026-06-01")]
    prices = pd.DataFrame(
        {"000001": [100.0, 110.0], "000002": [200.0, 190.0]},
        index=dates,
    )
    scores = score_screen(
        pd.Series({"000001": 100.0, "000002": 200.0}), prices, dates)
    assert scores[0]["selection_date"] == dates[0]
    assert scores[0]["exact_matches"] == 2
    assert scores[1]["exact_matches"] == 0


def test_score_screen_allows_krx_integer_rounding():
    date = pd.Timestamp("2026-05-29")
    prices = pd.DataFrame({"000001": [100.4]}, index=[date])
    scores = score_screen(pd.Series({"000001": 100.0}), prices, [date])
    assert scores[0]["exact_matches"] == 1
