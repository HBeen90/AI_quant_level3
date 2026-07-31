# -*- coding: utf-8 -*-
"""
verify_claims.py - 발표에 쓸 문장을 하나씩 실제로 재현해서 검증한다
====================================================================
이 프로젝트에서 같은 사고가 세 번 반복됐다.

  1) 채팅으로 "회전율 48.5% -> 24.6%, CAGR 20.3% -> 21.6%" 가 들어옴
  2) 대시보드가 그 위에 지수 3,161.9pt · Sharpe 0.89 · MDD -25.4% 를 실음
  3) 종목별 노출도·ADV60·벤치마크까지 실측처럼 표시됨

셋 다 원인이 같다 - **출처 없는 숫자가 문서·화면에 들어가는 것을 막는
장치가 없었다.** 매번 사후에 잡아내는 대신, 여기서 구조적으로 막는다.

원칙
----
  * 발표·문서에 쓰는 모든 수치는 이 파일에 **재현 함수**와 함께 등록한다.
  * 등록되지 않은 수치는 **인용 금지**다(합성·가정·추정 포함).
  * 이 스크립트가 통과해야 그 문장을 쓸 수 있다. 통과 못 하면 문장을 고친다.

실행
----
    python analysis/verify_claims.py            # 전체 검증
    python analysis/verify_claims.py --md       # FACTSHEET용 마크다운 출력
    python analysis/verify_claims.py --fast     # 테스트 스위트 실행 생략

출력의 각 줄은 "이 문장을 지금 이 순간 재현했다"는 뜻이다.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import warnings

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
warnings.filterwarnings("ignore")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

TOL = 1e-9


# ==========================================================================
# 재현 함수 - 각 claim 은 (실측값, 표시문자열) 을 돌려준다
# ==========================================================================
def c_base_date():
    """기준일 2020-06-15가 3장 일정 조문에서 재생되는가."""
    from analysis.index_calendar import as_of_today, rebalance_dates
    td = pd.bdate_range("2020-01-01", "2026-12-31")
    rebs = [d for d in rebalance_dates(td) if d <= as_of_today()]
    ok = pd.Timestamp("2020-06-15") in rebs
    return ok, (f"{rebs[0].date()} = 2020년 6월 만기일(둘째 목요일) 익주 첫 "
                f"영업일 · 오늘까지 정기변경 {len(rebs)}회")


def c_regular_equivalence():
    """정기변경: index_calc 신·구 경로 동치."""
    from src import index_calc as ic
    codes = ["005930", "000660", "042700", "089030", "003160", "348210",
             "112290"]
    rng = np.random.default_rng(11)
    dates = pd.bdate_range(ic.BASE_DATE, periods=260)
    px = pd.DataFrame(100000 * np.exp(np.cumsum(
        rng.normal(3e-4, 0.02, (260, len(codes))), axis=0)),
        index=dates, columns=codes)
    w1 = pd.Series([.2157, .1843, .18, .18, .1281, .1058, .0061], index=codes)
    f1 = pd.Series([12405393.9, 10598120.1, 90214.2, 13614.9, 3183.1,
                    2629.4, 1125.4], index=codes)
    recon = [{"date": dates[0], "weights": w1, "ff_mcap": f1}]
    old = ic.build_daily_series(px, recon)
    new = ic.build_index_series(px, recon)["level"]
    rel = float(((new - old).abs() / old).max())
    return rel < TOL, f"최대 상대차 {rel:.2e}"


def c_adhoc_equivalence():
    """수시편출: 제수 조정 == 드리프트 후 정규화 (파트 간 접합)."""
    from backtest.backtest import make_event, simulate_index
    from src import index_calc as ic
    codes = ["005930", "000660", "042700", "089030", "003160", "348210",
             "112290"]
    rng = np.random.default_rng(11)
    dates = pd.bdate_range(ic.BASE_DATE, periods=130)
    px = pd.DataFrame(100000 * np.exp(np.cumsum(
        rng.normal(3e-4, 0.02, (130, len(codes))), axis=0)),
        index=dates, columns=codes)
    w0 = pd.Series([.2157, .1843, .18, .18, .1281, .1058, .0061], index=codes)
    f0 = pd.Series([12405393.9, 10598120.1, 90214.2, 13614.9, 3183.1,
                    2629.4, 1125.4], index=codes)
    d_ex, gone = dates[60], "089030"
    lvA = ic.build_index_series(px, [{"date": dates[0], "weights": w0,
                                      "ff_mcap": f0}],
                                [{"date": d_ex, "kind": "exclusion",
                                  "tickers": [gone]}])["level"]
    r = px.pct_change(fill_method=None)
    w = w0.copy()
    for d in dates[1:dates.get_loc(d_ex) + 1]:
        w = w * (1 + r.loc[d])
        w = w / w.sum()
    w_ex = w.drop(gone)
    lvB = simulate_index(px, [make_event(dates[0], "regular", w0),
                              make_event(d_ex, "exclusion", w_ex / w_ex.sum())],
                         base=ic.BASE_INDEX_LEVEL)["level"]
    rel = float(((lvA - lvB).abs() / lvB).max())
    return rel < TOL, f"최대 상대차 {rel:.2e}"


def c_tr_equivalence():
    """TR: 제수 기반 == 수익률 기반 재투자."""
    from backtest.backtest import make_event, simulate_index
    from src import index_calc as ic
    codes = ["005930", "000660", "042700", "089030", "003160"]
    rng = np.random.default_rng(17)
    dates = pd.bdate_range(ic.BASE_DATE, periods=250)
    px = pd.DataFrame(100000 * np.exp(np.cumsum(
        rng.normal(2e-4, 0.015, (250, len(codes))), axis=0)),
        index=dates, columns=codes)
    w0 = pd.Series([.30, .25, .20, .15, .10], index=codes)
    f0 = pd.Series([12405393.9, 10598120.1, 90214.2, 13614.9, 3183.1],
                   index=codes)
    div = pd.DataFrame(0.0, index=dates, columns=codes)
    for d in (dates[80], dates[200]):
        dps = (px.loc[d] * pd.Series([0.018, 0.015, 0.004, 0.0, 0.010],
                                     index=codes)).round(0)
        div.loc[d] = dps.values
        px.loc[d:, :] = px.loc[d:, :] - dps.values
    recon = [{"date": dates[0], "weights": w0, "ff_mcap": f0}]
    a = ic.build_index_series(px, recon, [
        {"date": d, "kind": "dividend", "dps": div.loc[d]}
        for d in (dates[80], dates[200])])["level"]
    b = simulate_index(px, [make_event(dates[0], "regular", w0)],
                       base=ic.BASE_INDEX_LEVEL, mode="gross_tr",
                       ordinary_dividends=div)["level"]
    rel = float(((a - b).abs() / b).max())
    return rel < TOL, f"최대 상대차 {rel:.2e}"


def c_monthly_cap():
    """월말 30% 캡이 실제로 발동해 단일 종목 쏠림을 누르는가."""
    from analysis.audit_review_claims import scenario_prices, snapshot
    from backtest.backtest import build_event_schedule
    from src.rebalance import ConfigV2
    px = scenario_prices()
    events, _ = build_event_schedule(px, {px.index[0]: snapshot(px)},
                                     cfg=ConfigV2())
    caps = [e for e in events if e["reason"] == "cap"]
    reg = [e for e in events if e["reason"] == "regular"]
    r = px.pct_change(fill_method=None)
    w = reg[0]["target_weights"].copy()
    peak = 0.0
    for d in px.index[1:]:
        w = w * (1 + r.loc[d].reindex(w.index))
        w = w / w.sum()
        peak = max(peak, float(w.max()))
    capped = max(float(e["target_weights"].max()) for e in caps) if caps else 1.0
    return bool(caps) and capped <= 0.2500 + 1e-9, \
        f"캡 {len(caps)}회 발동 · 캡 OFF 최대 {peak:.2%} -> 캡 ON {capped:.2%}"


def c_bucket_drift():
    """개별 캡이 잡지 못하는 버킷 드리프트가 존재하는가(진짜 공백)."""
    from analysis.audit_review_claims import (bucket_weights, scenario_prices,
                                              snapshot)
    from backtest.backtest import build_event_schedule
    from src.rebalance import ANCHOR, ConfigV2
    px = scenario_prices()
    snap = snapshot(px)
    groups = snap.set_index("ticker")["group"]
    events, _ = build_event_schedule(px, {px.index[0]: snap}, cfg=ConfigV2())
    ev = {e["effective_date"]: e for e in events}
    r = px.pct_change(fill_method=None)
    w = events[0]["target_weights"].copy()
    for d in px.index[1:]:
        w = w * (1 + r.loc[d].reindex(w.index))
        w = w / w.sum()
        if d in ev:
            w = ev[d]["target_weights"].copy()
    a0 = bucket_weights(events[0]["target_weights"], groups)[ANCHOR]
    aT = bucket_weights(w, groups)[ANCHOR]
    return aT < a0 - 0.01, \
        f"앵커 합계 {a0:.2%} (정기변경일) -> {aT:.2%} (기말), 최대 단일 {w.max():.2%}"


def c_capacity_inverse():
    """고정 5% 상한이 ADV에 따라 며칠에 해당하는가."""
    from analysis.capacity_v2 import evaluate_fixed_cap
    ev = evaluate_fixed_cap(0.05, [15.0, 45.0, 500.0], 3000.0, 0.10)
    d = dict(zip(ev.iloc[:, 0], ev.iloc[:, 1]))
    ok = abs(d[15.0] - 100.0) < 1e-9 and abs(d[45.0] - 33.3333) < 1e-3
    return ok, (f"AUM 3,000억 · 참여율 10% 기준 - ADV 15억 {d[15.0]:.1f}일 · "
                f"45억 {d[45.0]:.1f}일 · 500억 {d[500.0]:.1f}일")


def c_pit_vs_frozen():
    """오늘 값 고정(FROZEN) 시 편출입이 0이 되어 정책 비교가 불가능한가."""
    from analysis.demo_why_pit import build_ledger, run
    from analysis.index_calendar import (as_of_today,
                                         pair_selection_to_rebalance)
    from src.rebalance import BUFFER_POLICIES
    led = build_ledger()
    td = pd.bdate_range("2020-01-01", "2026-12-31")
    sel = [s for s, r in pair_selection_to_rebalance(td) if r <= as_of_today()]
    fz = [run("FROZEN", p, sel, led)["편출입 합"] for p in BUFFER_POLICIES]
    pt = [run("PIT", p, sel, led)["편출입 합"] for p in BUFFER_POLICIES]
    ok = set(fz) == {0} and min(pt) > 0 and len(set(pt)) > 1
    return ok, (f"FROZEN {set(fz)} (4개 정책 동일) vs PIT {min(pt)}~{max(pt)}회 "
                f"(정책별 상이) - 합성 시나리오")


def c_pit_changes_outcome():
    """PIT 규율이 실제 편입 결과를 바꾸는가(형식이 아님)."""
    from analysis.build_pit_snapshots import as_of_ledger, screen, to_snapshot
    from src.rebalance import ConfigV2, select_v2
    from tests.test_pit_snapshots import _ledger
    led = _ledger()
    facts = pd.DataFrame(
        {"listed": [True] * 3, "close": [1e4] * 3,
         "market_cap": [5e12, 3e14, 8e11], "mcap_rank": [30.0, 2.0, 300.0],
         "adv60": [5e9, 5e11, 3e9], "listed_days": [3000, 9000, 2000]},
        index=pd.Index(["000100", "000200", "000300"], name="ticker"))
    cfg = ConfigV2()
    m20 = set(select_v2(to_snapshot(screen(
        facts, as_of_ledger(led, pd.Timestamp("2020-05-29")))),
        set(), cfg)["members"]["ticker"])
    m22 = set(select_v2(to_snapshot(screen(
        facts, as_of_ledger(led, pd.Timestamp("2022-05-31")))),
        set(), cfg)["members"]["ticker"])
    ok = "000100" not in m20 and "000100" in m22
    return ok, "동일 종목이 2020년 미편입 -> 2022년 편입 (근거 사업연도 변경)"


def c_judgment_snapshot():
    """2026-07-23 확정 33종목 판정과 7종목 비중을 재현하는가."""
    from analysis.verify_judgment_snapshot import run_verification
    r = run_verification()
    counts = r["group_counts"]
    ok = (
        r["candidate_count"] == 33
        and r["selected_count"] == 7
        and counts == {"앵커": 2, "핵심": 4, "위성": 1}
        and r["max_weight_error_pp"] < 0.005
    )
    return ok, (
        f"후보 33 -> 편입 7 (앵커 {counts['앵커']} · 핵심 {counts['핵심']} · "
        f"위성 {counts['위성']}) · 비중 최대 오차 "
        f"{r['max_weight_error_pp']:.6f}%p"
    )


def c_final_ledger():
    """역사적 PIT 판정 원장의 strict 계약과 NO_DATA 분리를 재현한다."""
    from analysis.build_pit_snapshots import load_ledger
    ledger = load_ledger(os.path.join(HERE, "data", "verdict_ledger.csv"))
    no_data = pd.read_csv(
        os.path.join(HERE, "data", "no_data_rows.csv"), dtype={"ticker": str})
    years = sorted(ledger["fiscal_year"].unique().tolist())
    ok = (
        len(ledger) == 215
        and ledger["ticker"].nunique() == 33
        and years == list(range(2019, 2026))
        and set(ledger["judgment_status"]) == {"FINAL"}
        and len(no_data) == 8
    )
    return ok, (
        f"strict FINAL {len(ledger)}행 · {ledger['ticker'].nunique()}종목 · "
        f"FY{years[0]}~FY{years[-1]} · NO_DATA {len(no_data)}행 별도 보존"
    )


def c_survivorship_survey():
    """생존편향 - 표본 기간 소멸 종목 전수 조사를 보존 명단으로 재현한다.

    무엇을 주장하고 무엇을 주장하지 않는가
    -------------------------------------
    주장한다   : 조사 범위(심사 13시점 전 시장 명단)와 후보 수(0건)
    주장 안 한다: 편향의 **크기**. 후보가 0건이라 재실행할 대상이 없어
                 크기 측정 자체가 성립하지 않는다. FORBIDDEN 에 그대로 둔다.

    두 기준(소멸 직전 / 편입 이력)이 **모두** 0건일 때만 통과시킨다. 한쪽만
    0이면 결론이 기준 선택에 의존한다는 뜻이고, 그건 조사가 아니라 우연이다.
    """
    from analysis.survivorship_check import (find_disappeared, ledger_tickers,
                                             review_dates)
    snap_dir = os.path.join(HERE, "data", "snapshots")
    ev_dir = os.path.join(HERE, "evidence", "survivorship")
    dates = review_dates(snap_dir)
    gone = find_disappeared(dates, ev_dir)          # 캐시 없으면 SystemExit
    known = ledger_tickers(snap_dir)
    n_strict = int(gone["소멸직전_반도체지수"].sum())
    n_ever = int(gone["반도체지수_이력"].sum())
    cand = gone[gone["반도체지수_이력"] & ~gone["ticker"].isin(known)]
    ok = (
        len(dates) == 13
        and len(gone) > 0                 # 전 시장 소멸 0건은 캐시 이상 신호
        and n_strict == n_ever            # 기준 선택에 의존하지 않을 것
        and len(cand) == 0
    )
    return ok, (
        f"심사 {len(dates)}시점 전 시장 소멸 {len(gone)}건 전수 조사 · "
        f"반도체지수 편입 이력 보유 {n_ever}건(직전 기준 {n_strict}건) · "
        f"판정 대상 후보 {len(cand)}건 - 크기는 미측정"
    )


def c_capacity_measured():
    """실측 ADV60 로 잰 정기변경 용량. 가정 ADV 시나리오와 구분되는 수치다.

    무엇이 달라졌나
    ---------------
    기존 클레임(c_capacity_inverse)은 **가정 ADV**(15·45·500억) 시나리오로
    "고정 % 상한은 ADV에 따라 소요일수가 수십 배 달라진다"는 구조를 보였다.
    이건 산식의 성질이지 우리 지수의 수치가 아니다.
    이 클레임은 2026-07-23 기준 **실측 ADV60** 으로 우리 지수의 실제
    정기변경 |Δw| 를 소화하는 데 며칠 걸리는지를 잰다.

    범위 제한(반드시 함께 말할 것)
      · ADV60 은 2026-07-23 **1시점** 값이다. 시계열이 아니다.
        (인용 금지 목록의 'ADV60 시계열' 항목은 그대로 유효하다)
      · 수시편출·긴급심사·거래정지 이벤트는 capacity 입력에 아직 없다.
        정기변경과 월간 캡만 반영된 값이다.
    """
    import warnings as _w
    import logging as _log
    from analysis.capacity_v2 import real_capacity
    import json as _json

    adv_csv = os.path.join(HERE, "data", "adv60.csv")
    man = _json.loads(open(os.path.join(HERE, "data", "adv60_manifest.json"),
                           encoding="utf-8").read())
    # 이 클레임은 백테스트 이벤트를 재생하므로 weighting 의 '희소 조항 발동'
    # 로그가 수십 줄 딸려 나온다. 확정 실행 로그에서 그 줄들은 정보가 아니라
    # 소음이고, 정작 봐야 할 [PASS]/[FAIL] 표를 밀어낸다. 재생 구간에서만
    # 낮추고 원복한다 - 엔진 기본 로그 설정은 건드리지 않는다.
    _wl = _log.getLogger("src.weighting")
    _prev = _wl.level
    _wl.setLevel(_log.ERROR)
    try:
        with _w.catch_warnings():
            _w.simplefilter("ignore")       # 하한 미달 경고는 여기서 의미 없음
            td = real_capacity(
                os.path.join(HERE, "data", "snapshots"),
                os.path.join(HERE, "out", "px.csv"),
                adv_csv, aum_eok=3000.0, participation=0.10, policy="mid")
    finally:
        _wl.setLevel(_prev)
    if td.empty:
        return False, "용량 재생 결과가 비었습니다"
    worst = td.sort_values("소요일수", ascending=False).iloc[0]
    adv = pd.read_csv(adv_csv, dtype={"ticker": str})
    ok = (
        len(adv) > 0
        and (pd.to_numeric(adv["adv60_krw"], errors="coerce") > 0).all()
        and float(worst["소요일수"]) > 0
        # 정의 단일화 확인 - 스크린과 같은 함수를 썼다는 기록이 남아 있어야 한다
        and "market_facts" in str(man.get("원천", ""))
        and str(man.get("asof", "")).strip() == "2026-07-23"
    )
    return ok, (
        f"AUM 3,000억·참여율 10% 기준 최대 소요 {float(worst['소요일수']):.1f}거래일 "
        f"({worst['ticker']} · |Δw| {float(worst['abs_delta_w']) * 100:.1f}% · "
        f"ADV60 {float(worst['adv60_억']):.0f}억 · 함의상한 "
        f"{float(worst['함의상한']) * 100:.2f}%) · ADV60 실측 {len(adv)}종목 "
        f"({man.get('asof')} 1시점 - 시계열 아님 · 정기·캡만 반영)"
    )


def c_bucket_mandate_real():
    """실지수에서 앵커 40%가 지켜진 적이 있는가 - 합성이 아닌 실측.

    무엇이 달라졌나
    ---------------
    기존 클레임(c_bucket_drift)은 **합성 가격**에서 "개별 캡이 못 잡는 버킷
    드리프트가 있다"는 메커니즘을 보였다. 그건 술식의 성질이지 우리 지수의
    수치가 아니다. 이 클레임은 실제 스냅샷 13회 · 실제 가격 1498거래일로
    같은 질문을 다시 던지고, 답이 다르다는 것을 고정한다.

      · 앵커 40%는 13회 중 1회만 실현됐다. 나머지는 희소 조항이 발동해
        85% / 67% / 64%에서 **출발**했다. 흘러내린 것이 아니다.
      · 40% 대비 괴리의 대부분은 구조 성분이다. 드리프트 성분은 소수다.
        즉 리밸런싱 주기를 조여도 해결되지 않는다.

    왜 CLAIMS 에 두는가
    -------------------
    이 문장은 규정과 구현의 불일치를 주장하므로 AUDITS 성격도 있다. 그러나
    발표에서 **먼저 말해야 하는 사실**이라 CLAIMS 에 둔다 - 질문받고 나서
    말하면 은폐로 읽힌다.
    """
    from analysis.bucket_drift import measure
    m = measure(os.path.join(HERE, "data", "snapshots"),
                os.path.join(HERE, "out", "px.csv"))
    rev, best = m["reviews"], m["실현가능_최소구성"]
    floor = m["cfg"].min_constituents
    ok = (
        m["재현오차"] < 1e-10                     # 엔진과 같은 지수를 본 것
        and m["총_회차"] > 0
        and m["규정충족_회차"] < m["총_회차"]      # 40%가 안 지켜진 회차가 있다
        and m["구조_비중"] > 0.5                   # 원인이 가격이 아니라 규칙이다
        and best["종목수"] > floor                 # 하한이 실현가능 하한보다 낮다
    )
    nocore = int((rev["핵심수"] == 0).sum())
    return ok, (
        f"앵커 40% 실현 {m['총_회차']}회 중 {m['규정충족_회차']}회 · "
        f"시간가중평균 {m['앵커_시간가중평균']:.1%} · "
        f"+-5%p 밴드 체류 {m['밴드내_비율']:.1%} · "
        f"괴리 분해 구조 {m['구조성분'] * 100:.2f}%p vs 드리프트 "
        f"{m['드리프트성분'] * 100:.2f}%p (구조가 {m['구조_비중']:.0%}) · "
        f"핵심군 0종목 {nocore}회 · 실현가능 하한 {best['종목수']}종목 > "
        f"선언 하한 {floor}종목"
    )


def c_pr_tr_parallel():
    """PR/TR 병기 - 배당 기여도를 재계산하고 **가정을 문장에 붙여서** 낸다.

    왜 인용 금지가 아니라 클레임인가
    -------------------------------
    '생존편향 크기'는 측정 자체를 안 했으니 인용 금지다. TR 수치는 다르다 -
    계산됐고 재현되며, 다만 **두 가지 가정에 의존**한다(배당락일 = 12월 마지막
    거래일 / 연간 DPS 일괄 반영). 그런 수치는 숨길 게 아니라 **가정을 달고**
    내보내는 것이 맞다. 그래서 재현값 문자열에 가정을 박아 넣는다 - 팩트시트에
    숫자만 실리고 가정이 떨어져 나가는 일을 구조적으로 막는다.

    검증 두 가지
      1) 연환산 기여도가 (TR누적/PR누적) 로 재계산되는가
      2) MDD 구간에 배당락일이 없으면 PR·TR MDD 가 **정확히** 같은가
         - 배당이 엉뚱한 날짜에 꽂히면 이 등식이 깨진다. 날짜 배선 검사다.
    """
    import json as _json
    tr_dir = os.path.join(HERE, "out", "backtest_tr")
    lv = pd.read_csv(os.path.join(tr_dir, "index_level_pr_tr.csv"),
                     index_col=0, parse_dates=True)
    div = pd.read_csv(os.path.join(HERE, "data", "dividends.csv"),
                      dtype={"ticker": str})
    man = _json.loads(open(os.path.join(HERE, "data",
                                        "dividends_manifest.json"),
                           encoding="utf-8").read())
    pr, tr = lv["PR"].astype(float), lv["TR"].astype(float)
    yrs = (lv.index[-1] - lv.index[0]).days / 365.25
    gap = (tr.iloc[-1] / pr.iloc[-1]) ** (1 / yrs) - 1

    def _mdd(s):
        dd = s / s.cummax() - 1
        return float(dd.min()), dd.idxmin(), s[:dd.idxmin()].idxmax()

    mdd_pr, low_pr, peak_pr = _mdd(pr)
    mdd_tr, low_tr, peak_tr = _mdd(tr)
    ex_dates = pd.to_datetime(div["ex_date"].unique())
    ex_in_window = [d for d in ex_dates if peak_pr <= d <= low_pr]
    ok = (
        gap > 0                                   # 배당은 TR을 끌어올린다
        and abs((1 + gap) * (pr.iloc[-1] / pr.iloc[0]) ** (1 / yrs)
                - (tr.iloc[-1] / tr.iloc[0]) ** (1 / yrs)) < 1e-9
        and len(div) > 0
        and str(man.get("인용_제한", "")).strip() != ""
        # 배당락일이 MDD 구간 밖이면 두 계열의 MDD 는 정확히 같아야 한다
        and (bool(ex_in_window) or abs(mdd_pr - mdd_tr) < 1e-12)
    )
    return ok, (
        f"연환산 배당 기여도 {gap * 100:.4f}%p · 배당 {len(div)}건/"
        f"{div['ticker'].nunique()}종목 · MDD 구간 내 배당락일 "
        f"{len(ex_in_window)}일이라 PR·TR MDD 일치 "
        f"(가정: 배당락일=12월 마지막 거래일 · 연간 DPS 일괄 반영 - 보조 비교 전용)"
    )


def c_kind_admin_history():
    """D3 KIND 0350 조사 결과와 보존 원응답 232건을 재검증한다."""
    import hashlib
    import json
    import zipfile

    data_dir = os.path.join(HERE, "data")
    evidence_dir = os.path.join(
        HERE, "evidence", "kind_admin_history_20260730")
    summary = pd.read_csv(
        os.path.join(data_dir, "admin_history_kind_2020_2026.csv"),
        dtype={"코드": str, "ticker": str}).fillna("")
    query = pd.read_csv(
        os.path.join(evidence_dir, "query_log.csv"),
        dtype={"ticker": str, "response_sha256": str})
    control = pd.read_csv(
        os.path.join(evidence_dir, "control_log.csv"),
        dtype={"ticker": str, "response_sha256": str})
    manifest = json.loads(open(
        os.path.join(evidence_dir, "run_manifest.json"),
        encoding="utf-8").read())
    raw_zip = os.path.join(evidence_dir, "raw_kind_responses.zip")

    rows = pd.concat([
        query[["response_file", "response_sha256"]],
        control[["response_file", "response_sha256"]],
    ], ignore_index=True)
    matched = 0
    with zipfile.ZipFile(raw_zip) as archive:
        names = set(archive.namelist())
        for row in rows.itertuples(index=False):
            if row.response_file in names and hashlib.sha256(
                    archive.read(row.response_file)).hexdigest().lower() \
                    == row.response_sha256.lower():
                matched += 1

    collector = os.path.join(
        HERE, "analysis", "collect_kind_admin_history.py")
    ok = (
        len(summary) == 33
        and summary["ticker"].nunique() == 33
        and set(summary["ticker"]) == set(query["ticker"])
        and summary["지정여부(Y/N)"].eq("N").all()
        and pd.to_numeric(summary["조회건수"]).eq(0).all()
        and len(query) == 231
        and query.groupby("ticker").size().eq(7).all()
        and pd.to_numeric(query["result_count"]).eq(0).all()
        and query["kind_type_code"].astype(str).eq("350").all()
        and len(control) == 1
        and str(control.iloc[0]["matched"]).upper() == "Y"
        and int(control.iloc[0]["result_count"]) == 2
        and matched == 232
        and manifest.get("query_count") == 231
        and manifest.get("event_count") == 0
        and manifest.get("positive_control", {}).get("status") == "PASS"
        and manifest.get("raw_response_entry_count") == 232
        and hashlib.sha256(open(raw_zip, "rb").read()).hexdigest().lower()
        == str(manifest.get("raw_response_zip_sha256", "")).lower()
        and hashlib.sha256(open(collector, "rb").read()).hexdigest().lower()
        == str(manifest.get("build_environment", {}).get(
            "collector_sha256", "")).lower()
    )
    return ok, (
        f"33종목·표적 231질의에서 관리종목 이벤트 0건 · "
        f"양성 대조 2건 · 원응답 해시 {matched}/232 일치 "
        f"(승인 서명 대기)"
    )


def c_dashboard_weights():
    """대시보드 표시 비중이 엔진에서 재현되는가 / 조문 상한을 지키는가."""
    from analysis.audit_dashboard_numbers import SHOWN_2026
    from src import weighting
    from src.rebalance import ConfigV2
    KR = {"anchor": "앵커", "core": "핵심", "satellite": "위성"}
    w = weighting.allocate(SHOWN_2026["group"].map(KR).to_numpy(),
                           SHOWN_2026["유동시총(조)"].to_numpy(float))
    err = float(np.abs(w - SHOWN_2026["화면 비중"].to_numpy()).max())
    cap = ConfigV2().core_ind_cap
    viol = SHOWN_2026[(SHOWN_2026["group"] == "core")
                      & (SHOWN_2026["화면 비중"] > cap + 1e-9)]
    # '재현 실패'가 확인되는 것이 이 claim 의 통과 조건이다
    return err > 0.005 and len(viol) > 0, \
        (f"최대 재현 오차 {err * 100:.2f}%p (허용 0.005%p) · "
         f"core 상한 {cap:.0%} 위반 {len(viol)}건")


def c_test_suite():
    """전 테스트 스위트가 통과하는가."""
    p = subprocess.run([sys.executable, os.path.join(HERE, "tests", "run_all.py")],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=HERE,
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    files = re.search(r"(\d+)/(\d+) 파일 통과", p.stdout)
    # 파일 요약줄("N/N 파일 통과")은 제외하고 파일별 케이스 수만 합산
    cases = sum(int(m) for m in
                re.findall(r"(\d+)/\d+ (?!파일)", p.stdout))
    return p.returncode == 0 and files is not None, \
        (f"{files.group(0) if files else '파싱 실패'} · "
         f"개별 케이스 합계 {cases}")



# ==========================================================================
# 등록부
# ==========================================================================
#: 발표·문서에 **인용해도 되는** 문장. PASS = 지금 이 순간 재현됨.
def c_cap_degeneracy():
    """월말 캡이 소수 종목 구성에서 상한이 아니라 균등비중 복원으로 퇴화하는가.

    c_monthly_cap 과 짝이다. 앞 문장은 합성 12종목에서 '캡이 25%로 누른다'를
    보이고, 이 문장은 **우리 지수의 실제 구성**에서 그 보장이 성립하지 않음을
    보인다. 앞 문장만 인용하면 실측 최대비중 33.33%와 정면으로 충돌한다.
    순서와 꼬리표를 바꾸지 말 것.
    """
    import pandas as pd
    from analysis.cap_feasibility import is_degenerate
    from src.rebalance import ConfigV2
    cfg = ConfigV2()
    deg3 = all(is_degenerate(pd.Series(w), cfg)[1]
               for w in ([0.34, 0.33, 0.33], [0.50, 0.30, 0.20],
                         [0.40, 0.35, 0.25], [0.36, 0.32, 0.32]))
    _, deg5, _, mx5 = is_degenerate(pd.Series([0.45, 0.25, 0.15, 0.10, 0.05]), cfg)
    log = pd.read_csv(os.path.join(_BT_DIR, "event_log.csv"))
    cap = log[log["reason"] == "cap"]
    small = cap[cap["n_members"] <= 4]
    share = float(small["one_way_turnover"].sum() / log["one_way_turnover"].sum())
    ok = deg3 and (not deg5) and mx5 <= 0.25 + 1e-9 and len(small) >= 1
    return ok, (f"3종목 구성은 캡 발동 시 100% 균등비중으로 퇴화(최대비중 33.33%) · "
                f"5종목은 퇴화 없이 25%로 눌림 · 실측 캡 {len(cap)}건 중 "
                f"{len(small)}건이 5종목 미만 구간 = 전체 회전율의 {share:.1%}")


def c_concentration_realized():
    """전 구간 집중도가 확정 단면보다 훨씬 높은가(단면만 인용 금지)."""
    import pandas as pd
    from analysis.concentration_replication import concentration_history
    from analysis.run_backtest import load_snapshots
    from backtest.backtest import build_event_schedule
    from src.rebalance import ConfigV2
    snaps = load_snapshots(os.path.join(HERE, "data", "snapshots"))
    px = pd.read_csv(os.path.join(_BT_DIR, "..", "px.csv"),
                     index_col=0, parse_dates=True)
    px.columns = [str(c).zfill(6) for c in px.columns]
    px = px.dropna(axis=1, how="all")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        events, _ = build_event_schedule(px, snaps, cfg=ConfigV2.with_policy("mid"))
    groups = {pd.Timestamp(d): snaps[d].set_index("ticker")["group"] for d in snaps}
    daily = concentration_history(px, events, groups)
    eff_med = float(daily["유효종목수"].median())
    top3_med = float(daily["상위3비중"].median())
    under5 = float((daily["유효종목수"] < 5).mean())
    return eff_med < 3.5 and top3_med > 0.99, \
        (f"전 {len(daily)}거래일 유효종목수 중앙 {eff_med:.2f} · "
         f"상위3 비중 중앙 {top3_med:.2%} · 유효종목수 5 미만 {under5:.1%} "
         f"(2026-07-23 단면은 유효종목수 5.78 - 전 구간 중 가장 분산된 시점)")


def c_band_infeasible_with_cap():
    """버킷 밴드(안 C)의 기준점이 개별 캡과 산술적으로 양립하는가."""
    import pandas as pd
    path = os.path.join(_BT_DIR, "bucket_drift_reviews.csv")
    r = pd.read_csv(path)
    col = [c for c in r.columns if "앵커" in c and "실현" in c]
    ref = r[col[0]].astype(float)
    ceiling = 2 * 0.30                      # 앵커 2종목 x 캡 트리거 30%
    reachable = int((ref <= ceiling + 1e-12).sum())
    return reachable == 1, \
        (f"캡 하에서 지속 가능한 앵커 버킷 최대치 = 앵커 2종목 x 30% = "
         f"{ceiling:.0%} · 밴드 기준점(리셋 실현치) {sorted(set(ref.round(2)))} 중 "
         f"도달 가능한 회차는 {reachable}/{len(ref)}회뿐 - 나머지는 밴드와 캡이 "
         f"서로를 되돌리는 진동이 된다")


def c_pit_recollection():
    """DART 원문 215건 독립 재수집이 원장 근거와 전수 일치하는가.

    '원장이 형식상 완성됐다'와 '그 근거가 원문에서 재확인됐다'는 다른 사실이다.
    이 클레임은 뒤의 것만 검증한다 - 접수번호·접수일·보고서명·원문 해시의
    재현이며, 매출 노출도 같은 **내용 판정값의 재판정은 포함하지 않는다**
    (파트2 책임). 그 구분을 흐리면 "판정까지 검증했다"는 과장이 된다.

    알려진 예외 4건은 목록으로 고정한다. 새 예외가 생기면 즉시 FAIL 이
    되도록 - 그래야 이 검증이 다음에도 의미를 갖는다.
    """
    import glob
    import pandas as pd
    paths = sorted(glob.glob(os.path.join(HERE, "evidence", "recollect_*",
                                          "crosscheck.csv")))
    if not paths:
        return False, "재수집 대조 결과가 없다 - analysis/recollect_pit_evidence.py 미실행"
    cc = pd.read_csv(paths[-1], dtype={"ticker": str})
    v = cc["verdict"].value_counts().to_dict()
    ok_rows = int(v.get("MATCH", 0) + v.get("MATCH_RCPNO_DATE", 0))
    bad = int(v.get("MISMATCH", 0) + v.get("NOT_COLLECTED", 0)
              + v.get("ONLY_LEDGER", 0) + v.get("ONLY_RECOLLECT", 0))
    # 접수일 표기 상이 2건 (rcept_dt vs rcpNo 내장일)
    date_only = {(str(r.ticker).zfill(6), int(r.fiscal_year))
                 for r in cc[cc["verdict"] == "MATCH_RCPNO_DATE"].itertuples()}
    # 원문 HBM 언급 0회 (B안 정정 반영분 - 신규 발견 아님)
    flagged = {(str(r.ticker).zfill(6), int(r.fiscal_year))
               for r in cc[cc["flag_zero_hbm"].astype(str).str.lower()
                           == "true"].itertuples()}
    KNOWN_DATE = {("014680", 2020), ("067310", 2022)}
    KNOWN_ZERO = {("322310", 2024), ("322310", 2025)}
    ok = (ok_rows == 215 and bad == 0
          and date_only == KNOWN_DATE and flagged == KNOWN_ZERO)
    extra = (date_only - KNOWN_DATE) | (flagged - KNOWN_ZERO)
    return ok, (f"215건 재수집 · 일치 {ok_rows} · 불일치 {bad} · "
                f"접수일 표기 상이 {len(date_only)}건(기지) · "
                f"원문 HBM 0회 {len(flagged)}건(B안 정정 반영분) · "
                f"신규 예외 {len(extra)}건"
                + (f" {sorted(extra)}" if extra else ""))


def c_buffer_policy_canonical():
    """27/67 근거가 '합성 민감도 + 실측 무차이 + 반증 생존' 세 겹으로 성립하는가.

    이 셋은 **한 묶음**이다. 앞만 인용하면 실측(policy_comparison.csv)과
    충돌하고, 가운데만 인용하면 규칙이 죽은 것처럼 들린다. 그래서 하나의
    클레임으로 봉인한다 - 셋 중 하나라도 깨지면 발표 문장 전체가 무효다.

    정본은 **다중 seed 중앙값**이다. 단일 seed 는 seed 변동 범위 안의 한
    점일 뿐이므로 인용하지 않는다(문서 buffer_policy_canonical_20260731.md).
    """
    import pandas as pd
    order = ["none", "narrow", "mid", "wide"]

    # (1) 합성 민감도 - 다중 seed 중앙값
    syn = pd.read_csv(os.path.join(HERE, "analysis",
                                   "buffer_policy_sensitivity_v2.csv"))
    if syn["seed"].nunique() < 5:
        return False, f"seed 수 {syn['seed'].nunique()} - 단일·소수 seed 는 정본이 아니다"
    med = syn.groupby("정책")["연율화 회전율"].median().reindex(order)
    drop = med["mid"] / med["none"] - 1.0
    mono = bool(med.is_monotonic_decreasing)

    # (2) 실측 - 네 정책이 동일하고 버퍼 발동 0
    real = pd.read_csv(os.path.join(_BT_DIR, "policy_comparison.csv"),
                       index_col=0)
    tno = real["연율화회전율(편도)"].astype(float)
    same = float(tno.max() - tno.min()) < 1e-12
    fired = int(pd.to_numeric(real.get("버퍼발동 건수", 0),
                              errors="coerce").fillna(0).sum())

    # (3) 반증 - 1%p 충격에서 정책이 갈라진다
    fal = pd.read_csv(os.path.join(_BT_DIR, "buffer_falsification.csv"))
    hit = fal[pd.to_numeric(fal.iloc[:, 0], errors="coerce").eq(1.0)]
    split = False
    if len(hit):
        r = hit.iloc[0]
        split = (int(r.get("none 발동", 0)) == 0
                 and int(r.get("mid 발동", 0)) >= 1)

    ok = mono and same and fired == 0 and split
    return ok, (f"합성 다중 seed({syn['seed'].nunique()}개) 중앙 회전율 "
                f"none {med['none']:.1%} -> mid {med['mid']:.1%}({drop:+.1%}) "
                f"-> wide {med['wide']:.1%} · "
                f"실측 네 정책 동일({'예' if same else '아니오'}) "
                f"버퍼 발동 {fired}건 · "
                f"반증 1%p 충격에서 정책 분기({'예' if split else '아니오'})")


def c_ablation_layers():
    """규칙 층을 하나씩 켰을 때 각 층이 실제로 무엇을 하는가.

    세 사실을 함께 봉인한다 - 따로 인용하면 오독된다.
      (1) 규칙 C(위성군) 없이는 표본 앞 구간을 **산출조차 못 한다**
          (2023년까지 노출도 30% 통과 종목 0개 · 비앵커 0종목은 60% 배분 불가)
      (2) 버퍼 층의 차이가 **0** 이다(발동 0건과 일관)
      (3) 월간 캡이 CAGR 을 크게 **올린다** - 리스크 통제 장치인데 수익
          기여가 크고 MDD 는 악화된다. 도입 취지와 반대 방향이므로
          성과로 주장하지 않고 사실로만 보고한다.
    """
    import pandas as pd
    cum = pd.read_csv(os.path.join(_BT_DIR, "ablation_cumulative.csv"))
    loo = pd.read_csv(os.path.join(_BT_DIR, "ablation_leave_one_out.csv"))
    infeasible = cum[cum["CAGR"].isna()]["구성"].tolist()
    ok = cum[cum["CAGR"].notna()].reset_index(drop=True)
    if len(ok) < 3 or len(infeasible) < 2:
        return False, f"층 구성이 예상과 다르다 (산출가능 {len(ok)} · 불가 {len(infeasible)})"
    buf = float(ok.iloc[2]["CAGR"] - ok.iloc[1]["CAGR"])      # ④ - ③
    cap = float(ok.iloc[2]["CAGR"] - ok.iloc[1]["CAGR"])
    # ④(버퍼) - ③(규칙C) 는 0, ⑤(캡) - ④ 가 캡 효과
    buf = float(ok.iloc[1]["CAGR"] - ok.iloc[0]["CAGR"])
    cap = float(ok.iloc[2]["CAGR"] - ok.iloc[1]["CAGR"])
    d_mdd = float(ok.iloc[2]["MDD"] - ok.iloc[1]["MDD"])
    no_c = loo[loo["구성"].str.contains("규칙 C")]
    c_blocks = bool(len(no_c) and pd.isna(no_c.iloc[0]["CAGR"]))
    ok_all = (len(infeasible) >= 2 and abs(buf) < 1e-9 and cap > 0.05
              and d_mdd < 0 and c_blocks)
    return ok_all, (f"규칙 0·A 만으로는 산출 불가({len(infeasible)}개 층) · "
                    f"버퍼 층 CAGR 차이 {buf:+.4%}p · "
                    f"캡 층 CAGR {cap:+.2%}p · MDD {d_mdd:+.2%}p "
                    f"(캡은 수익을 올리고 위험은 악화시킨다)")


def c_regime_robustness():
    """구간을 나눠도 결론이 유지되는가 - 그리고 하락장은 방어되지 않는다.

    구간 경계는 외부 거시 사건으로 **사전 정의**했고 우리 지수 성과를 보고
    자르지 않았다(`regime_robustness.CALENDAR_REGIMES` 상수).

    이 클레임이 PASS 라는 것은 "모든 구간에서 좋았다"가 아니라 **"긴축
    구간의 손실을 포함해 구간별 수치가 재현된다"**는 뜻이다. 유리한 구간만
    인용하지 않기 위해 손실 구간의 존재를 조건에 넣는다.
    """
    import pandas as pd
    cal = pd.read_csv(os.path.join(_BT_DIR, "regime_calendar.csv"))
    cap = pd.read_csv(os.path.join(_BT_DIR, "regime_capture.csv"))
    seg = cal[cal["구간"] != "전 구간"]
    losers = seg[seg["기간수익률"] < 0]
    if len(seg) < 3:
        return False, f"구간이 {len(seg)}개뿐 - 사전 정의 목록을 확인할 것"
    if len(losers) == 0:
        return False, ("손실 구간이 하나도 없다 - 구간 정의가 유리하게 "
                       "잘렸는지 확인할 것")
    up = cap[cap["구분"].str.contains("상승")].iloc[0]
    dn = cap[cap["구분"].str.contains("하락")].iloc[0]
    asym = float(dn["포착률"]) < float(up["포착률"])
    worst = losers.sort_values("기간수익률").iloc[0]
    return True, (f"사전 정의 {len(seg)}구간 · 손실 구간 {len(losers)}개 "
                  f"(최악 {worst['구간'][:12]} {worst['기간수익률']:.1%}) · "
                  f"포착률 상승 {up['포착률']:.3f} / 하락 {dn['포착률']:.3f}"
                  f"{' (비대칭 유리)' if asym else ' (비대칭 불리)'}")


CLAIMS = [
    ("기준일 2020-06-15는 임의 상수가 아니라 3장 일정 조문의 산출값이다",
     c_base_date, "analysis/index_calendar.py", "실측(조문 재생)"),
    ("정기변경 지수 산출은 두 독립 경로가 부동소수점 한계까지 일치한다",
     c_regular_equivalence, "src/index_calc.py", "실측(수치 검증)"),
    ("수시편출의 '정규화 = 제수 흡수'는 두 파트 구현으로 상호 실증됐다",
     c_adhoc_equivalence, "tests/test_index_calc_series.py", "실측(수치 검증)"),
    ("TR 지수는 제수 보정 경로와 배당 재투자 경로가 일치한다",
     c_tr_equivalence, "tests/test_tr_equivalence.py", "실측(수치 검증)"),
    ("월말 30% 캡은 상시 가동되며 5종목 이상 구성에서 쏠림을 25%로 누른다",
     c_monthly_cap, "analysis/audit_review_claims.py", "엔진 실측(합성 가격)"),
    # 아래 두 줄은 짝이다. 앞은 술식의 성질(합성 가격), 뒤는 우리 지수의 수치
    # (실측). 앞 문장만 인용하면 "드리프트가 원인"으로 읽히는데, 실측에서는
    # 드리프트가 소수 성분이다. 순서와 꼬리표를 바꾸지 말 것.
    ("개별 캡은 버킷 레벨을 보지 않는다(술식 성질 - 합성 가격 시연)",
     c_bucket_drift, "analysis/audit_review_claims.py", "엔진 실측(합성 가격)"),
    ("실지수에서 앵커 40%는 13회 중 1회만 실현됐고 괴리는 대부분 구조 성분이다"
     "(위원회 상정 사안)",
     c_bucket_mandate_real, "analysis/bucket_drift.py", "실측(스냅샷 13회·전 거래일)"),
    ("월말 캡은 5종목 미만 구성에서 상한이 아니라 균등비중 복원으로 퇴화한다"
     "(위원회 상정 사안)",
     c_cap_degeneracy, "analysis/cap_feasibility.py",
     "실측(술식 + 이벤트 원장)"),
    ("전 구간 집중도는 확정 단면보다 크게 높다 - 유효종목수 중앙 3.00",
     c_concentration_realized, "analysis/concentration_replication.py",
     "실측(전 거래일)"),
    ("버킷 밴드 기준점은 개별 캡과 산술적으로 양립하지 않는다(안 C 보류 근거)",
     c_band_infeasible_with_cap, "analysis/bucket_band_turnover.py",
     "실측(산술 + 리셋 실현치)"),
    ("27/67 근거는 합성 민감도이며 실측에서는 네 정책이 동일하다"
     "(버퍼 발동 0건 - 규칙은 1%p 충격에서 분기해 살아 있다)",
     c_buffer_policy_canonical, "docs/buffer_policy_canonical_20260731.md",
     "합성(다중 seed) + 실측 + 반증"),
    ("규칙 C 없이는 지수 산출 자체가 불가능하고, 월간 캡은 수익을 올리며 위험을 악화시킨다"
     "(층별 ablation · 위원회 상정 사안)",
     c_ablation_layers, "analysis/ablation_study.py", "실측(층별 재생)"),
    ("사전 정의 시장 구간별로 재현되며 긴축 구간에서는 손실이다"
     "(하락장 방어 안 됨 · 포착률은 비대칭)",
     c_regime_robustness, "analysis/regime_robustness.py",
     "실측(외부 사건 기준 구간)"),
    ("고정 % 상한은 ADV에 따라 소요일수가 수십 배 달라져 안전장치가 못 된다",
     c_capacity_inverse, "analysis/capacity_v2.py", "산식 실측(가정 ADV)"),
    ("오늘 판정값을 과거에 복사하면 편출입이 0이 되어 버퍼 비교가 불가능하다",
     c_pit_vs_frozen, "analysis/demo_why_pit.py", "구조 논증(합성 시나리오)"),
    ("PIT 규율은 형식이 아니라 실제 편입 결과를 바꾼다",
     c_pit_changes_outcome, "tests/test_pit_snapshots.py", "실측(단위 검증)"),
    ("2026-07-23 확정 33종목 판정은 7종목 구성과 공표 비중으로 재현된다",
     c_judgment_snapshot, "analysis/verify_judgment_snapshot.py",
     "실측(한 시점 교차검증)"),
    ("역사적 PIT 판정 원장은 strict FINAL 215행이며 NO_DATA 8행은 별도 보존된다",
     c_final_ledger, "data/verdict_ledger.csv", "실측(입력계약 검증)"),
    ("DART 원문 215건 독립 재수집이 원장 근거와 전수 일치한다(계보 METADATA_VERIFIED)",
     c_pit_recollection, "evidence/recollect_20260731/crosscheck.csv",
     "실측(독립 재수집 전수 대조)"),
    ("관리종목 이력 조사는 공식 KIND 0350 정확종목 질의와 양성 대조로 재현된다",
     c_kind_admin_history, "evidence/kind_admin_history_20260730/run_manifest.json",
     "실측(공식 원응답 교차검증)"),
    ("생존편향은 소멸 종목 전수 조사로 후보 0건이 확인됐다(크기는 미측정)",
     c_survivorship_survey, "evidence/survivorship/", "실측(상장 명단 대조)"),
    ("PR/TR 병기가 가능하며 배당 기여도는 재현된다(배당락일 근사 의존)",
     c_pr_tr_parallel, "out/backtest_tr/index_level_pr_tr.csv",
     "실측(가정 명시 · 보조 비교)"),
    ("실측 ADV60 기준 정기변경 소화 일수가 리밸런싱 주기에 육박한다",
     c_capacity_measured, "data/adv60.csv", "실측(ADV60 1시점 · 정기·캡)"),
    ("전 테스트 스위트가 통과한다",
     c_test_suite, "tests/run_all.py", "실측(실행)"),
]

# (실측 백테스트 클레임은 c_backtest_selfconsistent 정의 뒤에서 조건부로
#  등록한다 - forbidden_rows() 아래 참조)

#: 감사 '결과'. 문장이 결함을 주장하므로 PASS = **결함이 재현됨**을 뜻한다.
#: CLAIMS 와 섞으면 "발표에 쓸 문장" 표에 결함이 끼고, 결함을 고치는 순간
#: FAIL 로 바뀌어 회귀처럼 보인다. 그래서 분리한다.
AUDITS = [
    ("폐기된 구 대시보드 표시 비중은 엔진에서 재현되지 않고 조문 상한을 위반한다",
     c_dashboard_weights, "analysis/audit_dashboard_numbers.py",
     "과거 결함 보존 재현 - 현재 app.py 수치가 아님"),
]

#: 등록되지 않았으므로 **인용 금지**인 수치 - 발표·문서·화면 어디에도 쓰지 않는다
#: (백테스트 성과 수치는 forbidden_rows() 가 실행 상태에 따라 동적으로 얹는다
#:  - 잠정 실행 단계에서는 금지 유지, FINAL 매니페스트가 있어야 해제)
FORBIDDEN = [
    ("종목별 ADV60 · 유동시총 시계열",
     "pykrx 수집 미실행 (2026-07-23 1회분·PIT 스냅샷 13회분 제외)"),
    # 조사 범위·후보 수는 이제 재현되는 클레임이라 말할 수 있다(c_survivorship_survey).
    # 그러나 **크기**는 여전히 못 잰다 - 후보가 0건이면 재실행할 대상이 없어
    # 차이를 측정할 방법 자체가 없기 때문이다. '조사했다'와 '크기를 안다'는
    # 다른 말이므로 이 항목은 남긴다.
    ("생존편향 크기 (1.5~3.0%p 등)",
     "크기 미측정 - 조사 범위·후보 0건은 고지 가능하나 크기는 산출 불가"),
]


# --------------------------------------------------------------------------
# 실측 백테스트 상태 - FINAL 매니페스트 게이트
# --------------------------------------------------------------------------
_BT_DIR = os.path.join(HERE, "out", "backtest")
_SNAPSHOT_DIR = os.path.join(HERE, "data", "snapshots")


def _sha_file(path):
    import hashlib as _h
    return _h.sha256(open(path, "rb").read()).hexdigest().upper()


def _git_head():
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=HERE, capture_output=True,
            text=True, timeout=10)
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None


#: FINAL 이후에도 바뀌면 안 되는 코드 경로. run_backtest_final.ps1 의 7단계
#: 동결 커밋은 산출물(data/snapshots · out/ · docs/FACTSHEET.md)만 stage 하므로
#: 이 목록과 겹치지 않는다.
_CODE_PATHS = ("src", "analysis", "backtest", "tests", "app.py",
               "run_backtest_final.ps1", "requirements.txt")


def _code_changed_between(a: str, b: str) -> bool:
    """두 커밋 사이에 코드가 바뀌었는가. 확인 불가면 '바뀐 것'으로 본다(fail-closed).

    왜 커밋 동일성이 아니라 코드 차이를 보는가
    -----------------------------------------
    과거에는 매니페스트의 커밋이 현재 HEAD 와 **정확히 같을 것**을 요구했다.
    그런데 확정 실행의 마지막 단계가 산출물을 동결 커밋하므로, 성공한 실행은
    자기 손으로 HEAD 를 옮겨 방금 만든 FINAL 매니페스트를 무효로 만든다.
    수치가 풀린 상태가 단 한 순간(동결 커밋 직전)에만 존재하는 셈이라, 그 뒤
    누가 검증하든 항상 '잠정'으로 돌아가 있다.

    막으려던 것은 '커밋이 다른 것'이 아니라 **산출 이후 코드가 바뀌는 것**이다.
    그래서 그 쪽을 직접 검사한다 - 산출물만 담은 동결 커밋은 통과하고, 코드가
    한 줄이라도 바뀌면 FINAL 이 무효가 된다. 데이터 무결성은 기존의 입력·
    스냅샷·산출물 해시 대조가 그대로 담당하므로 약해지지 않는다.
    """
    if not a or not b:
        return True
    if a == b:
        return False
    try:
        r = subprocess.run(["git", "diff", "--name-only", a, b, "--",
                            *_CODE_PATHS],
                           cwd=HERE, capture_output=True, text=True, timeout=20)
    except Exception:
        return True
    if r.returncode != 0:                    # 알 수 없는 커밋·저장소 아님 등
        return True
    return bool(r.stdout.strip())


def _final_manifest_valid(m) -> bool:
    """FINAL 매니페스트의 게이트·해시가 현재 파일과 전부 일치해야 해제된다.

    산출 CSV·스냅샷·원장 어느 하나라도 매니페스트와 다르면(누락 포함)
    FINAL 로 인정하지 않는다 - 사후 변조 시 수치가 자동으로 다시 잠긴다.
    """
    from analysis.make_backtest_manifest import GATE_KEYS

    gates = m.get("gates") or {}
    if set(gates) != set(GATE_KEYS) or not all(
            all(str(gates[key].get(field, "")).strip()
                for field in ("value", "by", "on"))
            for key in GATE_KEYS):
        return False
    if str(gates["d1_index_asof"]["value"]).strip() \
            != str(m.get("index_asof", "")).strip():
        return False
    if not all(re.fullmatch(r"\d{4}-\d{2}-\d{2}",
                            str(gates[key]["on"]).strip())
               for key in GATE_KEYS):
        return False
    snapshot_commit = str(m.get("code_commit_snapshots") or "").strip()
    manifest_commit = str(m.get("code_commit_now") or "").strip()
    if not snapshot_commit or snapshot_commit != manifest_commit:
        return False
    # HEAD 와의 관계는 '같은 커밋인가'가 아니라 '그 뒤 코드가 바뀌었는가'로 본다.
    # (7단계 동결 커밋이 스스로를 무효화하던 문제 - _code_changed_between 참조)
    if _code_changed_between(manifest_commit, _git_head()):
        return False
    try:
        outputs = m.get("outputs") or {}
        snapshots = m.get("snapshots") or {}
        inputs = m.get("inputs") or {}
        if not outputs or not snapshots or not inputs:
            return False
        for name, want in outputs.items():
            if os.path.basename(name) != name:
                return False
            p = os.path.join(_BT_DIR, name)
            if not os.path.exists(p) or _sha_file(p) != str(want).upper():
                return False
        for name, want in snapshots.items():
            if os.path.basename(name) != name:
                return False
            p = os.path.join(_SNAPSHOT_DIR, name)
            if not os.path.exists(p) or _sha_file(p) != str(want).upper():
                return False
        for name, want in inputs.items():
            p = os.path.abspath(os.path.join(
                HERE, name.replace("/", os.sep)))
            if os.path.commonpath((HERE, p)) != HERE:
                return False
            if not os.path.exists(p) or _sha_file(p) != str(want).upper():
                return False
    except OSError:
        return False
    return True


def _backtest_status():
    """(status, manifest) - status: 'none' | 'provisional' | 'final'.

    FINAL 은 make_backtest_manifest.py --final 이 게이트 5건(fail-closed)을
    통과해 생성한 backtest_run_manifest_FINAL.json 이 있을 때만 인정한다.
    """
    import glob as _glob
    import json as _json
    best = ("none", None)
    for p in sorted(_glob.glob(
            os.path.join(_BT_DIR, "backtest_run_manifest*.json"))):
        try:
            m = _json.loads(open(p, encoding="utf-8-sig").read())
        except Exception:
            continue
        rt = str(m.get("run_type", ""))
        if rt == "FINAL_BACKTEST":
            if _final_manifest_valid(m):
                return "final", m
            # 게이트·해시 불일치 FINAL 은 무효 - 변조·구버전 산출물로는
            # 수치가 풀리지 않는다 (잠정 매니페스트가 있으면 잠정으로 강등)
        elif rt.startswith("PROVISIONAL"):
            best = ("provisional", m)
    return best


def _backtest_metrics():
    """index_level.csv 에서 성과 지표를 독립 재계산한다."""
    bt = pd.read_csv(os.path.join(_BT_DIR, "index_level.csv"),
                     index_col=0, parse_dates=True)
    lv = bt["level"].astype(float)
    yrs = (lv.index[-1] - lv.index[0]).days / 365.25
    cum = lv.iloc[-1] / lv.iloc[0] - 1
    cagr = (lv.iloc[-1] / lv.iloc[0]) ** (1 / yrs) - 1
    vol = float(lv.pct_change().std()) * float(np.sqrt(252))
    mdd = float((lv / lv.cummax() - 1).min())
    to_total = float(bt["turnover"].astype(float).sum())
    return {"level": float(lv.iloc[-1]), "cum": float(cum),
            "cagr": float(cagr), "vol": vol, "mdd": mdd,
            "turnover_total": to_total, "years": yrs}


def c_backtest_selfconsistent():
    """실측 백테스트 산출물의 자기일관성 + 입력 계보를 재검증한다."""
    status, m = _backtest_status()
    x = _backtest_metrics()
    ok = all(np.isfinite(v) for v in
             (x["cum"], x["cagr"], x["vol"], x["mdd"], x["turnover_total"]))
    notes = []
    if m:
        import hashlib as _h
        want = (m.get("inputs") or {}).get("data/verdict_ledger.csv")
        if want:
            got = _h.sha256(open(os.path.join(
                HERE, "data", "verdict_ledger.csv"), "rb").read()) \
                .hexdigest().upper()
            same = got == str(want).upper()
            ok = ok and same
            notes.append("원장 해시 " + ("일치" if same else "불일치"))
        if m.get("code_commit_snapshots"):
            notes.append(f"스냅샷 커밋 {str(m['code_commit_snapshots'])[:7]}")
    if status == "final":
        det = (f"레벨 {x['level']:,.2f} · 누적 {x['cum']:.4f} · "
               f"CAGR {x['cagr']:.4f} · 변동성 {x['vol']:.4f} · "
               f"MDD {x['mdd']:.4f}")
    else:
        det = ("재계산 5지표 유한·정합 - 수치는 FINAL 게이트"
               "(D1·D2·D3·판정 추인 2건) 통과 후 공개")
    if notes:
        det += " · " + " · ".join(notes)
    return ok, det


def forbidden_rows():
    """실행 상태를 반영한 인용 금지 목록 (동적 + 정적)."""
    status, _ = _backtest_status()
    rows = []
    if status == "none":
        rows += [("지수 레벨/누적수익률/CAGR/변동성/MDD/회전율",
                  "실측 백테스트 미실행"),
                 ("벤치마크 수익률·상관계수·추적오차",
                  "벤치마크 지수를 조회한 적 없음")]
    elif status == "provisional":
        rows += [("지수 레벨/누적수익률/CAGR/변동성/MDD/회전율 (잠정 실측치 일체)",
                  "잠정 실행 완료 - FINAL 게이트(D1·D2·D3·판정 추인 2건) 통과 "
                  "후 make_backtest_manifest.py --final 로 해제"),
                 ("벤치마크 수익률·상관계수·추적오차",
                  "벤치마크 PROVISIONAL - resolver·CONFIRMED 반영 재실행 전")]
    else:  # final
        if not os.path.exists(os.path.join(_BT_DIR, "benchmark_inference.csv")):
            rows += [("벤치마크 수익률·상관계수·추적오차",
                      "FINAL 실행에 벤치마크 미포함 - CONFIRMED 반영 재실행 필요")]
    return rows + FORBIDDEN


# 실측 백테스트 산출물이 존재할 때만 등록되는 클레임. 잠정 단계에서는
# 재현값에 수치를 노출하지 않으며(인용 금지 유지), FINAL 매니페스트가
# 생성되면 같은 클레임이 수치를 공개한다 - 등록부 원칙과 게이트를 양립시킨다.
if os.path.exists(os.path.join(_BT_DIR, "index_level.csv")):
    CLAIMS.append(
        ("실측 백테스트 산출물은 지표 재계산·입력 계보와 자기일관적으로 재현된다",
         c_backtest_selfconsistent, "out/backtest/index_level.csv",
         "실측(산출물 자기검증)"))

#: 실제로 유출된 적이 있는 구체 수치 - 문서·화면에서 자동으로 잡아낸다.
#: "규칙을 문서에 썼다"와 "규칙이 지켜지는지 기계가 본다"는 다르다.
FORBIDDEN_LITERALS = [
    ("48.5", "회전율 (미실측)"), ("24.6", "회전율 (미실측)"),
    ("38.2", "회전율 (미실측)"), ("18.2", "회전율 (미실측)"),
    ("20.3", "CAGR (미실측)"), ("21.6", "CAGR (미실측)"),
    ("22.4", "CAGR (미실측)"), ("20.9", "CAGR (미실측)"),
    ("21.3", "CAGR (미실측)"),
    ("3,161.9", "지수 레벨 (미실측)"), ("3161.9", "지수 레벨 (미실측)"),
    ("218.0", "누적수익률 (미실측)"), ("216.2", "누적수익률 (미실측)"),
    ("0.89", "샤프지수 (미실측)"), ("-25.4", "MDD (미실측)"),
    ("-34.1", "벤치마크 MDD (미실측)"), ("9.3", "벤치마크 CAGR (미실측)"),
    ("23.1", "변동성 (미실측)"), ("26.8", "벤치마크 변동성 (미실측)"),
]

#: 이 표현이 같은 줄에 있으면 "인용"이 아니라 "인용 금지 표시"로 본다.
#: 줄 전체를 스킵하므로 **좁고 명시적인 표현만** 둔다. '출처·사고·오류·위반'
#: 같은 흔한 단어를 넣으면(예: "실측 회전율 24.6% (출처: 사내 백테스트)")
#: 진짜 유출이 든 줄까지 통째로 면제돼 스캐너가 무력해진다.
NEGATION = ("금지", "쓰지", "미실측", "미측정", "미실행", "미실시", "미작성",
            "적 없음", "합성", "예시", "가짜", "재현되지", "무너",
            "창작", "\u26a0\ufe0f", "FORBIDDEN", "지어낸")


#: 감사 문서는 금지 수치를 '이건 쓰면 안 된다'는 예시로 정당하게 인용한다.
#: 그 구간은 아래 마커로 명시적으로 꺼 둔다 - 자동 추측보다 명시가 안전하다.
SCAN_OFF, SCAN_ON = "<!-- scan: off -->", "<!-- scan: on -->"

_MINUS = "\u2212"   # 수학용 유니코드 마이너스도 ASCII '-' 로 정규화한다
#: 숫자 토큰 - 천단위 콤마 그룹 또는 일반 소수(부호 포함). 숫자 경계에서만 시작해
#: '29.32' 안의 '9.32' 같은 부분매칭을 배제한다.
_NUM_RE = re.compile(r"(?<![\d.,])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")


def _provisional_literals():
    """잠정 백테스트 실측치의 유출을 스캐너로 잡기 위한 동적 금지 수치.

    FINAL 게이트 전에는 잠정 수치도 발표·문서에 쓸 수 없으므로, 산출물에서
    직접 파생한 대표 표기(6자리·% 환산·지수 레벨)를 금지 목록에 얹는다.
    FINAL 이 되면 자동으로 비워져 해제된다. 값이 짧아 오탐 위험이 있는
    반올림(1.4·64.6 등)은 넣지 않는다 - 구체 표기만 잡는다.
    """
    status, _ = _backtest_status()
    if status != "provisional":
        return []
    try:
        x = _backtest_metrics()
    except Exception:
        return []
    lits = [
        (f"{x['cum']:.6f}", "잠정 누적수익률"),
        (f"{x['cum'] * 100:.1f}", "잠정 누적수익률(%)"),
        (f"{x['cagr']:.6f}", "잠정 CAGR"),
        (f"{x['cagr'] * 100:.2f}", "잠정 CAGR(%)"),
        (f"{x['vol']:.6f}", "잠정 변동성"),
        (f"{x['vol'] * 100:.2f}", "잠정 변동성(%)"),
        (f"{x['mdd']:.6f}", "잠정 MDD"),
        (f"{x['mdd'] * 100:.2f}", "잠정 MDD(%)"),
        (f"{x['level']:.2f}", "잠정 지수 레벨"),
        (f"{x['level']:,.2f}", "잠정 지수 레벨"),
    ]
    return [(lit, what + " (FINAL 게이트 전 인용 금지)") for lit, what in lits]


def _forbidden_values():
    """FORBIDDEN_LITERALS(+동적 잠정 수치)를 float 로 정규화한
    (값, 표기, 설명) 목록. '3,161.9' 와 '3161.9' 처럼 값이 같은 항목은
    하나로 합친다(첫 표기 유지)."""
    seen, out = set(), []
    for lit, what in FORBIDDEN_LITERALS + _provisional_literals():
        v = float(lit.replace(",", ""))
        if v in seen:
            continue
        seen.add(v)
        out.append((v, lit, what))
    return out


_FORBIDDEN_VALUES = _forbidden_values()


def _numbers_in(line: str):
    """줄에서 숫자 토큰을 float 로 뽑는다(수학용 마이너스 정규화 포함)."""
    for m in _NUM_RE.finditer(line.replace(_MINUS, "-")):
        try:
            yield float(m.group().replace(",", ""))
        except ValueError:
            continue


def find_forbidden(line: str) -> list:
    """줄에 든 인용 금지 수치를 [(표기, 설명), ...]로 돌려준다.

    **값**으로 비교하므로 자리수 변형(24.6 vs 24.60)도 잡고, 부분일치(29.32
    속 9.3)는 숫자 토큰 단위 비교라 자연히 배제된다. 과거 문자열 정규식은
    뒤에 숫자가 붙은 '24.60'을 놓쳤다(자리수만 바꾸면 유출이 통과).

    음수 금지수치(MDD -25.4·벤치마크 -34.1)는 **절댓값 표기도 잡는다**: 낙폭은
    'MDD 25.4%'처럼 부호 없이 쓰는 일이 흔해, 부호만 떼면 유출이 통과하던
    구멍을 막는다(양수 금지수치는 그대로 부호 일치 비교)."""
    vals = list(_numbers_in(line))
    hits = []
    for v, lit, what in _FORBIDDEN_VALUES:
        targets = (v, -v) if v < 0 else (v,)   # 음수는 절댓값 표기도 금지
        if any(abs(t - x) < 1e-9 for t in targets for x in vals):
            hits.append((lit, what))
    return hits


def scan_forbidden(paths: list) -> pd.DataFrame:
    """문서에서 인용 금지 수치를 찾아낸다(발표 자료 최종 점검용).

    제외 규칙 두 가지 - 둘 다 '경보가 울려도 무시하게 되는 상태'를 막는다.
      1) 같은 줄에 부정 표현이 있으면(…는 미실측이므로 쓰지 않는다) 인용이 아니다.
      2) `<!-- scan: off -->` ~ `<!-- scan: on -->` 구간은 건너뛴다.
         감사 문서가 금지 수치를 예시로 드는 구간에 쓴다.
    """
    hits = []
    for path in paths:
        if not os.path.exists(path):
            hits.append({"파일": path, "줄": 0, "수치": "-",
                         "내용": "파일 없음"})
            continue
        on = True
        for i, line in enumerate(open(path, encoding="utf-8"), 1):
            if SCAN_OFF in line:
                on = False
                continue
            if SCAN_ON in line:
                on = True
                continue
            if not on or any(n in line for n in NEGATION):
                continue
            found = find_forbidden(line)
            if found:
                lit, what = found[0]
                hits.append({"파일": os.path.basename(path), "줄": i,
                             "수치": f"{lit} ({what})",
                             "내용": line.strip()[:70]})
    return pd.DataFrame(hits)


FACTSHEET_HEAD = """# 발표 팩트시트 - 지금 말할 수 있는 것 / 없는 것

