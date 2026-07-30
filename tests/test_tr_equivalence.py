# -*- coding: utf-8 -*-
"""TR(총수익) 지수 - 두 독립 경로의 동치성 검증.

리뷰 지적: "배당 처리 미반영, 제수 보정으로 TR 경로를 넣어라."
실제 상태: 수익률 기반 TR 경로(simulate_index mode='gross_tr')는 이미 있었다.
          제수 기반 경로가 없었고, 두 경로가 같은 답을 내는지도 미확인이었다.

이 파일이 하는 일:
  A. 제수 기반 TR (index_calc.adjust_divisor_for_dividend) ==
     수익률 기반 TR (backtest.simulate_index mode='gross_tr')
  B. PR 계열은 배당락 하락을 그대로 반영한다 - 이건 오류가 아니라 정의다.
  C. TR - PR = 배당수익률 (부호·크기 검증)
  D. 이중반영 방지 가드가 실제로 작동하는가

정기·수시 접합에서 했던 것과 같은 방식으로, TR도 '설계했다'가 아니라
'두 구현이 일치함을 실증했다'로 만든다.
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
CODES = ["005930", "000660", "042700", "089030", "003160"]


def _fixture(n: int = 250, seed: int = 17):
    """가격 패널 + 배당락일 2회(연 1회 결산배당 가정)."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(ic.BASE_DATE, periods=n)
    px = pd.DataFrame(
        100000 * np.exp(np.cumsum(rng.normal(2e-4, 0.015, (n, len(CODES))),
                                  axis=0)),
        index=dates, columns=CODES)
    w0 = pd.Series([.30, .25, .20, .15, .10], index=CODES)
    f0 = pd.Series([12405393.9, 10598120.1, 90214.2, 13614.9, 3183.1],
                   index=CODES)

    div = pd.DataFrame(0.0, index=dates, columns=CODES)
    ex_dates = [dates[80], dates[200]]
    for d in ex_dates:
        # 배당락일에 주당 배당금만큼 가격이 추가로 빠진다(락 효과 모사)
        dps = (px.loc[d] * pd.Series([0.018, 0.015, 0.004, 0.0, 0.010],
                                     index=CODES)).round(0)
        div.loc[d] = dps.values
        px.loc[d:, :] = px.loc[d:, :] - dps.values      # 락 이후 전 구간 하향
    return px, w0, f0, div, ex_dates


def test_divisor_tr_equals_returns_tr():
    """대조 A: 제수 기반 TR == 수익률 기반 TR."""
    px, w0, f0, div, ex_dates = _fixture()
    recon = [{"date": px.index[0], "weights": w0, "ff_mcap": f0}]
    adhoc = [{"date": d, "kind": "dividend", "dps": div.loc[d]}
             for d in ex_dates]

    tr_divisor = ic.build_index_series(px, recon, adhoc)["level"]
    tr_returns = simulate_index(px, [make_event(px.index[0], "regular", w0)],
                                base=ic.BASE_INDEX_LEVEL, mode="gross_tr",
                                ordinary_dividends=div)["level"]

    common = tr_divisor.index.intersection(tr_returns.index)
    rel = float(((tr_divisor.loc[common] - tr_returns.loc[common]).abs()
                 / tr_returns.loc[common]).max())
    assert rel < TOL, f"TR 두 경로 불일치 rel={rel:.2e}"
    print(f"[OK] 대조 A: 제수 기반 TR == 수익률 기반 TR (최대 상대차 {rel:.2e})")


