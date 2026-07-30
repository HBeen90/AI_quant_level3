# -*- coding: utf-8 -*-
"""
sensitivity_v2.py - 버퍼 정책(없음/좁음/중간/넓음) 민감도 분석
=============================================================
목적: v2(정원 폐지) 승인 판단과 유지 임계값 확정을 위한 근거자료.
데이터: 실측 시점별 노출도가 없으므로(수작업 산정 대기) 합성 시나리오를 쓴다.
  - 안정 종목: 노출도가 임계값에서 멀어 정책과 무관하게 편입 유지
  - 경계 종목: 노출도가 30%(핵심)·70%(위성) 주위에서 진동 - 진폭 1~5%p 혼합
  선택 기준(패시브 배점): 연율화 회전율·편출입 횟수·평균 종목 수·평균 노출도·
  HHI·비용 차감 성과. CAGR 자체로 정책을 고르지 않는다(데이터 스누핑).
결과의 해석 한계: 합성이므로 절대값이 아니라 정책 간 '차이'만 의미가 있다.
"""
import os
import sys
import warnings

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from src.rebalance import (ANCHOR, CORE, SAT, BUFFER_POLICIES, ConfigV2,
                          regular_rebalance_v2)
from backtest.backtest import (apply_costs, annualized_turnover, cagr,
                               simulate_index, make_event)

DEFAULT_SEEDS = [20260722 + k for k in range(11)]   # 다중 seed (리뷰 반영)
N_YEARS = 6
REVIEWS_PER_YEAR = 2


def build_universe_spec(rng):
    """합성 유니버스 명세. (군, 기준값, 진동진폭, 위상, 유동시총)"""
    spec = []
    spec += [("A1", ANCHOR, None, 0, 0, 300e12), ("A2", ANCHOR, None, 0, 0, 250e12)]
    # 핵심 안정 5: 노출도 0.40~0.85
    for i, e in enumerate([0.85, 0.70, 0.55, 0.45, 0.40]):
        spec.append((f"C{i}", CORE, e, 0.0, 0, (28 - 4 * i) * 1e12))
    # 핵심 경계 5: 0.30 주위, 진폭 1~5%p
    for i, amp in enumerate([0.01, 0.02, 0.03, 0.04, 0.05]):
        spec.append((f"B{i}", CORE, 0.30, amp, i, (10 - i) * 1e12))
    # 위성 안정 2 + 경계 3 (메모리향 0.70 주위 진동)
    spec += [("S0", SAT, 0.88, 0.0, 0, 7e12), ("S1", SAT, 0.80, 0.0, 0, 5e12)]
    for i, amp in enumerate([0.02, 0.03, 0.05]):
        spec.append((f"T{i}", SAT, 0.70, amp, i, (4 - i) * 1e12))
    return spec


def snapshot_at(spec, t, rng):
    rows = []
    for tkr, grp, base, amp, phase, mcap in spec:
        drift_mcap = mcap * float(np.exp(rng.normal(0, 0.10)))
        if grp == ANCHOR:
            rows.append((tkr.zfill(6), tkr, grp, np.nan, np.nan, drift_mcap, True))
        elif grp == CORE:
            expo = base + amp * (-1) ** (t + phase) \
                + float(rng.normal(0, 0.003))
            rows.append((tkr.zfill(6), tkr, grp, expo, np.nan, drift_mcap, True))
        else:
            mem = base + amp * (-1) ** (t + phase) \
                + float(rng.normal(0, 0.003))
            rows.append((tkr.zfill(6), tkr, grp, 0.10, mem, drift_mcap, True))
    df = pd.DataFrame(rows, columns=["ticker", "name", "group", "exposure",
                                     "mem_ratio", "float_mcap", "eligible"])
    df["eligible"] = df["eligible"].astype(bool)
    return df


def synth_prices(tickers, dates, rng):
    r = rng.normal(0.0004, 0.022, size=(len(dates), len(tickers)))
    return pd.DataFrame(100 * np.exp(np.cumsum(r, axis=0)),
                        index=dates, columns=tickers)


