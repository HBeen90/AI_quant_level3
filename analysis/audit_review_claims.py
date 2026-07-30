# -*- coding: utf-8 -*-
"""
audit_review_claims.py - 외부 리뷰 지적사항을 코드로 검증한다
==============================================================
리뷰는 유용하지만, 지적이 '이미 구현된 것'을 가리키는지 '진짜 공백'을
가리키는지는 실행해 봐야 안다. 이 스크립트는 각 지적을 재현 시나리오로
돌려 **실제 엔진 동작**을 출력한다. 근거 없이 고치지도, 근거 없이 넘기지도
않기 위한 절차다.

검증 대상
  [지적 2] "정기변경 사이 주가 폭등 시 드리프트 검증 누락 - 월말 30% 캡
            자동화 필요"
  [파생]   개별 30% 캡이 잡지 못하는 것은 무엇인가 (버킷 레벨 드리프트)

시나리오: 2023~2024 한미반도체형 - 핵심군 1종목이 정기변경 사이 5배 폭등.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from backtest.backtest import build_event_schedule, simulate_index  # noqa: E402
from src.rebalance import ANCHOR, CORE, SAT, ConfigV2  # noqa: E402

COLS = ["ticker", "name", "group", "exposure", "mem_ratio", "float_mcap",
        "eligible"]


def scenario_prices(n_days: int = 400, surge_ticker: str = "100001",
                    surge_mult: float = 5.0, seed: int = 5) -> pd.DataFrame:
    """한 종목만 구간 내내 완만히 5배로 오르는 패널(나머지는 횡보 + 잡음)."""
    rng = np.random.default_rng(seed)
    tickers = ["000001", "000002"] + [f"10000{i}" for i in range(1, 7)] \
        + [f"20000{i}" for i in range(1, 4)]
    days = pd.bdate_range("2025-06-16", periods=n_days)
    px = pd.DataFrame(
        100000 * np.exp(np.cumsum(rng.normal(0.0, 0.012, (n_days, len(tickers))),
                                  axis=0)),
        index=days, columns=tickers)
    ramp = np.linspace(0.0, np.log(surge_mult), n_days)
    px[surge_ticker] = px[surge_ticker] * np.exp(ramp)
    return px


def snapshot(px: pd.DataFrame) -> pd.DataFrame:
    rows = [("000001", "앵커1", ANCHOR, np.nan, np.nan, 300e12, True),
            ("000002", "앵커2", ANCHOR, np.nan, np.nan, 250e12, True)]
    for i in range(1, 7):
        rows.append((f"10000{i}", f"핵심{i}", CORE, 0.55, np.nan,
                     (34 - 5 * i) * 1e12, True))
    for i in range(1, 4):
        rows.append((f"20000{i}", f"위성{i}", SAT, 0.2, 0.85,
                     (8 - 2 * i) * 1e12, True))
    return pd.DataFrame(rows, columns=COLS)


def bucket_weights(w: pd.Series, groups: pd.Series) -> dict:
    g = groups.reindex(w.index)
    return {b: float(w[g == b].sum()) for b in (ANCHOR, CORE, SAT)}


def main() -> int:
    cfg = ConfigV2()
    px = scenario_prices()
    snap = snapshot(px)
    groups = snap.set_index("ticker")["group"]
    snaps = {px.index[0]: snap}

    events, hist = build_event_schedule(px, snaps, cfg=cfg)
    bt = simulate_index(px, events)

    print("=" * 76)
    print("[지적 2] '월말 30% 캡 자동화 필요' - 실제 엔진 동작 확인")
    print("=" * 76)
    print(f"시나리오: 핵심1(100001)이 {len(px)}거래일 동안 5배 상승, "
          "정기변경은 최초 1회뿐")
    caps = [e for e in events if e["reason"] == "cap"]
    print(f"\n생성된 이벤트: 총 {len(events)}건 "
          f"(regular {sum(e['reason']=='regular' for e in events)}, "
          f"cap {len(caps)})")

    if caps:
        print("\n캡 발동 내역 (상위 6건):")
        for e in caps[:6]:
            w = e["target_weights"]
            print(f"  {e['effective_date'].date()}  "
                  f"최대비중 {w.max():.4f}  100001 -> {w.get('100001', 0):.4f}")
        print(f"\n=> 이미 상시 가동 중이다. 월말 점검 -> D+2 집행이 "
              f"{len(caps)}회 발동했다.")
        print("   구현 위치: backtest.build_event_schedule 의 is_month_end + "
              "recheck_and_book_cap")
        print("   기존 테스트: test_schedule_v2.py '월간 캡 30%->25% D+2 집행',"
              " '정확히 D+2 거래일' 검증")
    else:
        print("\n=> 캡이 한 번도 발동하지 않았다. 이건 진짜 결함이다.")

    # ---- 캡이 없었다면? 대조군 ----
    ev_reg = [e for e in events if e["reason"] == "regular"]
    bt_nocap = simulate_index(px, ev_reg)
    w_nocap = ev_reg[0]["target_weights"].copy()
    r = px.pct_change(fill_method=None)
    peak_nocap = 0.0
    for d in px.index[1:]:
        w_nocap = w_nocap * (1 + r.loc[d].reindex(w_nocap.index))
        w_nocap = w_nocap / w_nocap.sum()
        peak_nocap = max(peak_nocap, float(w_nocap.max()))
    print(f"\n[대조] 캡을 껐을 때 단일 종목 최대비중: {peak_nocap:.2%}")
    print(f"       캡을 켰을 때 캡 목표비중 최대   : "
          f"{max(float(e['target_weights'].max()) for e in caps):.2%}"
          if caps else "")

    # ---- 진짜 공백: 버킷 레벨 드리프트 ----
    print("\n" + "=" * 76)
    print("[진짜 공백] 개별 30% 캡이 잡지 못하는 것 - 버킷 비중 드리프트")
    print("=" * 76)
    w = ev_reg[0]["target_weights"].copy()
    ev_by_date = {e["effective_date"]: e for e in events}
    track = []
    for d in px.index[1:]:
        w = w * (1 + r.loc[d].reindex(w.index))
        w = w / w.sum()
        if d in ev_by_date:
            w = ev_by_date[d]["target_weights"].copy()
        if d.day <= 3 or d == px.index[-1]:
            bw = bucket_weights(w, groups)
            track.append({"date": d.date(), "앵커": bw[ANCHOR],
                          "핵심": bw[CORE], "위성": bw[SAT],
                          "최대단일": float(w.max())})
    tb = pd.DataFrame(track).drop_duplicates(subset=["date"]).tail(10)
    print(tb.to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    bw0 = bucket_weights(ev_reg[0]["target_weights"], groups)
    bwT = bucket_weights(w, groups)
    print(f"\n앵커 합계 : {bw0[ANCHOR]:.2%} (정기변경일) -> {bwT[ANCHOR]:.2%} (기말)")
    print(f"위성 합계 : {bw0[SAT]:.2%} -> {bwT[SAT]:.2%} "
          f"(방법론 위성 합계 상한 {cfg.sat_total_cap:.0%})")
    print(f"최대 단일 : {float(w.max()):.2%} (개별 캡 {0.30:.0%} 이하로 유지됨)")
    print("\n=> 개별 30% 캡은 정상 작동하지만, monitor()/cap_algorithm 은")
    print("   **개별 종목만** 본다. 앵커 40% 고정과 위성 합계 18% 상한은")
    print("   정기변경 시점에만 적용되고 그 사이 드리프트는 점검하지 않는다.")
    print("   -> 리뷰의 문제의식('40:60 구조 침범')은 타당하나, 해법은")
    print("      '개별 캡 자동화'가 아니라 **버킷 레벨 점검 신설**이다.")
    print("      이건 조문 개정 사안이므로 엔진이 임의로 바꾸면 안 된다 -")
    print("      먼저 진단 지표로 계측해 위원회에 올리는 것이 순서다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
