# -*- coding: utf-8 -*-
"""
audit_dashboard_numbers.py - 폐기된 구 대시보드 수치를 엔진으로 검산한다
========================================================================
이 파일은 현재 app.py가 아니라 이전 하드코딩 화면의 결함을 보존 재현한다.
대시보드는 설득력이 매우 높은 매체다. 그래서 **틀린 수치를 실으면 틀린 채로
설득된다.** 화면에 뜬 값이 우리 엔진·우리 조문에서 재현되는지 기계로 확인한다.

검산 항목
  A. 목표 비중 재현   : 화면의 유동시총 -> weighting.allocate -> 화면의 비중?
  B. 조문 상한 준수   : core 18% / satellite 15% / anchor 25% / sat 합계 18%
  C. 누적수익률 <-> CAGR 정합
  D. 화면 간 종목 수 모순 (편출입 이력 vs PIT 스냅샷 조회기)
  E. 편출입 건수의 출처 (합성 데모와 일치하는가)

실행: python analysis/audit_dashboard_numbers.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from analysis.index_calendar import as_of_today  # noqa: E402
from src import weighting  # noqa: E402
from src.rebalance import ConfigV2  # noqa: E402

CFG = ConfigV2()
KR = {"anchor": "앵커", "core": "핵심", "satellite": "위성"}

# ── 화면(2026-06-15 심사 스냅샷)에 표시된 값 그대로 옮김 ────────────────────
SHOWN_2026 = pd.DataFrame([
    ("005930", "삼성전자",   "anchor",    285.00, 0.2400),
    ("000660", "SK하이닉스", "anchor",    112.00, 0.1600),
    ("042700", "한미반도체", "core",        7.30, 0.2932),
    ("003160", "디아이",     "core",        1.80, 0.1124),
    ("095340", "ISC",        "core",        1.20, 0.0845),
    ("067310", "하나마이크론", "core",      0.95, 0.0621),
    ("098120", "테크윙",     "satellite",   0.72, 0.0478),
], columns=["코드", "종목명", "group", "유동시총(조)", "화면 비중"])

SHOWN_2020 = pd.DataFrame([
    ("005930", "삼성전자",   "anchor",    180.00, 0.2400),
    ("000660", "SK하이닉스", "anchor",     48.00, 0.1600),
    ("042700", "한미반도체", "core",        0.73, 0.3450),
    ("003160", "디아이",     "core",        0.31, 0.1420),
    ("067310", "하나마이크론", "core",      0.22, 0.1130),
], columns=["코드", "종목명", "group", "유동시총(조)", "화면 비중"])

SHOWN_2023 = pd.DataFrame([
    ("005930", "삼성전자",   "anchor",    220.00, 0.2400),
    ("000660", "SK하이닉스", "anchor",     65.00, 0.1600),
    ("042700", "한미반도체", "core",        2.10, 0.3210),
    ("003160", "디아이",     "core",        0.80, 0.1220),
    ("095340", "ISC",        "core",        0.70, 0.0980),
    ("067310", "하나마이크론", "core",      0.50, 0.0590),
], columns=["코드", "종목명", "group", "유동시총(조)", "화면 비중"])


def check_weights(df: pd.DataFrame, label: str) -> pd.DataFrame:
    """A·B - 엔진 재현 + 조문 상한 준수."""
    groups = df["group"].map(KR).to_numpy()
    fmc = df["유동시총(조)"].to_numpy(float)
    w_engine = weighting.allocate(groups, fmc)
    out = df.copy()
    out["엔진 재현"] = np.round(w_engine, 4)
    out["차이(%p)"] = np.round((w_engine - df["화면 비중"]) * 100, 2)
    cap = {"anchor": CFG.anchor_ind_cap, "core": CFG.core_ind_cap,
           "satellite": CFG.sat_ind_cap}
    out["개별 상한"] = df["group"].map(cap)
    out["상한 위반"] = np.where(df["화면 비중"] > out["개별 상한"] + 1e-9,
                             "[FAIL] 초과", "")
    print(f"\n{'=' * 78}\n[A·B] {label} - 목표 비중 재현 및 상한 준수\n{'=' * 78}")
    print(out[["코드", "종목명", "group", "유동시총(조)", "화면 비중",
               "엔진 재현", "차이(%p)", "개별 상한", "상한 위반"]]
          .to_string(index=False))
    worst = float(np.abs(w_engine - df["화면 비중"].to_numpy()).max())
    print(f"\n  최대 재현 오차 {worst * 100:.2f}%p "
          f"(교차검증 리포트 허용치 0.005%p)")
    print(f"  화면 비중 합계 {df['화면 비중'].sum():.4f} · "
          f"엔진 합계 {w_engine.sum():.4f}")
    viol = out[out["상한 위반"] != ""]
    if len(viol):
        for _, r in viol.iterrows():
            print(f"  [FAIL] {r['종목명']} {r['화면 비중']:.2%} > "
                  f"{r['group']} 개별 상한 {r['개별 상한']:.0%} "
                  f"- 방법론 2.3.3 위반")
    anc = df.loc[df["group"] == "anchor", "화면 비중"].sum()
    print(f"  앵커 합계 {anc:.2%} (조문 40%) · "
          f"비앵커 합계 {1 - anc:.2%} (조문 60%)")
    return out


def check_return_consistency() -> None:
    """C - 누적수익률과 CAGR이 서로 맞는가."""
    print(f"\n{'=' * 78}\n[C] 누적수익률 ↔ CAGR 정합\n{'=' * 78}")
    rows = []
    for label, cum, cagr_shown in [
            ("카드 배지 +218.0%", 2.180, None),
            ("카드 본문 +216.2% (3,161.9pt)", 2.162, None)]:
        implied = (1 + cum) ** (1 / 6) - 1
        rows.append({"화면 표기": label, "누적": f"{cum:.1%}",
                     "함의 CAGR(6년)": f"{implied:.2%}"})
    for label, cagr in [("성과표 CAGR 0bp 22.4%", 0.224),
                        ("성과표 CAGR 30bp 21.6%", 0.216)]:
        implied_cum = (1 + cagr) ** 6 - 1
        rows.append({"화면 표기": label, "누적": f"{implied_cum:.1%} (역산)",
                     "함의 CAGR(6년)": f"{cagr:.2%}"})
    print(pd.DataFrame(rows).to_string(index=False))
    print("\n  같은 화면 안에서 +218.0%와 +216.2%가 동시에 표기된다.")
    print("  CAGR 22.4%(0bp)는 누적 236.2%를, 21.6%(30bp)는 223.3%를 함의한다.")
    print("  → 누적·CAGR·비용차감 세 값이 서로 맞지 않는다(같은 시계열이 아님).")


def check_cross_screen() -> None:
    """D - 화면 간 종목 수 모순."""
    print(f"\n{'=' * 78}\n[D] 화면 간 종목 수 모순\n{'=' * 78}")
    churn = {"2021-06-14": 17, "2023-06-12": 20, "2024-06-17": 21,
             "2025-06-16": 21, "2026-06-15": 7}
    pit = {"2020-06-15": 5, "2023-06-12": 6, "2025-12-15": 8, "2026-06-15": 7}
    rows = []
    for d in sorted(set(churn) | set(pit)):
        rows.append({"시행일": d,
                     "편출입 이력 화면": churn.get(d, "-"),
                     "PIT 조회기 화면": pit.get(d, "-"),
                     "일치": "[OK]" if churn.get(d) == pit.get(d)
                             else ("-" if d not in churn or d not in pit
                                   else "[FAIL] 모순")})
    print(pd.DataFrame(rows).to_string(index=False))
    print("\n  2023-06-12: 편출입 이력은 20종목, PIT 조회기는 6종목(후보 10).")
    print("  2025-06-16 21종목 → 2026-06-15 7종목이면 14종목이 한 번에 빠져야")
    print("  하는데, 편출입 이력의 '편출' 칸에는 222800(심텍) 하나뿐이다.")


def check_churn_provenance() -> None:
    """E - 편출입 건수가 어디서 왔는가."""
    print(f"\n{'=' * 78}\n[E] 편출입 건수의 출처\n{'=' * 78}")
    from analysis.demo_why_pit import build_ledger, run
    from analysis.index_calendar import pair_selection_to_rebalance
    from src.rebalance import BUFFER_POLICIES

    led = build_ledger()
    td = pd.bdate_range("2020-01-01", "2026-12-31")
    sel = [s for s, r in pair_selection_to_rebalance(td)
           if r <= as_of_today()]
    shown = {"none": (32, 29), "narrow": (28, 24), "mid": (21, 15),
             "wide": (18, 12)}
    rows = []
    for pol, (a_s, d_s) in shown.items():
        r = run("PIT", pol, sel, led)
        rows.append({"정책": pol, "화면 편입": a_s, "합성데모 편입": r["편입"],
                     "화면 편출": d_s, "합성데모 편출": r["편출"],
                     "일치": "[FAIL] 동일" if (a_s, d_s) == (r["편입"], r["편출"])
                             else "다름"})
    t = pd.DataFrame(rows)
    print(t.to_string(index=False))
    if (t["일치"] == "[FAIL] 동일").all():
        print("\n  화면의 편입·편출 건수가 `analysis/demo_why_pit.py` 의 "
              "**합성 데이터 출력과 4개 정책 전부 정확히 일치**한다.")
        print("  그 데모의 종목은 A00001·C00000·G00001·B00001·T00001 같은 "
              "가상 티커이고,")
        print("  노출도 궤적은 '테마 성장기의 전형적 패턴'을 손으로 만든 "
              "예시다(파일 헤더에 명시).")
        print("  → 화면은 그 합성 결과에 실제 회사명을 입힌 것이다. "
              "실측이 아니다.")


def main() -> int:
    print("대시보드 표시 수치 검산 - 화면의 값이 우리 엔진에서 재현되는가")
    for df, label in [(SHOWN_2026, "2026-06-15 스냅샷"),
                      (SHOWN_2023, "2023-06-12 스냅샷"),
                      (SHOWN_2020, "2020-06-15 스냅샷")]:
        check_weights(df, label)
    check_return_consistency()
    check_cross_screen()
    check_churn_provenance()
    print(f"\n{'=' * 78}")
    print("결론: 화면 수치는 엔진 재현·조문 상한·내부 정합 어느 것도 통과하지")
    print("못한다. 발표 전에 '합성/예시'임을 화면 자체에 명시하거나, 실측으로")
    print("교체해야 한다. 지금 상태로 발표하면 검산 한 번에 무너진다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
