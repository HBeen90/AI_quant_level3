# -*- coding: utf-8 -*-
"""
capacity_v2.py - 회전율 기반 용량 모델 + '유동성 함의 비중 상한'
=================================================================
기존 capacity_analysis.py 는 **초기 전량 편입** 기준의 보수적 상한을 준다
(자기 docstring에도 명시). 실제 운용 부담은 정기변경의 |Δw| 이므로, 이
모듈은 백테스트 이벤트에서 종목별 |Δw| 를 뽑아 그 기준으로 다시 잰다.

두 가지를 산출한다.

  (1) 소요일수  DaysToRebalance_i = AUM x |Δw_i| / (ADV60_i x 참여율)
  (2) **역산 상한**  w_max_i = ADV60_i x 참여율 x 허용일수 / AUM

(2)가 이 모듈의 핵심이다. "위성군 상한 5%" 같은 숫자를 감으로 정하는 대신,
**"정기변경을 며칠 안에 소화할 것인가"라는 운영 목표를 먼저 정하고 상한을
역산**한다. 같은 5%도 AUM·ADV에 따라 3일이 되기도 40일이 되기도 하므로,
고정 퍼센트는 시장이 변하면 의미를 잃는다. 허용일수는 변하지 않는다.

주의 - 이 모듈은 조문을 바꾸지 않는다. 진단 수치를 만들 뿐이며, 상한
신설·변경은 방법론 개정 절차(7.3)를 거쳐야 한다(재량 조정 금지 원칙).

사용
----
    # 정기·캡 재생 |Δw| 기반 용량 - 스냅샷+가격에서 종목별 |Δw| 를 뽑는다.
    #   (event_log.csv 에는 종목별 목표비중이 없어 집계 회전율만 있으므로,
    #    |Δw| 는 스냅샷+가격에서 다시 재생해야 한다. 수시편출·긴급편입은
    #    이 CLI 입력에 아직 연결되지 않아 공식 '전체 실측'으로 부르지 않는다.)
    python analysis/capacity_v2.py --snapshots data/snapshots --prices out/px.csv --adv data/adv60.csv --aum 3000 --policy mid

    # 데이터 없이 정책 감각만 - 역산 상한 표(기본 동작)
    python analysis/capacity_v2.py --aum 3000 --adv-scenarios "15,45,120,500"

--adv CSV 형식: 컬럼 `ticker`(6자리), `adv60_krw`(60일 평균 거래대금, 원 단위).
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

EOK = 1e8          # 1억원


# ----------------------------------------------------------------------
# 1) 종목별 |Δw| - 백테스트 이벤트에서 실측
# ----------------------------------------------------------------------
def turnover_detail(prices: pd.DataFrame, events: list) -> pd.DataFrame:
    """이벤트별·종목별 편도 매매비중 |Δw_i| 를 뽑는다.

    backtest.simulate_index 의 회전율 계산과 **같은 정의**를 쓴다 -
    목표비중끼리 빼는 게 아니라 '드리프트된 현재 비중 대비 목표비중'.
    (목표끼리 빼면 리밸런싱 사이 가격으로 벌어진 비중을 놓쳐 실제 매매량과
     어긋난다 - 기존 엔진의 원칙을 그대로 승계)
    """
    ev: dict = {}
    for e in sorted(events, key=lambda x: x["effective_date"]):
        ev.setdefault(pd.Timestamp(e["effective_date"]), []).append(e)
    rets = prices.pct_change(fill_method=None)
    dates = prices.index[prices.index >= min(ev)]

    rows, w = [], None
    for d in dates:
        if w is not None:
            r = rets.loc[d].reindex(w.index)
            if r.isna().any():
                raise ValueError(f"{d.date()} 활성 종목 수익률 결측: "
                                 f"{r.index[r.isna()].tolist()}")
            w = w * (1 + r)
            w = w / w.sum()
        for e in ev.get(d, []):
            tgt = e["target_weights"]
            if w is not None:
                idx = tgt.index.union(w.index)
                delta = tgt.reindex(idx, fill_value=0.0) \
                    - w.reindex(idx, fill_value=0.0)
                for t, dv in delta.items():
                    if abs(dv) > 1e-12:
                        rows.append({"date": d, "reason": e["reason"],
                                     "ticker": t, "delta_w": float(dv),
                                     "abs_delta_w": abs(float(dv))})
            w = tgt
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------
# 2) 용량 지표
# ----------------------------------------------------------------------
def days_to_trade(abs_delta_w: pd.Series, adv_krw: pd.Series,
                  aum_eok: float, participation: float = 0.10) -> pd.Series:
    """소요 거래일수 = AUM x |Δw| / (ADV60 x 참여율). 단위: 억원 기준."""
    need = abs_delta_w * aum_eok                       # 억원
    cap_per_day = (adv_krw / EOK) * participation      # 억원/일
    return need / cap_per_day


def capacity_implied_cap(adv_krw: pd.Series, aum_eok: float,
                         max_days: float,
                         participation: float = 0.10) -> pd.Series:
    """허용일수에서 역산한 비중 상한.  w_max = ADV x 참여율 x 허용일수 / AUM

    '위성 5% 상한' 같은 고정 퍼센트 대신 쓰는 값이다. 운영 목표(며칠 안에
    소화)를 고정하면 상한이 시장 유동성에 따라 자동으로 조정된다.
    """
    return (adv_krw / EOK) * participation * max_days / aum_eok


def implied_cap_table(adv_scenarios_eok: list, aums_eok: list,
                      days_list: list = (3, 5, 10, 20),
                      participation: float = 0.10) -> pd.DataFrame:
    """ADV x AUM x 허용일수 격자 -> 함의 상한(%) 표."""
    rows = []
    for adv in adv_scenarios_eok:
        for aum in aums_eok:
            r = {"ADV60(억)": adv, "AUM(억)": aum}
            for dd in days_list:
                r[f"{dd}일 내 소화 시 상한"] = \
                    adv * participation * dd / aum
            rows.append(r)
    return pd.DataFrame(rows)


def evaluate_fixed_cap(cap: float, adv_scenarios_eok: list, aum_eok: float,
                       participation: float = 0.10) -> pd.DataFrame:
    """제안된 고정 상한(예: 위성 5%)이 실제로 며칠에 해당하는지 되짚는다."""
    return pd.DataFrame({
        "ADV60(억)": adv_scenarios_eok,
        f"상한 {cap:.0%} 전량 매매 소요일":
            [cap * aum_eok / (a * participation) for a in adv_scenarios_eok],
    })


# ----------------------------------------------------------------------
# 3) 정기·캡 |Δw| 재생 경로
# ----------------------------------------------------------------------
def load_price_panel(path: str) -> pd.DataFrame:
    """가격 패널 CSV(index=날짜, columns=티커) 로드. 티커는 6자리 문자열로 고정."""
    px = pd.read_csv(path, index_col=0, parse_dates=True)
    px.columns = [str(c).zfill(6) for c in px.columns]
    return px


def load_adv(path: str) -> pd.Series:
    """ADV60 CSV -> Series(ticker -> 원). 컬럼: `ticker`, `adv60_krw`(원 단위)."""
    d = pd.read_csv(path, dtype={"ticker": str})
    if "ticker" not in d.columns or "adv60_krw" not in d.columns:
        sys.exit("[FAIL] --adv CSV 컬럼은 ticker, adv60_krw(원 단위) 이어야 합니다")
    tk = d["ticker"].astype(str).str.zfill(6)
    if tk.duplicated().any():
        dup = sorted(tk[tk.duplicated()].unique())
        sys.exit(f"[FAIL] --adv 에 중복 ticker 가 있습니다: {dup} "
                 "(종목당 한 행이어야 합니다)")
    s = pd.to_numeric(d["adv60_krw"], errors="coerce")
    if s.isna().any() or (s <= 0).any():
        sys.exit("[FAIL] --adv adv60_krw 에 결측·비양수 값이 있습니다")
    return pd.Series(s.to_numpy(dtype=float), index=tk.to_numpy())


def real_capacity(snapshots_dir: str, prices_csv: str, adv_csv: str,
                  aum_eok: float, participation: float = 0.10,
                  policy: str = "mid", max_days: float = 5.0) -> pd.DataFrame:
    """스냅샷+가격에서 정기변경·월간 캡을 재생해 종목별 |Δw| 용량을 낸다.

    event_log.csv 는 집계 회전율만 담아 종목별 |Δw| 가 없으므로, run_backtest 와
    **같은 엔진**(build_event_schedule)으로 이벤트를 재생성한 뒤 turnover_detail
    로 |Δw| 를 뽑는다 - 대시보드·백테스트와 같은 회전율 정의를 승계한다.
    현재 입력 계약에는 수시편출·긴급심사·거래정지 파일이 없으므로 이들 이벤트는
    포함하지 않는다. 공식 전체 용량 보고 전에는 해당 입력을 연결해야 한다.

    반환 컬럼: date · reason · ticker · abs_delta_w · adv60_억 · 소요일수 · 함의상한
    """
    from backtest.backtest import build_event_schedule       # 함수 지역 import(순환 방지)
    from src.rebalance import ConfigV2
    from analysis.run_backtest import load_snapshots

    snaps = load_snapshots(snapshots_dir, require_lineage=False)
    px = load_price_panel(prices_csv)
    events, _ = build_event_schedule(px, snaps, {}, cfg=ConfigV2.with_policy(policy))
    td = turnover_detail(px, events)
    if td.empty:
        return td
    adv = load_adv(adv_csv)
    missing = sorted(set(td["ticker"]) - set(adv.index))
    if missing:
        sys.exit(f"[FAIL] --adv 에 없는 종목: {missing} - ADV CSV 를 보강하십시오")
    adv_row = td["ticker"].map(adv)
    return td.assign(
        adv60_억=(adv_row / EOK).round(2),
        소요일수=days_to_trade(td["abs_delta_w"], adv_row,
                            aum_eok, participation).round(2),
        함의상한=capacity_implied_cap(adv_row, aum_eok, max_days,
                                  participation).round(4),
    )


# ----------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--aum", type=float, default=3000, help="AUM(억원)")
    ap.add_argument("--participation", type=float, default=0.10)
    ap.add_argument("--implied-cap", action="store_true",
                    help="데이터 없이 역산 상한 표만 출력")
    ap.add_argument("--adv-scenarios", default="15,45,120,500",
                    help="ADV60 시나리오(억원)")
    ap.add_argument("--aum-scenarios", default="500,1000,3000,10000")
    ap.add_argument("--fixed-cap", type=float, default=0.05,
                    help="검토 중인 고정 상한(위성 5% 제안 등)")
    ap.add_argument("--snapshots", default=None,
                    help="PIT 스냅샷 디렉터리 - 정기·캡 재생 |Δw| 용량 산출")
    ap.add_argument("--prices", default=None,
                    help="가격 패널 CSV(index=날짜, columns=티커)")
    ap.add_argument("--adv", default=None,
                    help="ADV60 CSV(ticker, adv60_krw 원 단위)")
    ap.add_argument("--policy", default="mid", help="버퍼 정책(이벤트 재생성용)")
    ap.add_argument("--max-days", type=float, default=5.0,
                    help="함의상한 역산 허용일수")
    a = ap.parse_args()

    advs = [float(x) for x in a.adv_scenarios.split(",")]
    aums = [float(x) for x in a.aum_scenarios.split(",")]

    # --- 정기·캡 재생 |Δw| 경로 ---
    if a.snapshots and a.prices and a.adv:
        td = real_capacity(a.snapshots, a.prices, a.adv, a.aum,
                           a.participation, a.policy, a.max_days)
        print("=" * 78)
        print(f"정기·캡 재생 |Δw| 용량 - AUM {a.aum:.0f}억 · 참여율 {a.participation:.0%}"
              f" · 정책 {a.policy}")
        print("=" * 78)
        if td.empty:
            print("이벤트에서 매매(|Δw|>0)가 없습니다 - 스냅샷·가격을 확인하십시오.")
        else:
            cols = ["date", "reason", "ticker", "abs_delta_w",
                    "adv60_억", "소요일수", "함의상한"]
            worst = td.sort_values("소요일수", ascending=False).head(15)
            print("[소요일수 상위 15]  소요일수 = AUM × |Δw| / (ADV60 × 참여율)")
            print(worst[cols].to_string(index=False))
            print(f"\n최대 소요일수 {td['소요일수'].max():.1f}거래일 "
                  f"(정기변경 주기 약 125거래일과 비교) · "
                  f"이벤트 {td['date'].nunique()}회 · 매매행 {len(td)}건")
        print("\n(아래는 데이터와 무관한 시나리오 참고표)\n")
    elif any((a.snapshots, a.prices, a.adv)):
        sys.exit("[FAIL] 실측 경로는 --snapshots · --prices · --adv 를 모두 "
                 "지정해야 합니다(하나라도 빠지면 시나리오 표만 출력).")

    print("=" * 78)
    print(f"유동성 함의 비중 상한 - 참여율 {a.participation:.0%} 기준")
    print("=" * 78)
    print("w_max = ADV60 x 참여율 x 허용일수 / AUM\n")
    tbl = implied_cap_table(advs, aums, participation=a.participation)
    fmt = {c: "{:.2%}".format for c in tbl.columns if "상한" in c}
    print(tbl.to_string(index=False, formatters=fmt))

    print("\n" + "-" * 78)
    print(f"[역질문] 제안된 고정 상한 {a.fixed_cap:.0%} 는 며칠에 해당하는가?"
          f"  (AUM {a.aum:.0f}억)")
    ev = evaluate_fixed_cap(a.fixed_cap, advs, a.aum, a.participation)
    print(ev.to_string(index=False, float_format=lambda x: f"{x:.1f}"))
    print("\n  같은 5%가 ADV에 따라 소요일수가 수십 배 차이 난다.")
    print("  => 고정 퍼센트는 '얼마나 안전한가'를 말해주지 않는다.")
    print("     허용일수를 정하고 상한을 역산하는 편이 조문으로서 안정적이다.")

    print("\n" + "-" * 78)
    print("[현행 조문과의 관계]")
    print("  현재 위성군 상한: 개별 15%(sat_ind_cap) · 합계 18%(sat_total_cap)")
    print(f"  AUM {a.aum:.0f}억 · ADV60 {advs[0]:.0f}억 종목이 개별 상한 15%까지"
          f" 차면")
    print(f"    전량 매매에 {0.15 * a.aum / (advs[0] * a.participation):.0f}"
          f"거래일 - 정기변경 주기(약 125거래일) 대비 부담 수준을 판단할 것.")
    print("  단, 정기변경 실제 매매는 전량이 아니라 |Δw| 이므로 이 값은 상한이다.")
    print("  정기·캡 재생 |Δw| 는 --snapshots · --prices · --adv 를 함께 지정하면"
          " 같은 표가 재생 이벤트 기준으로 나온다.")

    print("\n주의: 상한 신설·변경은 방법론 개정 절차(7.3) 사안이다. 본 모듈은")
    print("      위원회 상정용 진단 수치를 만들 뿐 조문을 바꾸지 않는다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