> **이 파일은 `python analysis/verify_claims.py --factsheet-out docs/FACTSHEET.md`
> 가 생성합니다. 손으로 고치지 마십시오** - 고치면 다음 생성 때 사라지고,
> 무엇보다 "재현된 값"이라는 이 문서의 유일한 존재 이유가 없어집니다.

## 왜 이 문서가 필요한가

<!-- scan: off -->
같은 사고가 세 번 반복됐습니다.

1. 채팅으로 "회전율 48.5% → 24.6%, CAGR 20.3% → 21.6%" 가 들어옴
2. 대시보드가 그 위에 지수 3,161.9pt · Sharpe 0.89 · MDD -25.4% 를 실음
3. 종목별 노출도 · ADV60 · 벤치마크까지 실측처럼 표시됨
<!-- scan: on -->

셋 다 원인이 같습니다 - **출처 없는 숫자가 문서·화면에 들어가는 것을 막는
장치가 없었습니다.** 매번 사후에 잡아내는 대신, 규칙 하나로 막습니다.

> **규칙: `verify_claims.py` 에 재현 함수가 등록된 수치만 인용한다.**
> 새 수치를 쓰고 싶으면 먼저 재현 함수를 추가하십시오. 추가할 수 없으면
> 그 수치는 아직 존재하지 않는 것입니다.