def test_pr_reflects_ex_dividend_drop_by_design():
    """대조 B: PR은 배당락 하락을 반영한다 - 정의대로 동작하는지 확인.

    리뷰는 이를 '착시·잘못 기록'이라 했으나, 가격지수의 정의가 그렇다.
    고칠 대상이 아니라 TR을 별도 계열로 병기할 대상이다.
    """
    px, w0, f0, div, ex_dates = _fixture()
    pr = ic.build_index_series(px, [{"date": px.index[0], "weights": w0,
                                     "ff_mcap": f0}])["level"]
    d = ex_dates[0]
    prev = px.index[px.index.get_loc(d) - 1]
    assert pr[d] < pr[prev], "PR이 배당락 하락을 반영하지 않음"

    tr = ic.build_index_series(
        px, [{"date": px.index[0], "weights": w0, "ff_mcap": f0}],
        [{"date": d, "kind": "dividend", "dps": div.loc[d]}])["level"]
    assert tr[d] > pr[d], "TR이 배당락을 상쇄하지 못함"
    gap = tr[d] / pr[d] - 1
    print(f"[OK] 대조 B: 배당락일 PR {pr[d]:.2f} < TR {tr[d]:.2f} "
          f"(상쇄폭 {gap:.4%}) - PR 하락은 정의상 정상")


def test_tr_minus_pr_equals_dividend_yield():
    """대조 C: 누적 TR-PR 격차가 실제 배당수익률과 일치하는가."""
    px, w0, f0, div, ex_dates = _fixture()
    recon = [{"date": px.index[0], "weights": w0, "ff_mcap": f0}]
    pr = ic.build_index_series(px, recon)["level"]
    tr = ic.build_index_series(
        px, recon, [{"date": d, "kind": "dividend", "dps": div.loc[d]}
                    for d in ex_dates])["level"]

    # 이론값: TR/PR = 제수비 = 각 배당락일 (M_ex + D) / M_ex 의 곱
    # (엔진과 독립적으로, IIF 정의부터 손으로 다시 계산한다)
    iif = ic.calc_iif(w0, f0)
    p0 = px.loc[px.index[0]]
    ratio = 1.0
    for d in ex_dates:
        M_ex = float((iif * f0 * px.loc[d] / p0).sum())
        D = float((iif * f0 * div.loc[d] / p0).sum())
        ratio *= (M_ex + D) / M_ex
    actual = float(tr.iloc[-1] / pr.iloc[-1])
    assert abs(actual - ratio) / ratio < 1e-9, \
        f"TR-PR 격차 {actual:.9f} != 이론 배당수익 {ratio:.9f}"
    print(f"[OK] 대조 C: 누적 TR/PR = {actual:.9f} "
          f"(독립 손계산 {ratio:.9f}) - 배당락 2회 누적 "
          f"{(actual - 1) * 100:.3f}%")


def test_double_count_guards():
    """대조 D: 이중반영 방지 가드가 실제로 막는가."""
    px, w0, f0, div, _ = _fixture()
    ev = [make_event(px.index[0], "regular", w0)]
    try:                                      # PR 모드에 배당 주입
        simulate_index(px, ev, mode="pr", ordinary_dividends=div)
        raise AssertionError("PR 모드에서 배당이 통과됨")
    except ValueError:
        pass
    try:                                      # TR 모드인데 배당 없음
        simulate_index(px, ev, mode="gross_tr")
        raise AssertionError("배당 없는 gross_tr 이 통과됨")
    except ValueError:
        pass
    try:                                      # 음수 배당(자본환급 오입력)
        ic.adjust_divisor_for_dividend(1000.0, 5000.0, -10.0)
        raise AssertionError("음수 배당이 통과됨")
    except ValueError:
        pass
    bad = div.copy()
    bad.iloc[10, 0] = -1.0
    try:
        simulate_index(px, ev, mode="gross_tr", ordinary_dividends=bad)
        raise AssertionError("수익률 경로에서 음수 배당이 통과됨")
    except ValueError:
        pass
    first_day = pd.DataFrame(0.0, index=px.index, columns=px.columns)
    first_day.iloc[0, 0] = 100.0
    try:
        simulate_index(px, ev, mode="gross_tr", ordinary_dividends=first_day)
        raise AssertionError("직전 가격 없는 배당이 조용히 소실됨")
    except ValueError:
        pass
    print("[OK] 대조 D: PR<->TR 이중반영·배당 입력 가드 5종 작동")


if __name__ == "__main__":
    test_divisor_tr_equals_returns_tr()
    test_pr_reflects_ex_dividend_drop_by_design()
    test_tr_minus_pr_equals_dividend_yield()
    test_double_count_guards()
    print("\n4/4 TR 경로 동치성·정의 검증 통과")
