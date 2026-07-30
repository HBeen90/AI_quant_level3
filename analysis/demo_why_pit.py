# -*- coding: utf-8 -*-
"""
demo_why_pit.py - "PIT 스냅샷이 왜 필요한가"를 엔진으로 증명하는 데모
=====================================================================
질문: 굳이 시점별 판정값을 다시 캐야 하나? 지금 확정된 2026-07-23 값을
      13회 시점에 그대로 쓰면 안 되나?

답: 안 된다. 그렇게 하면 **편출입이 한 번도 안 일어나서** 회전율이 0이 되고,
    버퍼 정책 4안이 전부 같은 답을 내놓는다. 즉 27/67을 고를 근거가
    구조적으로 만들어지지 않는다. 백테스트가 '돌아가긴 하는데 아무것도
    측정하지 못하는' 상태가 된다.

이 스크립트는 같은 엔진(regular_rebalance_v2)에 두 가지 입력을 넣어
그 차이를 숫자로 낸다. 가격은 쓰지 않는다 - 편입·편출 판정만 보므로
결과가 시장 데이터에 의존하지 않고 완전히 결정론적이다.

    A. FROZEN : 최신(FY2025) 판정값을 13회 시점에 전부 복사
    B. PIT    : 각 시점에 '그때 공개돼 있던' 사업연도 판정값

※ 노출도 궤적은 **예시(가상)**다. 실제 종목 판정값이 아니라, 테마 성장기에
  흔한 세 가지 패턴(안정·성장형·경계진동)을 재현한 것이다. 결론은 특정
  숫자가 아니라 '두 방식이 구조적으로 다른 결과를 낸다'는 사실이다.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from analysis.build_pit_snapshots import as_of_ledger, screen, to_snapshot  # noqa: E402
from analysis.index_calendar import (as_of_today,  # noqa: E402
                                     pair_selection_to_rebalance)  # noqa: E402
from src.rebalance import BUFFER_POLICIES, ConfigV2, select_v2  # noqa: E402

FYS = list(range(2019, 2026))          # FY2019 ~ FY2025 사업보고서 7개


# ----------------------------------------------------------------------
# 예시 후보 33종목의 '노출도 궤적' - 테마 성장기의 전형적 4패턴
# ----------------------------------------------------------------------
def exposure_paths() -> dict:
    """{종목코드: {사업연도: (group, exposure, mem_ratio)}}"""
    P: dict = {}

    def add(code, grp, expo_by_fy, mem_by_fy=None):
        P[code] = {fy: (grp, expo_by_fy[i],
                        None if mem_by_fy is None else mem_by_fy[i])
                   for i, fy in enumerate(FYS)}

    # ① 앵커 2 - 메모리 제조, 노출도 무관(규칙 0)
    for i, c in enumerate(["A00001", "A00002"]):
        add(c, "anchor", [np.nan] * 7)

    # ② 안정 핵심 5 - 전 구간 임계값에서 멀다. 정책과 무관하게 계속 편입.
    for i, base in enumerate([0.85, 0.72, 0.64, 0.55, 0.48]):
        add(f"C0000{i}", "core", [base] * 7)

    # ③ 성장형 4 - HBM 붐 전에는 미달, 2022~2023 사이에 임계값을 넘는다.
    #    (테마 지수의 존재 이유이자, '오늘 값 고정'이 가장 크게 왜곡하는 유형)
    add("G00001", "core", [0.12, 0.15, 0.22, 0.34, 0.61, 0.78, 0.81])
    add("G00002", "core", [0.08, 0.11, 0.18, 0.26, 0.44, 0.58, 0.66])
    add("G00003", "core", [0.19, 0.21, 0.25, 0.31, 0.47, 0.55, 0.59])
    add("G00004", "core", [0.05, 0.07, 0.13, 0.21, 0.29, 0.38, 0.52])

    # ④ 경계 진동 6 - 30% 근처에서 오르내린다. 버퍼룰이 겨냥하는 바로 그 종목.
    add("B00001", "core", [0.33, 0.28, 0.32, 0.29, 0.33, 0.28, 0.31])
    add("B00002", "core", [0.28, 0.31, 0.29, 0.32, 0.28, 0.31, 0.29])
    add("B00003", "core", [0.31, 0.29, 0.33, 0.27, 0.32, 0.30, 0.28])
    add("B00004", "core", [0.29, 0.33, 0.28, 0.31, 0.29, 0.32, 0.30])
    add("B00005", "core", [0.35, 0.26, 0.34, 0.27, 0.35, 0.26, 0.33])
    add("B00006", "core", [0.27, 0.32, 0.26, 0.33, 0.27, 0.32, 0.27])

    # ⑤ 명백 미달 5 - 전 구간 미편입(후보 명단에는 있으나 규칙 미달)
    for i, base in enumerate([0.05, 0.09, 0.12, 0.06, 0.11]):
        add(f"X0000{i}", "core", [base] * 7)

    # ⑥ 위성 안정 3 / 경계 4 / 미달 2 - 메모리향 비중 70% 기준
    for i, m in enumerate([0.92, 0.85, 0.79]):
        add(f"S0000{i}", "satellite", [0.15] * 7, [m] * 7)
    add("T00001", "satellite", [0.15] * 7, [0.72, 0.67, 0.71, 0.68, 0.72, 0.66, 0.70])
    add("T00002", "satellite", [0.15] * 7, [0.68, 0.71, 0.67, 0.72, 0.68, 0.71, 0.69])
    add("T00003", "satellite", [0.15] * 7, [0.71, 0.68, 0.73, 0.66, 0.71, 0.69, 0.67])
    add("T00004", "satellite", [0.15] * 7, [0.66, 0.72, 0.65, 0.73, 0.67, 0.72, 0.68])
    for i, m in enumerate([0.41, 0.55]):
        add(f"Y0000{i}", "satellite", [0.15] * 7, [m] * 7)
    return P


def build_ledger() -> pd.DataFrame:
    """궤적 -> 판정 원장. 공시일 = 회계연도 종료 후 이듬해 3/30 (실제 관행)."""
    rows = []
    rng = np.random.default_rng(0)
    for code, byfy in exposure_paths().items():
        ff = float(0.45 + 0.3 * rng.random())        # 유동비율(시점 불변 가정)
        for fy, (grp, expo, mem) in byfy.items():
            rows.append({
                "ticker": code, "name": code,
                "disclosed_at": f"{fy + 1}-03-30", "fiscal_year": fy,
                "sector": "메모리제조" if grp == "anchor" else "장비소재",
                "hbm_massproduction": grp == "anchor",
                "hbm_exposure": expo,
                "mem_ratio": mem if mem is not None else (0.5 if grp == "core" else np.nan),
                "process_confirmed": True, "committee_ok": True,
                "free_float": ff, "source": f"예시 FY{fy}", "admin_issue": False})
    d = pd.DataFrame(rows)
    d["disclosed_at"] = pd.to_datetime(d["disclosed_at"])
    return d


def fake_facts(tickers: list) -> pd.DataFrame:
    """시장 데이터는 이 데모의 관심사가 아니므로 전 종목 자격 통과로 고정.
    (실제로는 pykrx가 시점별로 채운다 - build_pit_snapshots.market_facts)"""
    rng = np.random.default_rng(1)
    return pd.DataFrame(
        {"listed": True, "close": 1e4,
         "market_cap": rng.uniform(1e12, 2e14, len(tickers)),
         "mcap_rank": np.arange(1, len(tickers) + 1) * 1.0,
         "adv60": 5e10, "listed_days": 3000},
        index=pd.Index(sorted(tickers), name="ticker"))


# ----------------------------------------------------------------------
def run(mode: str, policy: str, sel_dates: list, led: pd.DataFrame) -> dict:
    """13회 정기변경을 순차 실행하며 편입·편출을 센다. 가격 불필요."""
    cfg = ConfigV2.with_policy(policy)
    prev: set = set()
    added_total = dropped_total = 0
    n_hist, churn = [], []
    latest = pd.Timestamp("2099-01-01")               # FROZEN 기준 시점
    for d in sel_dates:
        pit = as_of_ledger(led, d if mode == "PIT" else latest)
        snap = to_snapshot(screen(fake_facts(pit["ticker"].tolist()), pit))
        res = select_v2(snap, prev_members=prev, cfg=cfg)
        cur = set(res["members"]["ticker"])
        added, dropped = cur - prev, prev - cur
        if prev:                                       # 최초 구성은 회전 아님
            added_total += len(added)
            dropped_total += len(dropped)
            churn.append(len(added) + len(dropped))
        n_hist.append(len(cur))
        prev = cur
    return {"편입": added_total, "편출": dropped_total,
            "편출입 합": added_total + dropped_total,
            "평균 종목수": float(np.mean(n_hist)),
            "최종 종목수": n_hist[-1],
            "회차별 변동": churn}


def main() -> int:
    led = build_ledger()
    td = pd.bdate_range("2020-01-01", "2026-12-31")
    today = as_of_today()
    pairs = [(s, r) for s, r in pair_selection_to_rebalance(td) if r <= today]
    sel_dates = [s for s, _ in pairs]

    print("=" * 74)
    print("PIT 스냅샷이 왜 필요한가 - 같은 엔진, 다른 입력")
    print("=" * 74)
    print(f"심사 시점 {len(sel_dates)}회: "
          f"{sel_dates[0].date()} ~ {sel_dates[-1].date()}")
    print(f"후보 {led['ticker'].nunique()}종목 · 원장 {len(led)}행 "
          f"(종목당 사업연도 {len(FYS)}개)\n")

    rows = {}
    for mode in ("FROZEN", "PIT"):
        for pol in BUFFER_POLICIES:
            r = run(mode, pol, sel_dates, led)
            rows[(mode, pol)] = {
                "유지선": f"{BUFFER_POLICIES[pol]['hold_core']:.2f}/"
                          f"{BUFFER_POLICIES[pol]['hold_sat']:.2f}",
                "편입": r["편입"], "편출": r["편출"],
                "편출입 합": r["편출입 합"],
                "평균 종목수": round(r["평균 종목수"], 1),
                "최종 종목수": r["최종 종목수"]}
    tbl = pd.DataFrame(rows).T
    tbl.index.names = ["입력", "버퍼정책"]
    print(tbl.to_string())

    frozen = tbl.loc["FROZEN", "편출입 합"]
    pit = tbl.loc["PIT", "편출입 합"]
    print("\n" + "-" * 74)
    print("[해석]")
    if frozen.nunique() == 1 and int(frozen.iloc[0]) == 0:
        print(f"  · FROZEN: 편출입 {int(frozen.iloc[0])}회. 4개 정책이 전부 동일한 결과.")
        print("    -> 회전율 0. 버퍼룰이 '아무것도 안 한' 것으로 측정된다.")
        print("    -> 27/67 대 25/65 대 29/69 를 비교할 근거가 만들어지지 않는다.")
        print("    -> 백테스트는 돌아가지만 측정하는 것이 없다.")
    print(f"  · PIT   : 편출입 {int(pit.min())}~{int(pit.max())}회, 정책마다 다르다.")
    print(f"    -> 버퍼를 넓힐수록 편출입 {int(pit.loc['none'])} -> "
          f"{int(pit.loc['wide'])}회로 줄어든다. 이 감소분이 곧 매매비용 절감이고,")
    print("       그 대가(적합도 하락)와 맞바꾸는 표가 27/67 채택의 근거가 된다.")
    print("\n  즉 '시점별로 변하는 판정값'이 이 백테스트의 유일한 정보원이다.")
    print("  오늘 값을 과거에 복사하면 정보량이 0이 되어, 엔진이 아무리 정교해도")
    print("  아무 결론도 나오지 않는다.")

    print("\n" + "-" * 74)
    print("[어떤 종목이 실제로 움직였나 - PIT/mid 기준]")
    prev, log = set(), []
    cfg = ConfigV2.with_policy("mid")
    for s, r in pairs:
        pit_led = as_of_ledger(led, s)
        snap = to_snapshot(screen(fake_facts(pit_led["ticker"].tolist()), pit_led))
        res = select_v2(snap, prev_members=prev, cfg=cfg)
        cur = set(res["members"]["ticker"])
        if prev and (cur - prev or prev - cur):
            log.append({"시행일": r.date(), "n": len(cur),
                        "편입": ",".join(sorted(cur - prev)) or "-",
                        "편출": ",".join(sorted(prev - cur)) or "-"})
        prev = cur
    print(pd.DataFrame(log).to_string(index=False) if log else "  (변동 없음)")
    print("\n  G* = 성장형(HBM 붐으로 임계값 통과) · B*/T* = 경계 진동 종목.")
    print("  성장형의 '언제 편입되는가'가 테마 지수의 서사이고, 경계 진동의")
    print("  '얼마나 들락거리는가'가 버퍼룰의 존재 이유다. 둘 다 시점별")
    print("  판정값 없이는 볼 수 없다.")

    print("\n" + "-" * 74)
    print("[작업량에 관한 좋은 소식]")
    print("  위 표에서 변동은 전부 6월 시행일에만 일어난다. 12월 시행일에는")
    print("  판정 변화가 없다 - 사업보고서가 연 1회(3월)뿐이라, 12월 심사는")
    print("  6월과 같은 사업연도 자료를 본다(시가총액만 갱신).")
    print("  => 판정 원장은 '종목 x 심사시점 13행'이 아니라 **'종목 x 사업연도'**")
    print(f"     로 만들면 된다. 이 데모에서 {led['ticker'].nunique()}종목 x "
          f"{len(FYS)}개 사업연도 = {len(led)}행.")
    print("     as_of_ledger 가 공시일로 시점 매핑을 알아서 해준다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
