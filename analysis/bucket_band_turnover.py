# -*- coding: utf-8 -*-
"""
bucket_band_turnover.py ― 버킷 밴드 감시(안 C)의 회전율 비용 계량
==================================================================
「40/60 버킷 규정 개정안」 5절의 미측정 항목을 채운다.

    "안 C 의 회전율 비용은 아직 재지 않았다. 밴드 폭 후보(3/5/10%p)별
     회전율 증가분은 다음 작업으로 남긴다."

무엇을 재는가
  현재 월간 점검은 **개별 종목만** 본다. 안 C 는 버킷 합계에 허용 밴드를
  두고 이탈 시 리밸런싱을 트리거하는 신규 조문이다. 이 스크립트는 밴드 폭
  b in {3, 5, 10}%p 각각에 대해 지수를 반사실(counterfactual)로 재생하고,
  **기준선 대비 늘어나는 편도 회전율**을 낸다.

기준점(reference)을 무엇으로 잡는가 ― 이 선택이 결과를 지배한다
  방법론 문언의 40% 를 기준으로 밴드를 걸면, 희소 조항이 발동한 12개 회차는
  출발부터 이탈 상태(85%/67%/64%)이므로 밴드가 매일 발동하고 회전율이
  발산한다. 그것은 안 C 의 비용이 아니라 **안 A/B 부재의 비용**이다.
  개정안 4절의 "A 또는 B 없이 C 만 하면 85%를 85%로 유지하는 감시가 된다"가
  바로 이 뜻이다.

  따라서 기본 기준점은 `reset`(직전 리셋 시점에 규칙이 실제로 실현한 앵커
  버킷 비중)이다. `--reference mandate` 로 40% 고정 기준도 낼 수 있으나,
  그 수치는 **안 B 채택을 전제하지 않은 참고치**이며 발표 인용 대상이 아니다.

경계 ― 엔진 코드를 건드리지 않는다
  확정 실행 매니페스트는 '코드 무변경'을 유효 조건으로 삼는다. 밴드는 아직
  조문이 아니므로 src/backtest 에 넣지 않고, 이 스크립트가 **이벤트 목록만**
  만들어 기존 `simulate_index` 에 먹인다. 회전율 수치는 전부 엔진이 낸다.

자기검증 2종(어긋나면 중단한다)
  1. 밴드 없는 반사실 경로가 엔진 기준선의 회전율·레벨을 재현하는가
     (같은 규칙을 두 번 구현했으므로, 일치하지 않으면 이 스크립트가 틀렸다)
  2. 밴드 = 무한대가 기준선과 동일한가 (트리거 자체의 무해성)

사용
    python analysis/bucket_band_turnover.py \
        --snapshots data/snapshots --prices-cache out/px.csv \
        --bands 3,5,10 --out out/backtest

산출
    bucket_band_turnover.csv   밴드별 회전율·이벤트 수·기준선 대비 증가분
    bucket_band_events.csv     발동 건별 원장(날짜·이탈폭·회전율)
    bucket_band_daily.csv      밴드별 일별 앵커 버킷 비중·기준점·이탈폭
"""
from __future__ import annotations

import argparse
import math
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "analysis"))

from analysis.index_calendar import as_of_today  # noqa: E402
from backtest.backtest import (annualized_turnover, build_event_schedule,  # noqa: E402
                               make_event, simulate_index)
from src.rebalance import ANCHOR, ConfigV2, monitor  # noqa: E402

TOL_SELFCHECK = 1e-12


# ----------------------------------------------------------------------
# 반사실 스케줄러 ― 정기(엔진 산출 재사용) + 캡(재계산) + 밴드(신규)
# ----------------------------------------------------------------------
def _bucket_total(w: pd.Series, groups: pd.Series, bucket: str = ANCHOR) -> float:
    g = groups.reindex(w.index)
    if g.isna().any():
        raise ValueError(f"군 미상 구성종목: {g.index[g.isna()].tolist()}")
    return float(w[g.eq(bucket)].sum())


