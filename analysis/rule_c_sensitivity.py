# -*- coding: utf-8 -*-
"""
rule_c_sensitivity.py ― 규칙 C(위성군 3요건)의 지수 성과 기여도
==================================================================
"선택편향을 고려하지 않았다"는 지적에 크기로 답하되, **무엇이 실제로
구속하고 있는지**를 정확히 짚는다.

먼저 오해를 하나 걷어낸다
  스냅샷의 탈락사유 문자열 `공정/위원회 미확인` 은 **판정이 수행되지
  않았다는 뜻이 아니다.** 해당 행의 `judgment_status` 는 전부 `FINAL`
  이며, `process_confirmed=False` 는 **판정 결과 미충족**을 뜻한다.
  테크윙이 그 증거다 ― FY2019~2023 은 False, HBM 공정에 진입한 FY2024
  부터 True 로 바뀐다. PIT 가 제대로 작동한 흔적이지 판정 누락이 아니다.

  (문자열이 오해를 부르므로 `build_pit_snapshots.py` 의 사유 표기를
   `규칙 C 미충족`으로 바꿀 것을 권고한다 ― 별도 안건.)

그러면 무엇을 재는가 ― 규칙 C 의 성과 기여도
  규칙 C 는 세 요건의 **결합**이다.

      (1) 메모리향 매출 비중 70% 이상
      (2) HBM 고유공정(TSV·하이브리드본딩 등) 귀속 매출을 문서로 확인
      (3) 지수위원회 확인 절차 통과

  (1)은 재무 수치라 재현이 쉽고, (2)(3)은 문서 판정이다. 이 스크립트는
  **(2)(3)을 껐을 때 지수가 어떻게 달라지는가**를 잰다. 즉 정량 요건만으로
  위성을 편입하는 대안 지수와 비교한다.

  이는 편향의 추정치가 아니라 **규칙 C 가 성과에 얼마나 기여하는가**의
  측정이다. 기여도가 클수록 그 규칙의 조문·근거가 중요해진다.

왜 이 측정이 필요한가
  방법론 조문(규칙 3)은 위성군을 "HBM 귀속 매출 분리가 어려운 전공정
  장비·소재 기업"이라는 **정성 서술**로만 적고 있다. 3요건은 코드
  (`src/selection.py`)와 판정원장에만 있다. 성과 기여도가 크다면 문서화
  되지 않은 규칙이 성과의 상당 부분을 만들고 있다는 뜻이고, 그것은 조문
  신설로 닫아야 할 결함이다.

경계
  엔진을 수정하지 않는다. 스냅샷의 `eligible`·`group` 만 시나리오대로
  바꿔 기존 `build_event_schedule` -> `simulate_index` 에 넣는다.
  이 스크립트는 조문을 바꾸지 않으며 진단 수치만 만든다.

사용
    python analysis/rule_c_sensitivity.py \\
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
                               build_event_schedule, cagr, max_drawdown,
                               simulate_index)
from src.rebalance import SAT, ConfigV2  # noqa: E402

RULE_C_UNMET = "공정/위원회 미확인"      # 현행 표기(오해 소지 - 본문 참조)
SAT_MEM_TH = 0.70


def rule_c_report(snaps: dict, ledger_path: str) -> pd.DataFrame:
    """심사시점별 규칙 C 요건 통과 현황. (2)(3) 이 구속하는 크기를 보인다."""
    led = pd.read_csv(ledger_path, dtype={"ticker": str})
    led["ticker"] = led["ticker"].str.strip().str.zfill(6)
    fin = set(led.loc[led["judgment_status"].astype(str).eq("FINAL"), "ticker"])
    rows = []
    for d in sorted(snaps):
        s = snaps[d]
        mem = pd.to_numeric(s["mem_ratio"], errors="coerce")
        hi = s[mem >= SAT_MEM_TH]
        rsn = hi.get("탈락사유", pd.Series("", index=hi.index)).fillna("").astype(str)
        unmet = rsn.str.strip().eq(RULE_C_UNMET)
        rows.append({
            "심사시점": str(pd.Timestamp(d).date()),
            "요건(1) 통과": len(hi),
            "요건(2)(3) 통과": int(hi["eligible"].astype(bool).sum()),
            "요건(2)(3) 미충족": int(unmet.sum()),
            "판정 FINAL 비율": (f"{sum(t in fin for t in hi['ticker'])}/{len(hi)}"
                           if len(hi) else "0/0"),
        })
    return pd.DataFrame(rows)


def relax_rule_c(snaps: dict) -> dict:
    """요건 (2)(3) 을 끈 스냅샷 사본 ― 정량 요건(1)만으로 위성 편입."""
    out = {}
    for d, s in snaps.items():
        t = s.copy()
        mem = pd.to_numeric(t["mem_ratio"], errors="coerce")
        rsn = t.get("탈락사유", pd.Series("", index=t.index)).fillna("").astype(str)
        flip = (mem >= SAT_MEM_TH) & (~t["eligible"].astype(bool)) \
            & rsn.str.strip().eq(RULE_C_UNMET)
        t.loc[flip, "eligible"] = True
        t.loc[flip, "group"] = SAT
        out[d] = t
    return out


def summarize(label, bt, hist, base):
    lv = bt["level"]
    md = max_drawdown(lv)
    n = pd.to_numeric(hist.get("n", pd.Series(dtype=float)),
                      errors="coerce").dropna()
    row = {"시나리오": label, "CAGR": cagr(lv), "연변동성": ann_vol(lv),
           "MDD": md["mdd"], "연율화회전율": annualized_turnover(bt),
           "정기변경 평균 종목수": float(n.mean()) if len(n) else np.nan,
           "최종레벨": float(lv.iloc[-1])}
    row["CAGR 차이"] = (row["CAGR"] - cagr(base["level"])) if base is not None else 0.0
    row["최종레벨 차이"] = (float(lv.iloc[-1] / base["level"].iloc[-1] - 1.0)
                     if base is not None else 0.0)
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots", required=True)
    ap.add_argument("--prices-cache", required=True)
    ap.add_argument("--policy", default="mid",
                    choices=("none", "narrow", "mid", "wide"))
    ap.add_argument("--out", default="out/backtest")
    a = ap.parse_args()

    import run_backtest as rb                                   # noqa: E402
    cfg = ConfigV2.with_policy(a.policy)
    snaps = rb.load_snapshots(a.snapshots)
    tickers = sorted({t for df in snaps.values() for t in df["ticker"]})
    px = rb.fetch_prices(tickers, min(snaps).strftime("%Y%m%d"),
                         as_of_today().strftime("%Y%m%d"), cache=a.prices_cache)
    px = px.dropna(axis=1, how="all")

    rep = rule_c_report(snaps, os.path.join(HERE, "data", "verdict_ledger.csv"))
    print("[규칙 C 요건별 통과 현황]\n", rep.to_string(index=False))

    ev0, h0 = build_event_schedule(px, snaps, cfg=cfg)
    bt0 = simulate_index(px, ev0, base=1000.0)
    ev1, h1 = build_event_schedule(px, relax_rule_c(snaps), cfg=cfg)
    bt1 = simulate_index(px, ev1, base=1000.0)

    tbl = pd.DataFrame([summarize("현행 (규칙 C 3요건 전부)", bt0, h0, None),
                        summarize("대안 (요건 1만 - 공정·위원회 해제)", bt1, h1, bt0)])
    os.makedirs(a.out, exist_ok=True)
    rep.to_csv(os.path.join(a.out, "rule_c_requirements.csv"),
               index=False, encoding="utf-8-sig")
    tbl.to_csv(os.path.join(a.out, "rule_c_sensitivity.csv"),
               index=False, encoding="utf-8-sig")

    print("\n[규칙 C 기여도]\n", tbl.round(4).to_string(index=False))
    gap = float(tbl.iloc[1]["CAGR 차이"])
    print(f"\n[해석] 요건 (2)(3)(공정 문서확인·위원회 확인)을 해제하면 CAGR 이 "
          f"{gap:+.2%}p 변한다. 이는 편향의 추정치가 아니라 **규칙 C 가 성과에 "
          f"기여하는 크기**다.")
    print("[함의] 방법론 조문(규칙 3)은 위성군을 정성 서술로만 적고 있고 "
          "3요건은 코드·판정원장에만 있다. 기여도가 이 정도면 조문 신설이 "
          "필요하다 ― 문서화되지 않은 규칙이 성과를 만들고 있다.")
    print(f"[저장] {a.out}/rule_c_sensitivity.csv · rule_c_requirements.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