발표 전 체크:

```powershell
python analysis/verify_claims.py            # 전부 PASS 여야 함
python analysis/verify_claims.py --scan 발표자료.md   # 금지 수치 유출 점검
```
"""

FACTSHEET_TAIL = """
## 성격 표기의 의미

| 표기 | 검증한 것 | 발표에서 |
|---|---|---|
| **실측(수치 검증)** | 두 독립 구현이 같은 답을 내는가 | 그대로 인용 가능 |
| **실측(조문 재생)** | 방법론 문장만으로 코드가 값을 재생하는가 | 그대로 인용 가능 |
| **실측(단위 검증)** | 규율이 결과를 바꾸는가 | 그대로 인용 가능 |
| **실측(한 시점 교차검증)** | 확정 단면 판정·비중이 엔진에서 재현되는가 | 해당 기준일에 한해 인용 가능 |
| **엔진 실측(합성 가격)** | 엔진이 조문대로 동작하는가 | "합성 가격으로 엔진 동작을 확인했다" |
| **산식 실측(가정 ADV)** | 산식이 맞는가 | "ADV를 가정하면" 을 반드시 붙일 것 |
| **구조 논증(합성 시나리오)** | 구조적으로 그럴 수밖에 없는가 | 숫자보다 구조를 말할 것 |