def _restore_bucket(w: pd.Series, groups: pd.Series, ref: float) -> pd.Series:
    """버킷 합계만 기준점으로 되돌린다(군 내부 상대비중 보존).

    밴드 조문의 문언 그대로다 ― 감시 대상이 버킷 합계이므로 되돌리는 것도
    버킷 합계여야 한다. 종목별 목표비중까지 전부 재산정하면 그것은 밴드가
    아니라 임시 정기변경이고, 회전율이 과대계상된다.
    """
    g = groups.reindex(w.index)
    a = g.eq(ANCHOR)
    wa, wn = float(w[a].sum()), float(w[~a].sum())
    if wa <= 0 or wn <= 0:
        raise ValueError("한쪽 버킷이 비어 밴드 복원이 불가능하다(퇴화 구성)")
    out = w.copy()
    out[a] = w[a] * (ref / wa)
    out[~a] = w[~a] * ((1.0 - ref) / wn)
    return out / out.sum()


def replay(prices: pd.DataFrame, snaps: dict, cfg: ConfigV2,
           band: float | None, reference: str = "reset") -> tuple:
    """반사실 이벤트 목록을 만든다.

    band=None 이면 밴드 없음(기준선 재구현). 반환: (events, daily DataFrame)

    정기변경 목표비중은 엔진(`build_event_schedule`)이 낸 값을 그대로 쓴다.
    밴드는 비중만 바꾸고 **구성종목을 바꾸지 않으므로** 정기 심사 결과
    (편입·편출·군 배정)는 밴드 유무와 무관하다. 캡은 비중에 의존하므로
    반사실 경로에서 다시 계산한다.
    """
    base_events, _hist = build_event_schedule(prices, snaps, cfg=cfg)
    reasons = {e["reason"] for e in base_events}
    unsupported = reasons - {"regular", "cap"}
    if unsupported:
        raise NotImplementedError(
            f"반사실 재생이 지원하지 않는 이벤트: {sorted(unsupported)} ― "
            "수시편출·긴급편입이 있는 구간은 구성종목 경로가 갈릴 수 있으므로 "
            "밴드 비용을 이 방식으로 추정하지 않는다(엔진 확장 필요).")

    regular = {pd.Timestamp(e["effective_date"]): e["target_weights"]
               for e in base_events if e["reason"] == "regular"}
    groups_by_date = {pd.Timestamp(d): snaps[d].set_index("ticker")["group"]
                      for d in snaps}

    rets = prices.pct_change(fill_method=None)
    dates = prices.index[prices.index >= min(regular)]
    next_dates = pd.Series(dates, index=dates).shift(-1)
    is_month_end = next_dates.notna() & (next_dates.dt.month != dates.month)

    events: list = []
    daily: list = []
    w = None
    groups = None
    ref = np.nan
    pending_cap: dict = {}
    pending_band: dict = {}

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
                raise ValueError(f"{d.date()} 활성 구성종목 수익률 결측: "
                                 f"{r.index[r.isna()].tolist()}")
            w = w * (1.0 + r)
            w = w / w.sum()

        if d in regular:                                   # 정기변경
            w = regular[d].copy()
            groups = groups_by_date[d].reindex(w.index)
            events.append(make_event(d, "regular", w))
            pending_band.clear()
            book_cap(i)
            ref = (_bucket_total(w, groups) if reference == "reset"
                   else cfg.anchor_total)
        elif d in pending_cap:                             # 월간 캡 집행
            tgt = pending_cap.pop(d)
            tgt = tgt[tgt.index.intersection(w.index)]
            w = tgt / tgt.sum()
            events.append(make_event(d, "cap", w))
            pending_band.clear()                           # 구성비중 갱신 ― 예약 무효
        elif d in pending_band:                            # 밴드 복원 집행
            pending_band.pop(d)
            w = _restore_bucket(w, groups, ref)
            events.append(make_event(d, "band", w))
            book_cap(i)                                    # 복원이 캡을 건드릴 수 있다

        cur = _bucket_total(w, groups) if w is not None else np.nan
        dev = cur - ref if w is not None else np.nan
        daily.append({"date": d, "anchor_weight": cur, "reference": ref,
                      "deviation": dev, "n": 0 if w is None else len(w)})

        if w is None:
            continue
        if bool(is_month_end.loc[d]):
            book_cap(i)
        if band is not None and not pending_cap and not pending_band \
                and abs(dev) > band + 1e-12 and i + 2 < len(dates):
            pending_band[dates[i + 2]] = True

    return events, pd.DataFrame(daily).set_index("date")


