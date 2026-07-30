# -*- coding: utf-8 -*-
"""
composition_timeline.py - 구성 종목 수 이력과 하한 미달 구간 계량
==================================================================
왜 필요한가
-----------
v2에서 고정 정원(핵심7·위성3)을 폐지하면서 종목 수는 규칙이 정하게 됐다.
그 결과 실제 백테스트 구성은 3~7종목이고, 하한(5종목)을 밑도는 구간이 표본의
대부분을 차지한다. 이 사실은 change_history.csv 의 under_min_start /
under_min_resolved 두 줄에만 남아 있어, 표를 훑는 것만으로는 **얼마나 오래**
미달이었는지 알 수 없다. 발표에서 "몇 종목짜리 지수냐"는 질문은 거의 확실히
나오고, 그때 "평균 3.7종목"과 "표본의 82%가 하한 미달"은 전혀 다른 무게의
답이다. 후자를 먼저 말해야 방어가 아니라 보고가 된다.

이 스크립트는 정기변경 시점별 구성(군별 내역 포함)과 거래일 기준 체류 기간을
같이 낸다 - 심사 횟수로 세면 13회 중 10회지만, 거래일로 세면 81.9%다.
같은 사실을 어느 단위로 세느냐에 따라 인상이 달라지므로 둘 다 싣는다.

사용
----
    python analysis/composition_timeline.py --snapshots data/snapshots \
        --index-level out/backtest/index_level.csv --out out/backtest
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys
import warnings

import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from src.rebalance import ANCHOR, CORE, SAT, ConfigV2, select_v2  # noqa: E402

SNAP_RE = re.compile(r"snapshot_(\d{8})\.csv$")


def load_snapshots(d: str) -> dict:
    out = {}
    for p in sorted(glob.glob(os.path.join(d, "snapshot_*.csv"))):
        m = SNAP_RE.search(os.path.basename(p))
        if not m:
            continue
        df = pd.read_csv(p, dtype={"ticker": str})
        df["ticker"] = df["ticker"].str.strip().str.zfill(6)
        for c in ("exposure", "mem_ratio", "float_mcap"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["eligible"] = (df["eligible"].astype(str).str.strip().str.lower()
                          .isin({"true", "1", "y", "yes", "참"}))
        out[pd.Timestamp(m.group(1))] = df
    if not out:
        raise SystemExit(f"[중단] 스냅샷을 찾지 못했습니다: {d}")
    return out


def timeline(snaps: dict, cfg: ConfigV2) -> pd.DataFrame:
    """정기변경 시점별 구성 - 군별 내역·하한 미달 여부·후보 규모."""
    rows, prev = [], set()
    for d in sorted(snaps):
        s = snaps[d]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            sel = select_v2(s, prev, cfg)
        m = sel["members"]
        cur = set(m["ticker"])
        rows.append({
            "심사일": d.date(),
            "후보(원장)": int(len(s)),
            "적격(사전스크린 통과)": int(s["eligible"].sum()),
            "구성": sel["n"],
            "앵커": int((m["group"] == ANCHOR).sum()),
            "핵심": int((m["group"] == CORE).sum()),
            "위성": int((m["group"] == SAT).sum()),
            "편입": ",".join(sorted(cur - prev)),
            "편출": ",".join(sorted(prev - cur)),
            f"하한({cfg.min_constituents}) 미달": sel["n"] < cfg.min_constituents,
        })
        prev = cur
    return pd.DataFrame(rows)


def dwell(tl: pd.DataFrame, index_level: str, cfg: ConfigV2) -> pd.DataFrame:
    """거래일 기준 체류 기간. 심사 횟수와 거래일은 전혀 다른 그림을 준다.

    index_level.csv 가 없으면(백테스트 미실행) 건너뛴다 - 이 스크립트는
    스냅샷만으로도 1번 표를 낼 수 있어야 한다.
    """
    if not os.path.exists(index_level):
        print(f"[건너뜀] 지수 계열 없음: {index_level} - 거래일 집계 생략")
        return pd.DataFrame()
    lv = pd.read_csv(index_level, parse_dates=["날짜"]).set_index("날짜")["level"]
    n = pd.Series(index=lv.index, dtype="float64")
    for _, r in tl.iterrows():
        n[lv.index >= pd.Timestamp(r["심사일"])] = r["구성"]
    n = n.dropna()
    tot = len(n)
    out = (n.value_counts().sort_index().rename("거래일").to_frame()
           .assign(**{"비중(%)": lambda x: (x["거래일"] / tot * 100).round(1)}))
    out.index.name = "구성 종목수"
    under = n < cfg.min_constituents
    print(f"\n== 2. 거래일 기준 체류 ({tot}거래일) ==")
    print(out.to_string())
    print(f"\n하한({cfg.min_constituents}) 미달 거래일: {int(under.sum())}일 "
          f"({under.mean() * 100:.1f}%)")
    if under.any():
        print(f"미달 구간: {n.index[under][0].date()} ~ {n.index[under][-1].date()}")
        print("  -> 심사 횟수로 세면 인상이 약해진다. 발표에서는 거래일 비중을"
              "\n     먼저 말하는 편이 정직하다(질문받고 나서 말하면 은폐로 읽힌다).")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="구성 종목 수 이력·하한 미달 계량")
    ap.add_argument("--snapshots", default="data/snapshots")
    ap.add_argument("--index-level", default="out/backtest/index_level.csv")
    ap.add_argument("--out", default="out/backtest")
    a = ap.parse_args()

    cfg = ConfigV2()
    snaps = load_snapshots(a.snapshots)
    os.makedirs(a.out, exist_ok=True)
    pd.set_option("display.width", 220)

    tl = timeline(snaps, cfg)
    print(f"== 1. 정기변경 시점별 구성 (심사 {len(tl)}회) ==")
    print(tl.to_string(index=False))
    tl.to_csv(os.path.join(a.out, "composition_timeline.csv"),
              index=False, encoding="utf-8-sig")

    dw = dwell(tl, a.index_level, cfg)
    if len(dw):
        dw.to_csv(os.path.join(a.out, "composition_dwell.csv"),
                  encoding="utf-8-sig")

    print(f"\n저장: {a.out}/ (composition_timeline.csv · composition_dwell.csv)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