**"엔진 실측(합성 가격)"의 뜻**: 검증한 것은 **엔진이 조문대로 동작하는가**
이지 **시장에서 실제로 그랬는가**가 아닙니다. "캡 OFF 47.66% → ON 25.00%"는
"우리 캡 로직이 작동한다"는 증거이지 "HBM 지수가 실제로 47%까지 갔었다"는
뜻이 아닙니다. **이 구분이 흐려진 것이 위 3번 사고의 정확한 메커니즘입니다.**

---

## 그래도 발표는 충분히 강합니다

위 문장들만으로 말할 수 있는 것:

- **조문이 코드로 재생됩니다.** 기준일조차 상수가 아니라 일정 조문의 산출값입니다.
- **파트 간 접합이 실증됐습니다.** "동치가 되도록 설계했다"가 아니라
  "두 사람의 구현을 대조해 1e-15 수준으로 일치함을 보였다"입니다.
- **PR/TR 이중 계열이 수학적으로 검증됐습니다.** 배당 데이터만 들어오면 바로 산출됩니다.
- **회전율 억제 장치가 작동합니다.** 그리고 그것이 못 잡는 것(버킷 드리프트)까지
  스스로 찾아 위원회 안건으로 올렸습니다.
- **유동성 상한을 감이 아니라 역산으로 다룹니다.** "5%가 안전한가"에
  "ADV 15억이면 100거래일"이라고 답할 수 있습니다.