# ----------------------------------------------------------------------
# 집계
# ----------------------------------------------------------------------
def measure(prices: pd.DataFrame, snaps: dict, cfg: ConfigV2,
            bands: list, reference: str = "reset") -> dict:
    base_events, _ = build_event_schedule(prices, snaps, cfg=cfg)
    engine = simulate_index(prices, base_events, base=1000.0)

    # 자기검증 1 ― 반사실 재구현이 엔진 기준선을 재현하는가
    cf_events, cf_daily = replay(prices, snaps, cfg, band=None,
                                 reference=reference)
    cf = simulate_index(prices, cf_events, base=1000.0)
    d_tno = float((cf["turnover"] - engine["turnover"]).abs().max())
    d_lvl = float((cf["level"] / engine["level"] - 1.0).abs().max())
    if d_tno > TOL_SELFCHECK or d_lvl > TOL_SELFCHECK:
        sys.exit(f"[FAIL] 반사실 재구현이 엔진 기준선과 다르다 ― "
                 f"회전율 최대차 {d_tno:.3e} · 레벨 상대차 {d_lvl:.3e}. "
                 "이 스크립트를 신뢰할 수 없으므로 중단한다.")

    # 자기검증 2 ― 밴드 무한대는 기준선과 동일해야 한다
    inf_events, _ = replay(prices, snaps, cfg, band=math.inf,
                           reference=reference)
    inf = simulate_index(prices, inf_events, base=1000.0)
    if float((inf["turnover"] - engine["turnover"]).abs().max()) > TOL_SELFCHECK:
        sys.exit("[FAIL] 밴드=무한대가 기준선과 다르다 ― 트리거 로직 결함")

    rows = [_summarize("기준선(밴드 없음)", np.nan, engine, engine)]
    ledger: list = []
    dailies = {"기준선": cf_daily["deviation"]}
    for b in bands:
        ev, dly = replay(prices, snaps, cfg, band=b, reference=reference)
        bt = simulate_index(prices, ev, base=1000.0)
        rows.append(_summarize(f"밴드 +-{b * 100:.0f}%p", b, bt, engine))
        dailies[f"band_{b * 100:.0f}"] = dly["deviation"]
        log = bt.attrs["event_log"]
        for _, r in log[log["reason"] == "band"].iterrows():
            ledger.append({"band_pp": b * 100,
                           "date": r["effective_date"],
                           "one_way_turnover": r["one_way_turnover"],
                           "deviation_at_trigger": float(
                               dly["deviation"].loc[:r["effective_date"]].iloc[-3])
                           if len(dly.loc[:r["effective_date"]]) >= 3 else np.nan})

    table = pd.DataFrame(rows)
    mono = table.iloc[1:]["회전율(편도,합)"].to_numpy()
    warn = None
    if len(mono) > 1 and not np.all(np.diff(mono) <= 1e-9):
        warn = ("[주의] 밴드가 넓어지는데 회전율이 늘어난 구간이 있다 ― "
                "경로 의존(캡과의 상호작용) 가능성. 밴드별 원장을 확인할 것.")
    return {"table": table, "ledger": pd.DataFrame(ledger),
            "daily": pd.DataFrame(dailies), "warning": warn,
            "selfcheck": {"turnover_max_diff": d_tno, "level_max_rel_diff": d_lvl}}


