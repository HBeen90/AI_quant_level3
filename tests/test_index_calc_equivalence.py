# -*- coding: utf-8 -*-
"""파트 3(소연) x 파트 인서 - 지수 산출 경로 동치성 공동 대조 테스트.

증명하는 것:
  A. 정기변경(가변 종목 수 포함): 인서님 IIF+제수 경로(build_daily_series)와
     소연 비중 drift 경로(simulate_index)가 동일 시계열을 산출한다.
  B. 수시편출(무대체): 인서님 3.4 제수 조정(adjust_base_market_cap,
     델타M = -편출 종목 기여분)과 소연의 '드리프트 후 정규화' 이벤트가
     수학적으로 동치다 - 방법론의 "정규화 = 제수 흡수" 조문 실증.

허용오차 1e-9 (실측 1e-15 수준). 인서님 코드는 수정 없이 그대로 사용.
부속: to_reconstitution_events() - 소연 이벤트 -> 인서님 입력 어댑터.
검증 범위는 정기변경과 무대체 수시편출이다. cap·emergency_fill·주식교부
합병은 이 테스트가 증명하지 않으며 별도 공동 대조 대상으로 남긴다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
from src import index_calc as ic
from backtest.backtest import make_event, simulate_index

TOL = 1e-9
CODES = ["005930", "000660", "042700", "089030", "003160", "348210", "112290"]


def to_reconstitution_events(events: list, fmc_by_date: dict) -> list:
    """소연 이벤트({effective_date, reason, target_weights}) 중 '정기변경'을
    인서님 build_daily_series 입력({date, weights, ff_mcap})으로 변환한다.

    fmc_by_date: {시행일: 유동시총 Series} - 인계 CSV(코드·ff_market_cap)에서
    구성. 무대체 exclusion은 3.4 제수 조정 경로로 처리하며 본 테스트 B가
    동치성을 검증한다. cap·emergency_fill은 이 어댑터와 검증 범위에서 제외한다.
    """
    out = []
    for e in events:
        if e["reason"] != "regular":
            continue
        d = e["effective_date"]
        if d not in fmc_by_date:
            raise ValueError(f"정기변경 {d.date()} 의 유동시총 누락 - "
                             "인계 CSV(ff_market_cap)로 fmc_by_date 구성 필요")
        w = e["target_weights"]
        fmc = pd.to_numeric(fmc_by_date[d].reindex(w.index), errors="coerce")
        bad = fmc.isna() | ~np.isfinite(fmc) | (fmc <= 0)
        if bad.any():
            tickers = fmc.index[bad].astype(str).tolist()
            raise ValueError(f"정기변경 {d.date()} 의 종목별 유동시총 누락·비양수: "
                             f"{tickers}")
        out.append({"date": d, "weights": w, "ff_mcap": fmc})
    return out


def _prices(n_days: int, seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(ic.BASE_DATE, periods=n_days)
    return pd.DataFrame(
        100000 * np.exp(np.cumsum(rng.normal(3e-4, 0.02, (n_days, len(CODES))),
                                  axis=0)),
        index=dates, columns=CODES)


def test_regular_path_equivalence():
    """대조 A: 정기 2회(7종목 -> 6종목 축소·캡 반영 비중)."""
    px = _prices(260)
    dates = px.index
    w1 = pd.Series([.2157, .1843, .18, .18, .1281, .1058, .0061], index=CODES)
    w2 = pd.Series([.2157, .1843, .18, .18, .18, .06],
                   index=[c for c in CODES if c != "089030"])
    f1 = pd.Series([12405393.9, 10598120.1, 90214.2, 13614.9,
                    3183.1, 2629.4, 1125.4], index=CODES)
    f2 = f1.drop("089030") * 1.03
    d2 = dates[126]

    events = [make_event(dates[0], "regular", w1),
              make_event(d2, "regular", w2)]
    lv_mine = simulate_index(px, events, base=ic.BASE_INDEX_LEVEL)["level"]
    recon = to_reconstitution_events(events, {dates[0]: f1, d2: f2})
    lv_insu = ic.build_daily_series(px, recon)

    common = lv_insu.index.intersection(lv_mine.index)
    rel = float(((lv_insu.loc[common] - lv_mine.loc[common]).abs()
                 / lv_mine.loc[common]).max())
    assert len(common) == len(px.index)
    assert rel < TOL, f"정기 경로 불일치 rel={rel:.2e}"
    print(f"[OK] 대조 A 정기변경 동치 (최대 상대차 {rel:.2e})")


def test_adhoc_exclusion_equivalence():
    """대조 B: 수시편출 - 3.4 제수 조정(델타M<0) == 드리프트 후 정규화."""
    px = _prices(130)
    dates = px.index
    w0 = pd.Series([.2157, .1843, .18, .18, .1281, .1058, .0061], index=CODES)
    f0 = pd.Series([12405393.9, 10598120.1, 90214.2, 13614.9,
                    3183.1, 2629.4, 1125.4], index=CODES)
    d_ex, gone = dates[60], "089030"

    # 경로 A: 인서님 저수준 - IIF·제수 + 편출일 제수 조정
    iif, B = ic.rebalance(ic.BASE_INDEX_LEVEL, w0, f0)
    p0, members = px.iloc[0], list(CODES)
    lvA = {dates[0]: ic.BASE_INDEX_LEVEL}
    for d in dates[1:]:
        M = ic.calc_market_cap_M(f0[members], iif[members],
                                 px.loc[d, members], p0[members])
        lvA[d] = ic.calc_index_level(M, B)
        if d == d_ex:
            contrib = float(iif[gone] * f0[gone] * px.loc[d, gone] / p0[gone])
            B = ic.adjust_base_market_cap(B, M, -contrib)   # 델타M = -기여분
            members = [c for c in members if c != gone]
    lvA = pd.Series(lvA)

    # 경로 B: 소연 - 드리프트 후 편출·정규화 이벤트
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

    rel = float(((lvA - lvB).abs() / lvB).max())
    assert rel < TOL, f"수시 경로 불일치 rel={rel:.2e}"
    assert abs(lvA[dates[61]] - lvB[dates[61]]) / lvB[dates[61]] < TOL  # 익일 무점프
    print(f"[OK] 대조 B 수시편출 '정규화 = 제수 흡수' 동치 (최대 상대차 {rel:.2e})")


def test_adapter_rejects_missing_fmc():
    """어댑터 fail-closed: 정기변경 일자·종목별 fmc 누락 시 명시적 실패."""
    e = [make_event(pd.Timestamp(ic.BASE_DATE), "regular",
                    pd.Series([0.5, 0.5], index=CODES[:2]))]
    try:
        to_reconstitution_events(e, {})
        raise AssertionError("누락이 통과됨")
    except ValueError:
        pass

    d = pd.Timestamp(ic.BASE_DATE)
    try:
        to_reconstitution_events(e, {d: pd.Series([100.0], index=CODES[:1])})
        raise AssertionError("종목별 누락이 통과됨")
    except ValueError:
        print("[OK] 어댑터 일자·종목별 fmc 누락 fail-closed")


if __name__ == "__main__":
    test_regular_path_equivalence()
    test_adhoc_exclusion_equivalence()
    test_adapter_rejects_missing_fmc()
    print("\n3/3 동치성 공동 대조 통과 - 파트 간 접합 검증 완료")
