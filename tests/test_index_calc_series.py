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
    """기업행위 ΔM: 그날 지수는 그대로, 가격이 안 움직였으면 이후에도 지수가
    그대로 유지된다.

    (예전 버전은 "ΔM>0이면 이후 레벨이 낮아져야 한다"고 반대로 단정하고
    있었다 - 제수(B)만 조정하고 해당 종목의 유동시총 기준은 안 바꾸는
    구현의 버그를 정답인 것처럼 검증하고 있었던 것. 가격이 하나도 안
    움직인 통제된 상황에서 확인해야 이 문제가 정확히 드러난다.)"""
    dates = pd.bdate_range(ic.BASE_DATE, periods=10)
    flat = pd.DataFrame({c: 100.0 for c in CODES}, index=dates)  # 가격 전혀 불변
    w0 = pd.Series(np.repeat(1 / len(CODES), len(CODES)), index=CODES)
    f0 = pd.Series(np.linspace(1e6, 1e4, len(CODES)), index=CODES)
    d_ca = dates[3]
    ticker = CODES[0]

    base = ic.build_index_series(flat, [{"date": dates[0], "weights": w0,
                                         "ff_mcap": f0}])
    with_ca = ic.build_index_series(
        flat, [{"date": dates[0], "weights": w0, "ff_mcap": f0}],
        [{"date": d_ca, "kind": "share_change", "ticker": ticker, "delta_M": 5e5}])

    assert abs(with_ca.loc[d_ca, "level"] - base.loc[d_ca, "level"]) < 1e-9, \
        "기업행위 당일 지수가 점프함 - 제수 조정 시점 오류"
    nxt = dates[4]
    assert abs(with_ca.loc[nxt, "level"] - base.loc[nxt, "level"]) < 1e-6, \
        "가격 변화가 전혀 없는데 ΔM 반영 다음날 지수가 달라짐 - " \
        "제수만 조정하고 유동시총 기준을 안 갱신한 회귀 버그"
    print("[OK] 기업행위 ΔM 흡수 (당일 무점프 · 가격 불변 시 이후에도 왜곡 없음)")


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