- **2026-07-23 판정은 실데이터로 재현했습니다.** 33종목에서 7종목을
  재선정하고 비중까지 공표 반올림 범위로 맞췄습니다.
- **look-ahead를 구조적으로 차단했고, 그 규율이 결과를 바꾼다는 것까지 보였습니다.**

성과 수치를 아직 공개하지 않아도 **방법론의 완성도**는 이것으로 증명됩니다.
역사적 PIT 원장(FINAL 215행)으로 전구간 잠정 백테스트까지 실행·자기검증을
마쳤고, 위원회 게이트(D1 기준일·D2 벤치마크·D3 수시변경·판정 추인 2건)가
닫히기 전에는 수치를 공개하지 않는 규율이 그대로 작동하고 있습니다.

---

## 인용 금지 수치를 쓰고 싶어지면 (해제 절차)

백테스트 성과 수치의 해제는 손으로 목록을 지우는 것이 아니라 **게이트로**
합니다. ① `data/final_run_gates.json` 에 5개 게이트(D1 기준일·D2 벤치마크·
D3 수시변경·판정 추인 2건)의 value/by/on 을 기입하고 ② `run_backtest_final.ps1`
로 확정 실행하면 `make_backtest_manifest.py --final` 이 FINAL 매니페스트를
생성하고, 이 등록부가 자동으로 수치를 공개 전환합니다. 게이트가 하나라도
비어 있으면 FINAL 은 생성되지 않습니다(fail-closed).

