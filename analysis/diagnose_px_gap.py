# -*- coding: utf-8 -*-
"""
diagnose_px_gap.py ― 두 가격 패널의 불일치를 원인별로 분류한다
================================================================
`fetch_prices_kis.py --compare` 가 FAIL 을 냈을 때, 그 1,000여 셀이
**무엇 때문에** 어긋났는지 가른다. 원인이 다르면 처리가 완전히 다르다.

  분할보정 차이 : 한쪽만 액면분할·무상증자를 소급 반영했다. 비율이 정확히
                  2·0.5·5·0.2 같은 단순 배수이고, 어떤 날짜 이전 구간에만
                  걸린다. **데이터가 틀린 것**이므로 원천을 확정해야 한다.
  반올림 차이   : 수정주가를 원 단위로 맞추는 시점·방식이 달라 1e-4 수준의
                  잡음이 흩어진다. 값이 틀린 게 아니라 표시 정밀도 문제이며,
                  허용오차를 어디로 둘지의 판단이다.
  기타          : 위 둘로 설명되지 않는다. 사람이 봐야 한다.

판정을 어떻게 하는가 ― 단절점 탐색
  분할보정 차이는 "어느 날 이전은 배수 k, 이후는 1.0" 이라는 계단 모양을
  만든다. 그래서 비율 시계열에서 **1.0 로 바뀌는 첫 날**을 찾고, 그 앞이
  단일 배수로 일정한지 확인한다. 일정하면 분할보정 차이로 확정하고 그
  날짜를 보고한다 ― 그 날짜가 실제 분할·증자일과 맞는지는 사람이 KRX
  공시로 대조할 것(이 스크립트는 날짜를 제시할 뿐 단정하지 않는다).

경계
  어느 패널이 옳은지 **판정하지 않는다.** 두 패널의 차이를 성격별로
  정리해 사람이 원천을 확정할 수 있게 하는 것이 범위다. 자동으로 한쪽을
  채택하면 조용히 틀린 데이터가 확정된다.

사용
    python analysis\\diagnose_px_gap.py --new out\\px_kis.csv --old out\\px.csv
    python analysis\\diagnose_px_gap.py --new out\\px_kis.csv --old out\\px.csv ^
        --tol 1e-6 --rounding-tol 1e-3 --out out\\px_gap_diagnosis.csv
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

#: 분할·증자에서 실제로 나오는 배수. 이 목록 밖의 배수는 '기타'로 남긴다
#: ― 억지로 맞추면 원인 오진이 된다.
SPLIT_RATIOS = [0.1, 0.2, 0.25, 1 / 3, 0.5, 2 / 3, 1.5, 2.0, 3.0, 4.0, 5.0, 10.0]
SPLIT_REL_TOL = 1e-3           # 배수 판정 허용 상대오차


def _load(path: str, what: str) -> pd.DataFrame:
    if not os.path.exists(path):
        sys.exit(f"[FAIL] {what} 패널이 없다: {path}")
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df.columns = [str(c).zfill(6) for c in df.columns]
    return df.sort_index()


def _named_ratio(k: float) -> str | None:
    for s in SPLIT_RATIOS:
        if abs(k / s - 1.0) <= SPLIT_REL_TOL:
            return f"{s:.6g}"
    return None


def classify_ticker(new: pd.Series, old: pd.Series, tol: float,
                    rounding_tol: float) -> dict:
    """한 종목의 불일치 성격을 가른다."""
    both = new.notna() & old.notna()
    n_ok = int(both.sum())
    if n_ok == 0:
        return {"판정": "관측 겹침 없음", "불일치 셀": 0}

    r = (new[both] / old[both]).astype(float)
    rel = (r - 1.0).abs()
    bad = rel > tol
    n_bad = int(bad.sum())
    if n_bad == 0:
        return {"판정": "일치", "불일치 셀": 0, "유효 셀": n_ok}

    row = {"유효 셀": n_ok, "불일치 셀": n_bad,
           "최대 상대오차": float(rel.max()),
           "불일치 시작": str(r[bad].index.min().date()),
           "불일치 끝": str(r[bad].index.max().date())}

    # (1) 전부 미세하면 반올림 차이
    if float(rel.max()) <= rounding_tol:
        row["판정"] = "반올림 차이"
        row["비고"] = (f"최대 {rel.max():.2e} <= {rounding_tol:g} · "
                     "값이 아니라 표시 정밀도 문제로 보인다")
        return row

    # (2) 계단 모양인가 ― 1.0 으로 복귀하는 단절점 탐색
    ok_mask = rel <= rounding_tol
    if ok_mask.any() and bad.any():
        first_ok = ok_mask[ok_mask].index.min()
        head = r[r.index < first_ok]
        tail_bad = bad[bad.index >= first_ok]
        if len(head) and not tail_bad.any():
            k = float(head.median())
            named = _named_ratio(k)
            spread = float((head / k - 1.0).abs().max())
            if named and spread <= SPLIT_REL_TOL:
                row["판정"] = "분할보정 차이"
                row["배수"] = named
                row["단절점"] = str(first_ok.date())
                row["비고"] = (
                    f"{first_ok.date()} 이전 전 구간이 정확히 x{named} · "
                    f"구간 내 편차 {spread:.1e} · 실제 분할·증자일과 대조할 것")
                return row
            row["판정"] = "계단형(배수 불명)"
            row["배수"] = f"{k:.6g}"
            row["단절점"] = str(first_ok.date())
            row["비고"] = (f"단절점은 잡혔으나 배수가 단순 분할비가 아니다"
                         f"(구간 편차 {spread:.1e}) ― 사람이 확인")
            return row

    # (3) 단일 배수로 전 구간이 어긋나면 (겹침 구간 전체가 분할 전)
    k = float(r.median())
    named = _named_ratio(k)
    if named and float((r / k - 1.0).abs().max()) <= SPLIT_REL_TOL:
        row["판정"] = "분할보정 차이(전 구간)"
        row["배수"] = named
        row["비고"] = "겹침 구간 전체가 단일 배수 ― 분할일이 구간 밖일 수 있다"
        return row

    row["판정"] = "기타"
    row["비고"] = (f"중앙 배수 {k:.6g} · 최대 {rel.max():.2e} ― "
                 "단절점·단일배수 모두 아님. 원자료를 직접 볼 것")
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--new", required=True, help="신 패널 (예: out/px_kis.csv)")
    ap.add_argument("--old", required=True, help="구 패널 (예: out/px.csv)")
    ap.add_argument("--tol", type=float, default=1e-6, help="불일치 판정 기준")
    ap.add_argument("--rounding-tol", type=float, default=1e-3,
                    help="이 이하만 흩어져 있으면 반올림 차이로 본다")
    ap.add_argument("--out", default=None, help="분류 결과 CSV")
    ap.add_argument("--detail", default=None,
                    help="특정 종목의 셀별 상세를 저장 (6자리 티커)")
    a = ap.parse_args()

    new = _load(a.new, "신")
    old = _load(a.old, "구")
    tickers = sorted(set(new.columns) & set(old.columns))
    only_new = sorted(set(new.columns) - set(old.columns))
    only_old = sorted(set(old.columns) - set(new.columns))
    days = new.index.intersection(old.index)
    if not tickers or len(days) == 0:
        sys.exit("[FAIL] 겹치는 종목·날짜가 없다")

    print(f"[대조] 겹침 {len(days)}일 x {len(tickers)}종목")
    if only_new:
        print(f"[주의] 신 패널에만: {only_new}")
    if only_old:
        print(f"[주의] 구 패널에만: {only_old}")

    rows = []
    for t in tickers:
        r = classify_ticker(new.loc[days, t], old.loc[days, t],
                            a.tol, a.rounding_tol)
        r["ticker"] = t
        rows.append(r)
    df = pd.DataFrame(rows).set_index("ticker")

    bad = df[df["판정"] != "일치"].copy()
    order = ["분할보정 차이", "분할보정 차이(전 구간)", "계단형(배수 불명)",
             "기타", "반올림 차이", "관측 겹침 없음"]
    bad["_o"] = bad["판정"].map({k: i for i, k in enumerate(order)}).fillna(9)
    bad = bad.sort_values(["_o", "최대 상대오차"], ascending=[True, False])
    bad = bad.drop(columns="_o")

    print(f"\n[요약] 일치 {int((df['판정'] == '일치').sum())}종목 · "
          f"불일치 {len(bad)}종목")
    print(df["판정"].value_counts().to_string())

    if len(bad):
        pd.set_option("display.width", 220)
        pd.set_option("display.max_colwidth", 70)
        cols = [c for c in ["판정", "배수", "단절점", "불일치 셀",
                            "최대 상대오차", "불일치 시작", "불일치 끝", "비고"]
                if c in bad.columns]
        print("\n[불일치 상세]\n", bad[cols].to_string())

    if a.out:
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        df.to_csv(a.out, encoding="utf-8-sig")
        print(f"\n[저장] {a.out}")

    if a.detail:
        t = str(a.detail).zfill(6)
        if t not in tickers:
            sys.exit(f"[FAIL] {t} 는 겹침 종목이 아니다")
        d = pd.DataFrame({"신": new.loc[days, t], "구": old.loc[days, t]})
        d["배수"] = d["신"] / d["구"]
        p = os.path.join(os.path.dirname(a.out or "."), f"px_gap_{t}.csv")
        d.to_csv(p, encoding="utf-8-sig")
        print(f"[저장] {t} 셀별 상세 -> {p}")
        chg = d["배수"].round(6).diff().abs() > 1e-9
        if chg.any():
            print(f"[{t}] 배수가 바뀌는 날: "
                  f"{[str(x.date()) for x in d.index[chg][:10]]}")

    print("\n[경계] 이 스크립트는 어느 패널이 옳은지 판정하지 않는다. "
          "분할보정 차이로 분류된 종목은 KRX 공시로 실제 분할·증자일을 "
          "확인하고 원천을 확정할 것.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
