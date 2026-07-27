# -*- coding: utf-8 -*-
"""index_calc.build_index_series 동치성 - 접합 검증 노트 3-2 이행 확인.

기존 대조 B는 '저수준 조합을 손으로 쓰면 동치'임을 보였다. 그 조합을
build_index_series() 로 함수화했으므로, 함수 경로도 소연 파트
simulate_index 와 동치인지 다시 못 박는다(회귀 방지).
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from backtest.backtest import make_event, simulate_index  # noqa: E402
from src import index_calc as ic  # noqa: E402

TOL = 1e-9
CODES = ["005930", "000660", "042700", "089030", "003160", "348210", "112290"]


def _prices(n: int, seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(ic.BASE_DATE, periods=n)
    return pd.DataFrame(
        100000 * np.exp(np.cumsum(rng.normal(3e-4, 0.02, (n, len(CODES))), axis=0)),
        index=dates, columns=CODES)


def test_regular_only_matches_build_daily_series():
    """정기변경만 있을 때 신·구 함수가 같은 시계열을 준다(하위호환)."""
    px = _prices(260)
    w1 = pd.Series([.2157, .1843, .18, .18, .1281, .1058, .0061], index=CODES)
    w2 = pd.Series([.2157, .1843, .18, .18, .18, .06],
                   index=[c for c in CODES if c != "089030"])
    f1 = pd.Series([12405393.9, 10598120.1, 90214.2, 13614.9, 3183.1,
                    2629.4, 1125.4], index=CODES)
    f2 = f1.drop("089030") * 1.03
    recon = [{"date": px.index[0], "weights": w1, "ff_mcap": f1},
             {"date": px.index[126], "weights": w2, "ff_mcap": f2}]
    old = ic.build_daily_series(px, recon)
    new = ic.build_index_series(px, recon)["level"]
    rel = float(((new - old).abs() / old).max())
    assert rel < TOL, f"신·구 경로 불일치 rel={rel:.2e}"
    print(f"[OK] 정기 전용 하위호환 (최대 상대차 {rel:.2e})")


def test_exclusion_matches_soyeon_normalization():
    """수시편출: 제수 조정(함수 경로) == 드리프트 후 정규화 이벤트."""
    px = _prices(130)
    dates = px.index
    w0 = pd.Series([.2157, .1843, .18, .18, .1281, .1058, .0061], index=CODES)
    f0 = pd.Series([12405393.9, 10598120.1, 90214.2, 13614.9, 3183.1,
                    2629.4, 1125.4], index=CODES)
    d_ex, gone = dates[60], "089030"

    lvA = ic.build_index_series(
        px, [{"date": dates[0], "weights": w0, "ff_mcap": f0}],
        [{"date": d_ex, "kind": "exclusion", "tickers": [gone]}])

    r = px.pct_change(fill_method=None)
    w = w0.copy()
    for d in dates[1:dates.get_loc(d_ex) + 1]:
        w = w * (1 + r.loc[d])
        w = w / w.sum()
    w_ex = w.drop(gone)
    w_ex = w_ex / w_ex.sum()
    lvB = simulate_index(px, [make_event(dates[0], "regular", w0),
                              make_event(d_ex, "exclusion", w_ex)],
                         base=ic.BASE_INDEX_LEVEL)["level"]

    rel = float(((lvA["level"] - lvB).abs() / lvB).max())
    assert rel < TOL, f"수시 경로 불일치 rel={rel:.2e}"
    assert lvA.loc[d_ex, "n_members"] == len(CODES) - 1
    assert lvA["event"].loc[d_ex] == "exclusion"
    # 편출 익일 무점프 (제수 흡수의 목적)
    nxt = dates[61]
    assert abs(lvA.loc[nxt, "level"] - lvB[nxt]) / lvB[nxt] < TOL
    print(f"[OK] 수시편출 제수 조정 == 정규화 (최대 상대차 {rel:.2e}, 익일 무점프)")


def test_share_change_absorbs_without_jump():
    """기업행위 ΔM: 그날 지수는 그대로, 이후 주가 변동만 반영."""
    px = _prices(60)
    w0 = pd.Series(np.repeat(1 / len(CODES), len(CODES)), index=CODES)
    f0 = pd.Series(np.linspace(1e6, 1e4, len(CODES)), index=CODES)
    d_ca = px.index[30]
    base = ic.build_index_series(px, [{"date": px.index[0], "weights": w0,
                                       "ff_mcap": f0}])
    with_ca = ic.build_index_series(
        px, [{"date": px.index[0], "weights": w0, "ff_mcap": f0}],
        [{"date": d_ca, "kind": "share_change", "delta_M": 5e5}])
    assert abs(with_ca.loc[d_ca, "level"] - base.loc[d_ca, "level"]) < 1e-9, \
        "기업행위 당일 지수가 점프함 - 제수 조정 시점 오류"
    assert with_ca.loc[px.index[31], "level"] < base.loc[px.index[31], "level"], \
        "ΔM>0 인데 이후 레벨이 안 낮아짐 - 제수 방향 오류"
    print("[OK] 기업행위 ΔM 흡수 (당일 무점프 · 이후 제수 반영)")


def test_fail_closed_on_missing_price():
    """활성 구성종목 가격 결측은 조용히 넘어가지 않는다."""
    px = _prices(40)
    px.iloc[20, 2] = np.nan
    w0 = pd.Series(np.repeat(1 / len(CODES), len(CODES)), index=CODES)
    f0 = pd.Series(np.linspace(1e6, 1e4, len(CODES)), index=CODES)
    try:
        ic.build_index_series(px, [{"date": px.index[0], "weights": w0,
                                    "ff_mcap": f0}])
        raise AssertionError("결측이 통과됨")
    except ValueError as e:
        assert "결측" in str(e)
        print("[OK] 가격 결측 fail-closed")


if __name__ == "__main__":
    test_regular_only_matches_build_daily_series()
    test_exclusion_matches_soyeon_normalization()
    test_share_change_absorbs_without_jump()
    test_fail_closed_on_missing_price()
    print("\n4/4 build_index_series 동치성·안전장치 통과")