그 외의 새 수치는 지금처럼 재현 함수를 먼저 추가하십시오. 그 순서를
지키면 이 사고는 다시 나지 않습니다.
"""


def _run(entries):
    rows, failed = [], 0
    for text, fn, src, kind in entries:
        try:
            ok, detail = fn()
        except Exception as e:                       # 재현 실패도 결과다
            ok, detail = False, f"재현 중 예외: {type(e).__name__}: {e}"
        rows.append({"문장": text, "결과": "PASS" if ok else "FAIL",
                     "재현값": detail, "근거": src, "성격": kind})
        failed += (not ok)
    return rows, failed


def _md_table(rows, header="발표에 쓸 수 있는 문장"):
    out = [f"| {header} | 재현값 | 근거 파일 | 성격 |", "|---|---|---|---|"]
    for r in rows:
        mark = "" if r["결과"] == "PASS" else "[FAIL] 사용 금지: "
        out.append(f"| {mark}{r['문장']} | {r['재현값']} | `{r['근거']}` | "
                   f"{r['성격']} |")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", action="store_true", help="마크다운 표만 출력")
    ap.add_argument("--factsheet", action="store_true",
                    help="FACTSHEET.md 전체를 생성해 표준출력으로")
    ap.add_argument("--factsheet-out", default=None,
                    help="FACTSHEET를 UTF-8 파일로 직접 저장(PowerShell 리다이렉션 회피)")
    ap.add_argument("--scan", nargs="*", default=None,
                    help="문서에서 인용 금지 수치 유출 점검")
    ap.add_argument("--fast", action="store_true", help="테스트 스위트 생략")
    a = ap.parse_args()

    if a.scan is not None:
        hits = scan_forbidden(a.scan)
        print("=" * 78)
        print("인용 금지 수치 유출 점검")
        print("=" * 78)
        if len(hits):
            print(hits.to_string(index=False))
            print(f"\n{len(hits)}건 발견. 각 줄이 정말 인용인지 확인하고, "
                  "인용이면 삭제하거나 '미실측' 표기를 붙이십시오.")
            print("부정 문맥(금지·미실측·합성 등)이 같은 줄에 있으면 자동 제외됩니다.")
            return 1
        print("유출 없음.")
        return 0

    if a.factsheet_out:
        a.factsheet = True
    claims = [c for c in CLAIMS if not (a.fast and c[1] is c_test_suite)]
    rows, failed = _run(claims)
    arows, afailed = _run(AUDITS)

    if a.factsheet or a.md:
        body = (_md_table(rows) + "\n\n"
                + "**인용 금지 (미등록 수치)**\n\n"
                + "| 수치 | 왜 못 쓰는가 |\n|---|---|\n"
                + "\n".join(f"| {n} | {w} |" for n, w in forbidden_rows()) + "\n\n"
                + "**감사 결과 (결함 재현 - 발표 문장이 아님)**\n\n"
                + _md_table(arows, "감사 항목"))
        if a.factsheet:
            from analysis.index_calendar import as_of_today
            rendered = (
                FACTSHEET_HEAD
                + f"\n---\n\n## 검증된 문장 "
                  f"(생성 {as_of_today().date()} · {len(rows) - failed}/"
                  f"{len(rows)} 재현 성공)\n\n"
                + body
                + "\n"
                + FACTSHEET_TAIL
            )
            if a.factsheet_out:
                os.makedirs(os.path.dirname(a.factsheet_out) or ".", exist_ok=True)
                with open(a.factsheet_out, "w", encoding="utf-8", newline="\n") as f:
                    f.write(rendered)
                print(f"[OK] FACTSHEET 저장: {a.factsheet_out}")
            else:
                print(rendered)
        else:
            print(body)
        return 1 if failed else 0

    print("=" * 78)
    print("발표 문장 재현 검증 - 통과한 문장만 쓸 수 있다")
    print("=" * 78)
    for r in rows:
        print(f"\n[{r['결과']}] {r['문장']}")
        print(f"        재현값: {r['재현값']}")
        print(f"        근거  : {r['근거']}  ({r['성격']})")
    print("\n" + "-" * 78)
    print("[감사 결과 - 결함이 재현되는지 확인. 발표 문장이 아님]")
    for r in arows:
        print(f"  [{r['결과']}] {r['문장']}")
        print(f"          {r['재현값']}")
    print("\n" + "=" * 78)
    print(f"{len(rows) - failed}/{len(rows)} 발표 문장 재현 성공"
          + ("" if not failed else f" - {failed}개 실패, 해당 문장 사용 금지"))
    print("\n[인용 금지 - 아래 수치는 어떤 문서·화면에도 쓰지 않는다]")
    for n, why in forbidden_rows():
        print(f"  · {n:38s} {why}")
    print("\n문서 점검:  python analysis/verify_claims.py --scan 발표자료.md")
    print("새 수치를 쓰려면 이 파일에 재현 함수를 먼저 추가하십시오.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