def run_policy(policy, spec, snaps_by_date, prices, review_dates):
    cfg = ConfigV2.with_policy(policy)
    prev, events, adds, drops, ns, expos, hhis, top1s = set(), [], 0, 0, [], [], [], []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for d in review_dates:
            out = regular_rebalance_v2(snaps_by_date[d], prev, cfg)
            if prev:                                   # 최초 구성은 교체로 안 셈
                adds += len(out["added"])
                drops += len(out["dropped"])
            prev = set(out["weights"].index)
            w = out["weights"]
            events.append(make_event(d, "regular", w))
            ns.append(out["n"])
            m = out["members"].set_index("ticker")
            na = m.index[m["group"] != ANCHOR]
            wexpo = float((w[na] * m.loc[na, "exposure"].fillna(
                m.loc[na, "mem_ratio"])).sum() / w[na].sum())
            expos.append(wexpo)
            hhis.append(float((w ** 2).sum()))
            top1s.append(float(w.max()))
    bt = simulate_index(prices, events)
    net = apply_costs(bt, cost_bp=30.0)  # 왕복비용 30bp x 편도 회전율
    return {
        "정책": policy,
        "유지임계(핵심/위성)": f"{cfg.hold_core:.0%}/{cfg.hold_sat:.0%}",
        "연율화 회전율": annualized_turnover(bt),
        "편입 횟수": adds,
        "편출 횟수": drops,
        "편입+편출 횟수": adds + drops,
        "평균 종목 수": float(np.mean(ns)),
        "평균 노출도(비중가중)": float(np.mean(expos)),
        "평균 HHI": float(np.mean(hhis)),
        "평균 최대비중": float(np.mean(top1s)),
        "총수익률(무비용)": float(bt["level"].iloc[-1] / bt["level"].iloc[0] - 1),
        "총수익률(30bp차감)": float(net.iloc[-1] / net.iloc[0] - 1),
        "CAGR(30bp)": float(cagr(net)),
        "비용 드래그(연, bp)": (cagr(bt["level"]) - cagr(net)) * 1e4,
    }


def run_seed(seed: int) -> pd.DataFrame:
    """seed 1개에 대해 4개 정책을 '동일한' 스냅샷·가격 경로로 평가한다."""
    spec = build_universe_spec(np.random.default_rng(seed))
    dates = pd.bdate_range("2020-06-15", periods=int(252 * N_YEARS))
    review_dates = [dates[i * 126] for i in range(N_YEARS * REVIEWS_PER_YEAR)]
    snap_rng = np.random.default_rng(seed + 1)
    snaps = {d: snapshot_at(spec, t, snap_rng)
             for t, d in enumerate(review_dates)}      # 전 정책 공유
    prices = synth_prices([s[0].zfill(6) for s in spec], dates,
                          np.random.default_rng(seed + 2))
    rows = [run_policy(p, spec, snaps, prices, review_dates)
            for p in ("none", "narrow", "mid", "wide")]
    df = pd.DataFrame(rows)
    df.insert(0, "seed", seed)
    return df


def main(seeds=None, out_path=None):
    import argparse
    if seeds is None:
        ap = argparse.ArgumentParser(
            description="버퍼 정책 민감도 - 다중 seed 중앙값·범위 보고")
        ap.add_argument("--seeds", type=int, default=len(DEFAULT_SEEDS),
                        help="합성 seed 개수 (기본 11)")
        ap.add_argument("--out", default=os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "buffer_policy_sensitivity_v2.csv"),
            help="결과 CSV 경로 (기본: 스크립트 폴더)")
        args = ap.parse_args()
        seeds = [DEFAULT_SEEDS[0] + k for k in range(args.seeds)]
        out_path = args.out
    long = pd.concat([run_seed(s) for s in seeds], ignore_index=True)
    metrics = ["연율화 회전율", "편입 횟수", "편출 횟수", "편입+편출 횟수",
               "평균 종목 수", "평균 노출도(비중가중)", "평균 HHI",
               "CAGR(30bp)", "비용 드래그(연, bp)"]
    med = long.groupby("정책")[metrics].median()
    lo = long.groupby("정책")[metrics].min()
    hi = long.groupby("정책")[metrics].max()
    order = ["none", "narrow", "mid", "wide"]
    pd.set_option("display.width", 200)
    print(f"== 중앙값 (seed {len(seeds)}개) ==")
    print(med.loc[order].round(4).to_string())
    print("\n== 범위 [최소, 최대] ==")
    rng_tbl = lo.loc[order].round(3).astype(str) + " ~ " + \
        hi.loc[order].round(3).astype(str)
    print(rng_tbl.to_string())
    print("\n주의: 합성 시나리오 - 정책 간 차이만 유효. 유지 임계값 27/67은"
          "\n현행 운영값이며, 최소 2회차 실측 축적 후 방법론 개정 절차로만"
          "\n재검토한다. 합성 결과는 최적값을 주장하지 않는다.")
    long.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"\nlong-format 결과 저장: {out_path}")
    return long


if __name__ == "__main__":
    main()
