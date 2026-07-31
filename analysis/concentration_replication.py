# -*- coding: utf-8 -*-
"""
concentration_replication.py ― 집중도 계량 + 복제 가능성 실증 (파트4 결손 보강)
==============================================================================
패시브 채점 기준 대비, 파트4(rebalance.py · backtest.py)가 **수치를 하나도
내지 못하던 두 항목**을 채운다.

  [리스크·비용 15점 / "집중도"]
      backtest.summary() 가 내는 지표는 누적수익률·CAGR·연변동성·MDD·
      회전율·상관계수 여섯 개다. 집중도 지표는 **하나도 없다.**
      평균 3.72종목 · 앵커 버킷 최대 85% 인 지수에서 집중도는 부수 지표가
      아니라 대표 리스크인데, 그것을 재는 코드가 없었다.

  [백테스트·검증 20점 / "투자가능성·추종오차"]
      benchmark_inference 의 추종오차 17.18% 는 **KRX 반도체 대비 괴리**이지
      "이 지수를 따라갈 수 있는가"의 답이 아니다. tracking_metrics() 는
      외부 tracker 레벨을 요구하는데 그 레벨을 만드는 코드가 없었다.
      여기서 만든다 ― 체결 지연과 거래대금 참여율 제약을 건 복제 포트폴리오.

경계
  엔진 코드(src/·backtest/)를 수정하지 않는다. 확정 실행 매니페스트가
  '코드 무변경'을 유효 조건으로 삼기 때문이다. 두 계량 모두 엔진 산출물
  (이벤트·레벨)을 소비하는 분석 계층으로 구현했다. 발표 후 조문·지표를
  확정하면 그때 summary() 에 접는다.

사용
    python analysis/concentration_replication.py \
        --snapshots data/snapshots --prices-cache out/px.csv \
        --adv data/adv60.csv --aum 1000,3000,10000 --out out/backtest

산출
    concentration_daily.csv     일별 HHI·유효종목수·최대비중·버킷 합계
    concentration_summary.csv   구간 요약 + 상품화 참고 한도 대조
    replication_tracking.csv    AUM·참여율별 추종오차·누적 추종차이
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
from backtest.backtest import (build_event_schedule, simulate_index,  # noqa: E402
                               tracking_metrics)
from src.rebalance import ANCHOR, CORE, SAT, ConfigV2  # noqa: E402

TRADING_DAYS = 252

#: 상품화 시 대조할 참고 한도. **법률 자문이 아니다** ― 실제 적용 규정은
#: 상품 구조(ETF/ETN)·상장지·수탁사에 따라 다르므로, 여기서는 널리 쓰이는
#: UCITS 5/10/40 형태를 '대조용 기준선'으로만 둔다. 위반 여부가 아니라
#: **어느 규격에 걸리는지**를 보여주는 것이 목적이다.
REFERENCE_LIMITS = {
    "단일종목 10%": ("max_weight", 0.10),
    "단일종목 20%": ("max_weight", 0.20),
    "5% 초과 종목 합계 40%": ("sum_over_5pct", 0.40),
    "유효종목수 5 이상": ("effective_n_min", 5.0),
}


# ----------------------------------------------------------------------
# 1) 집중도
# ----------------------------------------------------------------------
def concentration(w: pd.Series, groups: pd.Series | None = None) -> dict:
    """한 시점 비중 벡터의 집중도.

    HHI 는 합이 1인 비중의 제곱합이다. 역수 1/HHI 가 **유효종목수**로,
    "실질적으로 몇 종목에 분산돼 있는가"를 종목 수 단위로 읽게 해 준다.
    명목 7종목이어도 앵커 둘이 60%를 먹으면 유효종목수는 3 근처로 떨어진다
    ― 명목 종목 수만 보고하면 이 사실이 드러나지 않는다.
    """
    w = w.astype(float)
    w = w / w.sum()
    hhi = float((w ** 2).sum())
    s = w.sort_values(ascending=False)
    out = {
        "n": int(len(w)),
        "HHI": hhi,
        "유효종목수": 1.0 / hhi if hhi > 0 else np.nan,
        "최대비중": float(s.iloc[0]),
        "상위3비중": float(s.iloc[:3].sum()),
        "5%초과합계": float(w[w > 0.05].sum()),
    }
    if groups is not None:
        g = groups.reindex(w.index)
        for name, key in ((ANCHOR, "앵커합계"), (CORE, "핵심합계"), (SAT, "위성합계")):
            out[key] = float(w[g.eq(name)].sum())
    return out


def concentration_history(prices: pd.DataFrame, events: list,
                          groups_by_date: dict) -> pd.DataFrame:
    """**일별** 집중도. 리셋 시점만 재면 안 되는 이유가 있다.

    개별 캡(30%->25%)은 월말에만 발동하므로, 리셋 직후 값만 보고하면
    한 달간 드리프트로 올라간 최대비중이 통째로 빠진다. 집중도는 표시용
    숫자가 아니라 보유 기간 내내 실재한 상태이므로 전 거래일을 잰다.
    """
    ev: dict = {}
    for e in sorted(events, key=lambda x: x["effective_date"]):
        ev[pd.Timestamp(e["effective_date"])] = e["target_weights"]
    rets = prices.pct_change(fill_method=None)
    dates = prices.index[prices.index >= min(ev)]

    rows, w, groups = [], None, None
    last_reset = None
    for d in dates:
        if w is not None:
            r = rets.loc[d].reindex(w.index)
            if r.isna().any():
                raise ValueError(f"{d.date()} 구성종목 수익률 결측: "
                                 f"{r.index[r.isna()].tolist()}")
            w = w * (1.0 + r)
            w = w / w.sum()
        if d in ev:
            w = ev[d].copy()
            last_reset = max([x for x in groups_by_date if x <= d], default=None)
        if last_reset is not None:
            groups = groups_by_date[last_reset].reindex(w.index)
        rows.append({"date": d, **concentration(w, groups)})
    return pd.DataFrame(rows).set_index("date")


def concentration_summary(daily: pd.DataFrame) -> pd.DataFrame:
    """구간 요약 + 참고 한도 대조. '며칠이나 넘겼는가'를 같이 낸다.

    평균만 보고하면 "평균은 한도 안"이라는 문장이 만들어지는데, 한도는
    평균이 아니라 상시 지켜야 하는 제약이다. 초과 거래일 수를 함께 낸다.
    """
    rows = []
    for col in ("HHI", "유효종목수", "최대비중", "상위3비중", "5%초과합계",
                "앵커합계", "핵심합계", "위성합계", "n"):
        if col not in daily.columns:
            continue
        s = daily[col].dropna()
        rows.append({"지표": col, "평균": s.mean(), "중앙": s.median(),
                     "최소": s.min(), "최대": s.max()})
    tbl = pd.DataFrame(rows)

    checks = []
    total = len(daily)
    for label, (kind, lim) in REFERENCE_LIMITS.items():
        if kind == "max_weight":
            bad = daily["최대비중"] > lim
        elif kind == "sum_over_5pct":
            bad = daily["5%초과합계"] > lim
        else:
            bad = daily["유효종목수"] < lim
        checks.append({"지표": f"[참고한도] {label}",
                       "평균": np.nan, "중앙": np.nan,
                       "최소": np.nan,
                       "최대": f"초과 {int(bad.sum())}일 ({bad.mean() * 100:.1f}%)"
                       if total else "―"})
    return pd.concat([tbl, pd.DataFrame(checks)], ignore_index=True)


# ----------------------------------------------------------------------
# 2) 복제 가능성
# ----------------------------------------------------------------------
def replicate(prices: pd.DataFrame, events: list,
              adv: pd.Series | None = None, aum_krw: float = 3000e8,
              participation: float = 0.10, lag: int = 1,
              cost_bp: float = 30.0) -> pd.DataFrame:
    """제약 있는 복제 포트폴리오를 굴려 tracker 레벨을 만든다.

    모델(단순하고 보수적인 한 손잡이)
      - 목표비중은 이벤트 시행일 종가 기준으로 공표되나, 추종자는 **D+lag**
        부터 체결한다(당일 종가 동시 체결 가정은 실무적으로 성립하지 않는다).
      - 하루에 종목 i 가 소화할 수 있는 금액은 ADV60_i x 참여율이다.
        필요 거래 대비 부족하면 **전 종목 동일 비율 λ 만큼만** 이동한다.
        (λ = min_i 허용_i/|Δw_i|, 1 이하). 종목별로 따로 움직이면 현금이
        생겨 자기금융이 깨지므로, 한 스칼라로 묶어 완전투자를 유지한다.
      - 체결분에 편도 회전율 x cost_bp 의 비용을 레벨에서 뺀다.
      - adv=None 이면 참여율 제약 없이 **체결 지연만** 반영한다.

    한계(고지)
      정수주·호가단위·시장충격·차입/현금 버퍼·배당 재투자 시차를 모형화하지
      않는다. 따라서 산출된 추종오차는 **하한**이다 ― 실제 추종오차는 이보다
      작아지지 않는다. 이 방향성이 보장되므로 방어 논리로 쓸 수 있다.
    """
    ev: dict = {}
    for e in sorted(events, key=lambda x: x["effective_date"]):
        ev[pd.Timestamp(e["effective_date"])] = e["target_weights"]
    rets = prices.pct_change(fill_method=None)
    dates = list(prices.index[prices.index >= min(ev)])

    target = None          # 미달성 목표비중
    exec_from = None       # 체결 개시일 (공표일 + lag 거래일)
    wt = None
    lvl = 1000.0
    rows = []
    for i, d in enumerate(dates):
        if wt is not None:                                  # 보유분 드리프트
            r = rets.loc[d].reindex(wt.index)
            if r.isna().any():
                raise ValueError(f"{d.date()} 복제 포트폴리오 수익률 결측: "
                                 f"{r.index[r.isna()].tolist()}")
            lvl *= 1.0 + float((wt * r).sum())
            wt = wt * (1.0 + r)
            wt = wt / wt.sum()

        if d in ev:                                         # 공표
            target = ev[d].copy()
            exec_from = dates[min(i + lag, len(dates) - 1)]

        traded, done = 0.0, target is None
        if target is not None and exec_from is not None and d >= exec_from:
            idx = target.index.union(wt.index) if wt is not None else target.index
            u = target.reindex(idx, fill_value=0.0)
            v = (wt.reindex(idx, fill_value=0.0) if wt is not None
                 else pd.Series(0.0, index=idx))
            delta = u - v
            if float(delta.abs().sum()) > 1e-12:
                lam = 1.0
                if adv is not None and wt is not None:
                    cap = adv.reindex(idx).fillna(0.0) * participation / aum_krw
                    moving = delta.abs() > 1e-12
                    ratio = (cap[moving] / delta.abs()[moving]).replace(
                        [np.inf, -np.inf], np.nan).dropna()
                    if len(ratio):
                        lam = float(np.clip(ratio.min(), 0.0, 1.0))
                new = v + lam * delta
                traded = 0.5 * float((new - v).abs().sum())
                new = new[new > 1e-12]
                wt = new / new.sum()
                lvl *= 1.0 - cost_bp / 1e4 * traded
                if lam >= 1.0 - 1e-12:
                    target, exec_from = None, None
            else:
                target, exec_from = None, None
            done = target is None

        rows.append({"date": d, "tracker_level": lvl, "traded": traded,
                     "target_reached": bool(done)})
    return pd.DataFrame(rows).set_index("date")


def replication_table(index_level: pd.Series, prices: pd.DataFrame,
                      events: list, adv: pd.Series | None,
                      aums: list, participation: float = 0.10,
                      lag: int = 1, cost_bp: float = 30.0) -> pd.DataFrame:
    rows = []
    for aum in aums:
        trk = replicate(prices, events, adv=adv, aum_krw=aum * 1e8,
                        participation=participation, lag=lag, cost_bp=cost_bp)
        m = tracking_metrics(index_level, trk["tracker_level"])
        rows.append({
            "AUM(억)": aum,
            "참여율": participation,
            "체결지연(D+)": lag,
            "비용(bp)": cost_bp,
            "추종오차(연율)": float(m["추종오차(연율)"]),
            "누적 추종차이": float(m["누적 추종차이"]),
            "최대 일간 절대 추종차이": float(m["최대 일간 절대 추종차이"]),
            "일간 상관계수": float(m["일간 수익률 상관계수"]),
        })
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots", required=True)
    ap.add_argument("--prices-cache", required=True)
    ap.add_argument("--adv", default=None,
                    help="ADV60 CSV (ticker, adv60_krw). 없으면 체결지연만 반영")
    ap.add_argument("--aum", default="1000,3000,10000", help="억원 단위 콤마 구분")
    ap.add_argument("--participation", type=float, default=0.10)
    ap.add_argument("--lag", type=int, default=1)
    ap.add_argument("--cost-bp", type=float, default=30.0)
    ap.add_argument("--policy", default="mid",
                    choices=("none", "narrow", "mid", "wide"))
    ap.add_argument("--out", default="out/backtest")
    a = ap.parse_args()

    import run_backtest as rb                                      # noqa: E402

    cfg = ConfigV2.with_policy(a.policy)
    snaps = rb.load_snapshots(a.snapshots)
    tickers = sorted({t for df in snaps.values() for t in df["ticker"]})
    px = rb.fetch_prices(tickers, min(snaps).strftime("%Y%m%d"),
                         as_of_today().strftime("%Y%m%d"), cache=a.prices_cache)
    px = px.dropna(axis=1, how="all")

    events, _ = build_event_schedule(px, snaps, cfg=cfg)
    bt = simulate_index(px, events, base=1000.0)
    groups_by_date = {pd.Timestamp(d): snaps[d].set_index("ticker")["group"]
                      for d in snaps}

    daily = concentration_history(px, events, groups_by_date)
    summ = concentration_summary(daily)

    adv = None
    if a.adv:
        t = pd.read_csv(a.adv, dtype={"ticker": str})
        adv = t.set_index(t["ticker"].str.zfill(6))["adv60_krw"].astype(float)

    aums = [float(x) for x in a.aum.split(",") if x.strip()]
    rep = replication_table(bt["level"], px, events, adv, aums,
                            participation=a.participation, lag=a.lag,
                            cost_bp=a.cost_bp)

    os.makedirs(a.out, exist_ok=True)
    daily.to_csv(os.path.join(a.out, "concentration_daily.csv"),
                 encoding="utf-8-sig")
    summ.to_csv(os.path.join(a.out, "concentration_summary.csv"),
                index=False, encoding="utf-8-sig")
    rep.to_csv(os.path.join(a.out, "replication_tracking.csv"),
               index=False, encoding="utf-8-sig")

    print("[집중도 ― 일별 전 거래일]\n", summ.to_string(index=False))
    print("\n[복제 가능성 ― 제약 하 추종오차]\n", rep.round(4).to_string(index=False))
    if adv is None:
        print("\n[주의] --adv 미지정 ― 참여율 제약 없이 체결지연만 반영했다. "
              "이 값은 추종오차의 하한이며 유동성 제약을 담지 않는다.")
    print(f"\n[저장] {a.out}/concentration_daily.csv · concentration_summary.csv "
          f"· replication_tracking.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
