# -*- coding: utf-8 -*-
"""
frequency_sensitivity.py ― 정기변경 주기 민감도 (P2)
======================================================
상용지수 검토(P2)가 남긴 질문에 실측으로 답한다.

    "반기 재선정은 테마 변화를 늦게 반영하는 것 아닌가.
     분기 재가중을 추가하면 무엇이 좋아지고 무엇을 잃는가."

무엇을 비교하는가 ― 재선정과 재가중을 분리한다
  두 가지를 섞어 말하면 답이 안 나온다.

    재선정(reselection) : 구성종목을 다시 고르는 것. PIT 판정 스냅샷이
                          필요하며 현재 반기(6·12월)분 13회만 존재한다.
                          분기 재선정은 스냅샷이 없어 **측정 불가**다.
    재가중(reweight)    : 구성종목은 그대로 두고 목표비중만 다시 계산하는 것.
                          유동시총만 있으면 되므로 측정 가능하다.

  따라서 이 스크립트가 재는 것은 **재가중 주기**다. 분기 재선정을 측정했다고
  말하면 안 된다.

유동시총 근사 ― 명시된 가정
  중간 시점의 유동시총은 관측되지 않으므로, 직전 스냅샷의 유동시총을 가격
  수익률로 이월한다(주식수·유동비율 불변 가정). 정기변경일에는 실제 스냅샷
  값으로 리셋되므로 오차가 누적되지 않는다. 배당·증자·유동비율 변경은
  반영하지 않으며, 이는 **분기 재가중을 과대평가하지도 과소평가하지도 않는
  방향 중립적 근사**다(모든 정책에 동일하게 적용된다).

캡과의 상호작용 ― 이번 분석의 핵심
  회전율의 60.5%가 이미 월말 캡에서 나오고, 5종목 미만 구간에서는 캡이
  균등비중 복원으로 퇴화한다(`cap_feasibility.py`). 분기 재가중을 추가하면
  그 위에 또 하나의 되돌림이 얹힌다. 밴드(안 C)에서 진동이 발생한 것과 같은
  구조가 나타나는지 확인해야 한다.

경계
  엔진 코드를 수정하지 않는다. 이벤트 목록만 만들어 기존 `simulate_index`
  에 먹이며, 회전율·성과 수치는 전부 엔진이 낸다.

사용
    python analysis/frequency_sensitivity.py \\
        --snapshots data/snapshots --prices-cache out/px.csv --out out/backtest

산출
    frequency_sensitivity.csv        정책별 성과·회전율·집중도
    frequency_reweight_events.csv    재가중 이벤트 원장
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "analysis"))

from analysis.index_calendar import as_of_today  # noqa: E402
from backtest.backtest import (annualized_turnover, build_event_schedule,  # noqa: E402
                               cagr, make_event, max_drawdown, simulate_index,
                               ann_vol)
from src.rebalance import ConfigV2, assign_weights_v2, monitor  # noqa: E402

#: 재가중 시점. 정기변경이 6·12월이므로 분기는 3·9월을 추가한다.
QUARTERLY_MONTHS = (3, 9)


def _reweight_dates(dates: pd.DatetimeIndex, regular: set,
                    months: tuple) -> list:
    """해당 월의 **마지막 거래일 다음 영업일**이 아니라, 그 달 첫 거래일 기준
    ― 정기변경(만기일 익주)과 겹치지 않게 분기 경계만 잡는다."""
    out = []
    for (y, m), grp in pd.Series(dates, index=dates).groupby(
            [dates.year, dates.month]):
        if m in months:
            d = grp.index[0]
            if d not in regular:
                out.append(d)
    return sorted(out)


def replay(prices: pd.DataFrame, snaps: dict, cfg: ConfigV2,
           reweight: bool) -> tuple:
    """반사실 이벤트 목록. reweight=False 면 기준선(현행 반기) 재구현."""
    base_events, _ = build_event_schedule(prices, snaps, cfg=cfg)
    reasons = {e["reason"] for e in base_events}
    if reasons - {"regular", "cap"}:
        raise NotImplementedError(
            f"지원하지 않는 이벤트: {sorted(reasons - {'regular', 'cap'})} ― "
            "수시편출·긴급편입 구간은 구성종목 경로가 갈리므로 이 방식으로 "
            "주기를 비교하지 않는다.")
    regular = {pd.Timestamp(e["effective_date"]): e["target_weights"]
               for e in base_events if e["reason"] == "regular"}
    snap_by_date = {pd.Timestamp(d): snaps[d] for d in snaps}

    rets = prices.pct_change(fill_method=None)
    dates = prices.index[prices.index >= min(regular)]
    next_dates = pd.Series(dates, index=dates).shift(-1)
    is_month_end = next_dates.notna() & (next_dates.dt.month != dates.month)
    rw_dates = set(_reweight_dates(dates, set(regular), QUARTERLY_MONTHS)) \
        if reweight else set()

    events, log = [], []
    w = None
    members = None            # ticker -> group
    fmc0 = None               # 직전 스냅샷 유동시총
    px0 = None                # 그 시점 가격
    pending_cap: dict = {}

    def book_cap(i: int) -> None:
        pending_cap.clear()
        if w is None:
            return
        adj, changed = monitor(w, cfg)
        if changed and i + 2 < len(dates):
            pending_cap[dates[i + 2]] = adj

    for i, d in enumerate(dates):
        if w is not None:
            r = rets.loc[d].reindex(w.index)
            if r.isna().any():
                raise ValueError(f"{d.date()} 구성종목 수익률 결측")
            w = w * (1.0 + r)
            w = w / w.sum()

        if d in regular:                                   # 정기변경
            w = regular[d].copy()
            snap = snap_by_date[d]
            members = snap.set_index("ticker")["group"].reindex(w.index)
            fmc0 = pd.to_numeric(
                snap.set_index("ticker")["float_mcap"], errors="coerce"
            ).reindex(w.index)
            px0 = prices.loc[d].reindex(w.index)
            events.append(make_event(d, "regular", w))
            book_cap(i)
            continue

        if d in pending_cap:                               # 월간 캡
            tgt = pending_cap.pop(d)
            tgt = tgt[tgt.index.intersection(w.index)]
            w = tgt / tgt.sum()
            events.append(make_event(d, "cap", w))

        elif d in rw_dates and w is not None:              # 분기 재가중
            fmc = fmc0 * (prices.loc[d].reindex(w.index) / px0)   # 이월 근사
            m = pd.DataFrame({"ticker": w.index, "group": members.values,
                              "float_mcap": fmc.values})
            try:
                tgt = assign_weights_v2(m, cfg)
            except Exception as e:
                log.append({"date": d, "n": len(w), "skipped": str(e)})
                tgt = None
            if tgt is not None:
                t = 0.5 * float((tgt.reindex(w.index).fillna(0.0) - w).abs().sum())
                log.append({"date": d, "n": len(w), "one_way_turnover": t,
                            "anchor_before": float(w[members.eq("anchor")].sum()),
                            "anchor_after": float(
                                tgt[members.reindex(tgt.index).eq("anchor")].sum())})
                w = tgt
                events.append(make_event(d, "reweight", w))
                book_cap(i)

        if w is not None and bool(is_month_end.loc[d]):
            book_cap(i)

    return events, pd.DataFrame(log)


def summarize(label: str, bt: pd.DataFrame, base: pd.DataFrame | None) -> dict:
    lg = bt.attrs["event_log"]
    by = lg.groupby("reason")["one_way_turnover"].sum()
    lv = bt["level"]
    md = max_drawdown(lv)
    tot = float(bt["turnover"].sum())
    row = {
        "정책": label,
        "CAGR": cagr(lv),
        "연변동성": ann_vol(lv),
        "MDD": md["mdd"],
        "연율화회전율": annualized_turnover(bt),
        "정기": float(by.get("regular", 0.0)),
        "재가중": float(by.get("reweight", 0.0)),
        "캡": float(by.get("cap", 0.0)),
        "캡 발동": int((lg["reason"] == "cap").sum()),
        "재가중 발동": int((lg["reason"] == "reweight").sum()),
    }
    if base is not None:
        b = float(base["turnover"].sum())
        row["회전율 증가율"] = tot / b - 1.0 if b else np.nan
        row["최종레벨 차이"] = float(lv.iloc[-1] / base["level"].iloc[-1] - 1.0)
    else:
        row["회전율 증가율"] = 0.0
        row["최종레벨 차이"] = 0.0
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots", required=True)
    ap.add_argument("--prices-cache", required=True)
    ap.add_argument("--policy", default="mid",
                    choices=("none", "narrow", "mid", "wide"))
    ap.add_argument("--out", default="out/backtest")
    a = ap.parse_args()

    import run_backtest as rb                                    # noqa: E402
    cfg = ConfigV2.with_policy(a.policy)
    snaps = rb.load_snapshots(a.snapshots)
    tickers = sorted({t for df in snaps.values() for t in df["ticker"]})
    px = rb.fetch_prices(tickers, min(snaps).strftime("%Y%m%d"),
                         as_of_today().strftime("%Y%m%d"), cache=a.prices_cache)
    px = px.dropna(axis=1, how="all")

    # 자기검증: 재가중 없는 반사실이 엔진 기준선을 재현하는가
    engine = simulate_index(px, build_event_schedule(px, snaps, cfg=cfg)[0])
    ev0, _ = replay(px, snaps, cfg, reweight=False)
    bt0 = simulate_index(px, ev0)
    d = float((bt0["turnover"] - engine["turnover"]).abs().max())
    if d > 1e-12:
        sys.exit(f"[FAIL] 반사실 재구현이 엔진과 다르다 (회전율 최대차 {d:.3e})")
    print(f"[자기검증] 반사실 == 엔진 기준선 (회전율 {d:.2e})")

    ev1, log = replay(px, snaps, cfg, reweight=True)
    bt1 = simulate_index(px, ev1)

    tbl = pd.DataFrame([summarize("현행 (반기 재선정·반기 재가중)", bt0, None),
                        summarize("분기 재가중 추가", bt1, bt0)])
    os.makedirs(a.out, exist_ok=True)
    tbl.to_csv(os.path.join(a.out, "frequency_sensitivity.csv"),
               index=False, encoding="utf-8-sig")
    log.to_csv(os.path.join(a.out, "frequency_reweight_events.csv"),
               index=False, encoding="utf-8-sig")

    print("\n[재가중 주기 민감도]\n", tbl.round(4).to_string(index=False))
    print("\n[주의] 재는 것은 **재가중** 주기다. 분기 재선정은 PIT 스냅샷이 "
          "반기분만 존재해 측정 불가이며, 측정했다고 말하면 안 된다.")
    print(f"[저장] {a.out}/frequency_sensitivity.csv · frequency_reweight_events.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