def _summarize(label: str, band: float, bt: pd.DataFrame,
               base: pd.DataFrame) -> dict:
    log = bt.attrs["event_log"]
    by = log.groupby("reason")["one_way_turnover"].sum()
    total = float(bt["turnover"].sum())
    base_total = float(base["turnover"].sum())
    return {
        "정책": label,
        "밴드(%p)": np.nan if band is None or not np.isfinite(band) else band * 100,
        "밴드 발동": int((log["reason"] == "band").sum()),
        "캡 발동": int((log["reason"] == "cap").sum()),
        "회전율(편도,합)": total,
        "연율화회전율": annualized_turnover(bt),
        "밴드 회전율": float(by.get("band", 0.0)),
        "캡 회전율": float(by.get("cap", 0.0)),
        "정기 회전율": float(by.get("regular", 0.0)),
        "기준선 대비 증가(%p)": (total - base_total) * 100,
        "기준선 대비 증가율": (total / base_total - 1.0) if base_total else np.nan,
    }


# ----------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots", required=True, help="PIT 스냅샷 디렉터리")
    ap.add_argument("--prices-cache", required=True, help="가격 캐시 CSV")
    ap.add_argument("--bands", default="3,5,10",
                    help="밴드 폭(%%p) 콤마 구분. 기본 3,5,10")
    ap.add_argument("--reference", default="reset", choices=("reset", "mandate"),
                    help="밴드 기준점. reset=직전 리셋 실현치(기본) · "
                         "mandate=조문 40%% 고정(안 B 미채택 시 참고용)")
    ap.add_argument("--policy", default="mid",
                    choices=("none", "narrow", "mid", "wide"))
    ap.add_argument("--out", default="out/backtest")
    a = ap.parse_args()

    import run_backtest as rb                              # noqa: E402

    cfg = ConfigV2.with_policy(a.policy)
    snaps = rb.load_snapshots(a.snapshots)
    tickers = sorted({t for df in snaps.values() for t in df["ticker"]})
    start = min(snaps).strftime("%Y%m%d")
    end = as_of_today().strftime("%Y%m%d")          # 마지막 심사일이 아니라 as_of
    px = rb.fetch_prices(tickers, start, end, cache=a.prices_cache)
    px = px.dropna(axis=1, how="all")

    bands = [float(x) / 100.0 for x in a.bands.split(",") if x.strip()]
    res = measure(px, snaps, cfg, bands, reference=a.reference)

    os.makedirs(a.out, exist_ok=True)
    res["table"].to_csv(os.path.join(a.out, "bucket_band_turnover.csv"),
                        index=False, encoding="utf-8-sig")
    res["ledger"].to_csv(os.path.join(a.out, "bucket_band_events.csv"),
                         index=False, encoding="utf-8-sig")
    res["daily"].to_csv(os.path.join(a.out, "bucket_band_daily.csv"),
                        encoding="utf-8-sig")

    sc = res["selfcheck"]
    print(f"[자기검증] 반사실 재구현 == 엔진 기준선 "
          f"(회전율 {sc['turnover_max_diff']:.2e} · 레벨 "
          f"{sc['level_max_rel_diff']:.2e})")
    print(f"[기준점] {a.reference}"
          + (" (조문 40% 고정 ― 안 B 미채택 시 참고치, 발표 인용 금지)"
             if a.reference == "mandate" else " (직전 리셋 실현치)"))
    print("\n[밴드 폭별 회전율 비용]\n",
          res["table"].round(4).to_string(index=False))
    if res["warning"]:
        print("\n" + res["warning"])
    print(f"\n[저장] {a.out}/bucket_band_turnover.csv · bucket_band_events.csv "
          f"· bucket_band_daily.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
