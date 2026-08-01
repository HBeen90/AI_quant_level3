# -*- coding: utf-8 -*-
"""
ablation_study.py ― 규칙 층별 기여도 (Ablation)
=================================================
"어느 규칙이 무엇을 하고 있는가"를 층을 하나씩 켜며 잰다. 외부 리뷰가
가장 강하게 요청한 항목이다.

층 구성 ― 이 지수는 알파 전략이 아니라 규칙 기반 지수다
  일반적인 전략 ablation(신호 → 필터 → 비용)과 축이 다르다. 여기서는
  **조문 층**을 켠다.

    규칙 0  앵커군   메모리 제조 + HBM 양산 (필수 편입)
    규칙 A  핵심군   HBM 매출 노출도 30% 이상
    규칙 C  위성군   메모리향 70% + 공정 확인 + 위원회 확인
    버퍼     유지 임계값 27/67 히스테리시스
    캡       월말 30% 초과 -> 25%, D+2 집행

'산출 불가'도 결과다
  규칙 0 만으로는 지수가 성립하지 않는다. 가중 규칙이 비앵커에 60%를
  배분하도록 정하고 있어 비앵커가 0종목이면 배분이 불가능하다.
  **규칙 0 + A 도 마찬가지다** ― 2023년까지 노출도 30%를 넘는 기업이 그
  시점 자료 기준으로 존재하지 않았기 때문이다. 즉 위성군(규칙 C)이 없으면
  이 지수는 표본의 앞 8회차를 산출할 수 없다.

  이 두 행을 표에서 빼면 안 된다. 규칙 C 의 존재 이유가 거기 있다.

Sharpe 에 대하여
  무위험수익률 0 을 가정한 단순 비율(CAGR / 연변동성)이다. 국고채 수익률을
  차감하지 않았으므로 **절대 수준을 인용하지 말고 층 간 비교로만** 쓴다.
  표에 가정을 함께 적는다.

경계
  엔진을 수정하지 않는다. 스냅샷의 `eligible` 과 `ConfigV2` 만 조작하고,
  캡은 이벤트 생성 단계에서 예약을 건너뛴다. 회전율·성과는 전부
  `simulate_index` 가 낸다.

사용
    python analysis/ablation_study.py \\
        --snapshots data/snapshots --prices-cache out/px.csv --out out/backtest
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
from backtest.backtest import (ann_vol, annualized_turnover,  # noqa: E402
                               build_event_schedule, cagr, make_event,
                               max_drawdown, simulate_index)
from src.rebalance import ConfigV2, InfeasibleComposition, monitor  # noqa: E402

ANCHOR, CORE, SAT = "anchor", "core", "satellite"


def _mask(snaps: dict, keep: set) -> dict:
    """허용 군 밖의 종목을 부적격으로 돌린 스냅샷 사본."""
    out = {}
    for d, s in snaps.items():
        t = s.copy()
        t.loc[~t["group"].isin(keep), "eligible"] = False
        out[d] = t
    return out


def build_events(prices: pd.DataFrame, snaps: dict, cfg: ConfigV2,
                 use_cap: bool) -> list:
    """캡을 끌 수 있는 이벤트 생성. 정기변경 목표는 엔진 산출을 그대로 쓴다."""
    base, _ = build_event_schedule(prices, snaps, cfg=cfg)
    unsupported = {e["reason"] for e in base} - {"regular", "cap"}
    if unsupported:
        raise NotImplementedError(f"지원하지 않는 이벤트: {sorted(unsupported)}")
    if use_cap:
        return base

    regular = {pd.Timestamp(e["effective_date"]): e["target_weights"]
               for e in base if e["reason"] == "regular"}
    rets = prices.pct_change(fill_method=None)
    dates = prices.index[prices.index >= min(regular)]
    events, w = [], None
    for d in dates:
        if w is not None:
            r = rets.loc[d].reindex(w.index)
            if r.isna().any():
                raise ValueError(f"{d.date()} 수익률 결측")
            w = w * (1.0 + r)
            w = w / w.sum()
        if d in regular:
            w = regular[d].copy()
            events.append(make_event(d, "regular", w))
    return events


def run_layer(prices: pd.DataFrame, snaps: dict, keep: set,
              policy: str, use_cap: bool) -> dict | None:
    """한 층 조합의 지수를 재생한다. 산출 불가면 None 과 사유를 돌려준다."""
    cfg = ConfigV2.with_policy(policy)
    s = _mask(snaps, keep)
    try:
        ev = build_events(prices, s, cfg, use_cap)
    except InfeasibleComposition as e:
        return {"infeasible": str(e)}
    except ValueError as e:
        return {"infeasible": str(e)}
    bt = simulate_index(prices, ev, base=1000.0)
    log = bt.attrs["event_log"]
    n = [len(e["target_weights"]) for e in ev if e["reason"] == "regular"]
    lv = bt["level"]
    vol = ann_vol(lv)
    g = cagr(lv)
    return {
        "bt": bt,
        "CAGR": g,
        "연변동성": vol,
        "MDD": max_drawdown(lv)["mdd"],
        "Sharpe(rf=0)": g / vol if vol and np.isfinite(vol) and vol > 0 else np.nan,
        "연율화회전율": annualized_turnover(bt),
        "정기 평균종목수": float(np.mean(n)) if n else np.nan,
        "캡 발동": int((log["reason"] == "cap").sum()),
        "최종레벨": float(lv.iloc[-1]),
    }


#: 누적 ablation 층. (라벨, 허용 군, 버퍼 정책, 캡 사용)
LAYERS = [
    ("① 규칙 0 (앵커만)", {ANCHOR}, "none", False),
    ("② + 규칙 A (핵심군)", {ANCHOR, CORE}, "none", False),
    ("③ + 규칙 C (위성군)", {ANCHOR, CORE, SAT}, "none", False),
    ("④ + 버퍼 27/67", {ANCHOR, CORE, SAT}, "mid", False),
    ("⑤ + 월간 캡 (현행)", {ANCHOR, CORE, SAT}, "mid", True),
]

#: 참고 ― 한 층만 제거(leave-one-out). 누적표가 못 보여주는 것을 채운다.
LEAVE_ONE_OUT = [
    ("현행 - 규칙 A", {ANCHOR, SAT}, "mid", True),
    ("현행 - 규칙 C", {ANCHOR, CORE}, "mid", True),
    ("현행 - 버퍼", {ANCHOR, CORE, SAT}, "none", True),
    ("현행 - 캡", {ANCHOR, CORE, SAT}, "mid", False),
]

COLS = ["CAGR", "연변동성", "MDD", "Sharpe(rf=0)", "연율화회전율",
        "정기 평균종목수", "캡 발동", "최종레벨"]


def table(prices, snaps, layers) -> pd.DataFrame:
    rows = []
    for label, keep, policy, cap in layers:
        r = run_layer(prices, snaps, keep, policy, cap)
        if r is None or "infeasible" in r:
            rows.append({"구성": label, **{c: np.nan for c in COLS},
                         "비고": "산출 불가 - " + r["infeasible"][:46]})
        else:
            rows.append({"구성": label, **{c: r[c] for c in COLS}, "비고": ""})
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots", required=True)
    ap.add_argument("--prices-cache", required=True)
    ap.add_argument("--out", default="out/backtest")
    a = ap.parse_args()

    import run_backtest as rb                                   # noqa: E402
    snaps = rb.load_snapshots(a.snapshots)
    tickers = sorted({t for df in snaps.values() for t in df["ticker"]})
    px = rb.fetch_prices(tickers, min(snaps).strftime("%Y%m%d"),
                         as_of_today().strftime("%Y%m%d"), cache=a.prices_cache)
    px = px.dropna(axis=1, how="all")

    cum = table(px, snaps, LAYERS)
    loo = table(px, snaps, LEAVE_ONE_OUT)

    os.makedirs(a.out, exist_ok=True)
    cum.to_csv(os.path.join(a.out, "ablation_cumulative.csv"),
               index=False, encoding="utf-8-sig")
    loo.to_csv(os.path.join(a.out, "ablation_leave_one_out.csv"),
               index=False, encoding="utf-8-sig")

    pd.set_option("display.width", 200)
    print("[누적 Ablation ― 층을 하나씩 켠다]\n",
          cum.round(4).to_string(index=False))
    print("\n[Leave-one-out ― 현행에서 한 층만 뺀다]\n",
          loo.round(4).to_string(index=False))
    print("\n[Sharpe] 무위험수익률 0 가정(CAGR/연변동성). 절대 수준을 인용하지 "
          "말고 층 간 비교로만 쓸 것.")
    print("[산출 불가] 규칙 0·A 만으로는 지수가 성립하지 않는다 - 가중 규칙이 "
          "비앵커에 60%를 배분하는데 2023년까지 노출도 30% 통과 종목이 0개였다. "
          "위성군(규칙 C)의 존재 이유가 여기 있다.")
    print(f"[저장] {a.out}/ablation_cumulative.csv · ablation_leave_one_out.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
