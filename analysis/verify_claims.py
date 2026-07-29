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
CLAIMS = [
    ("기준일 2020-06-15는 임의 상수가 아니라 3장 일정 조문의 산출값이다",
     c_base_date, "analysis/index_calendar.py", "실측(조문 재생)"),
    ("정기변경 지수 산출은 두 독립 경로가 부동소수점 한계까지 일치한다",
     c_regular_equivalence, "src/index_calc.py", "실측(수치 검증)"),
    ("수시편출의 '정규화 = 제수 흡수'는 두 파트 구현으로 상호 실증됐다",
     c_adhoc_equivalence, "tests/test_index_calc_series.py", "실측(수치 검증)"),
    ("TR 지수는 제수 보정 경로와 배당 재투자 경로가 일치한다",
     c_tr_equivalence, "tests/test_tr_equivalence.py", "실측(수치 검증)"),
    ("월말 30% 캡은 상시 가동되며 단일 종목 쏠림을 25%로 누른다",
     c_monthly_cap, "analysis/audit_review_claims.py", "엔진 실측(합성 가격)"),
    ("개별 캡이 잡지 못하는 버킷 레벨 드리프트가 존재한다(위원회 상정 사안)",
     c_bucket_drift, "analysis/audit_review_claims.py", "엔진 실측(합성 가격)"),
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
    ("전 테스트 스위트가 통과한다",
     c_test_suite, "tests/run_all.py", "실측(실행)"),
]

#: 감사 '결과'. 문장이 결함을 주장하므로 PASS = **결함이 재현됨**을 뜻한다.
#: CLAIMS 와 섞으면 "발표에 쓸 문장" 표에 결함이 끼고, 결함을 고치는 순간
#: FAIL 로 바뀌어 회귀처럼 보인다. 그래서 분리한다.
AUDITS = [
    ("폐기된 구 대시보드 표시 비중은 엔진에서 재현되지 않고 조문 상한을 위반한다",
     c_dashboard_weights, "analysis/audit_dashboard_numbers.py",
     "과거 결함 보존 재현 - 현재 app.py 수치가 아님"),
]

#: 등록되지 않았으므로 **인용 금지**인 수치 - 발표·문서·화면 어디에도 쓰지 않는다
FORBIDDEN = [
    ("지수 레벨 / 누적수익률 / CAGR", "지수 시계열이 산출된 적 없음"),
    ("연율화 회전율 (48.5% · 24.6% 등)", "실측 백테스트 미실행"),
    ("변동성 / MDD / 샤프지수", "동일"),
    ("벤치마크 수익률·MDD·상관계수", "벤치마크 지수를 조회한 적 없음"),
    ("DART 215건 독립 재수집 대조 완료 주장",
     "FINAL_INPUT_CONTRACT은 완성됐으나 L09 계보 검증은 아직 PARTIAL"),
    ("종목별 ADV60 · 유동시총 시계열", "pykrx 수집 미실행 (2026-07-23 1회분 제외)"),
    ("생존편향 크기 (1.5~3.0%p 등)", "대조 미실시 - 방향만 고지 가능"),
]

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


def _forbidden_values():
    """FORBIDDEN_LITERALS 를 float 로 정규화한 (값, 표기, 설명) 목록.
    '3,161.9' 와 '3161.9' 처럼 값이 같은 항목은 하나로 합친다(첫 표기 유지)."""
    seen, out = set(), []
    for lit, what in FORBIDDEN_LITERALS:
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

성과 수치가 없어도 **방법론의 완성도**는 이것으로 증명됩니다. 역사적 PIT
판정 원장은 완성됐고, 시장 데이터·벤치마크가 확정되기 전에는 성과를
산출하지 않는다고 말하는 쪽이 못 지킬 숫자를 띄우는 것보다 강합니다.

---

## 인용 금지 수치를 쓰고 싶어지면

역사적 PIT 판정 원장은 strict FINAL 215행으로 완성됐습니다. 성과 수치를
해제하려면 시장 데이터 커버리지와 벤치마크 계보를 확정하고 전구간
백테스트를 실행해야 합니다. 없는 연도를 보간하거나 2026 판정을 과거에
복사하지 않습니다.

그때 `verify_claims.py` 에 재현 함수를 추가하고 `FORBIDDEN` 에서 해당
항목을 지우십시오. 그 순서를 지키면 이 사고는 다시 나지 않습니다.
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
                + "\n".join(f"| {n} | {w} |" for n, w in FORBIDDEN) + "\n\n"
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
    for n, why in FORBIDDEN:
        print(f"  · {n:38s} {why}")
    print("\n문서 점검:  python analysis/verify_claims.py --scan 발표자료.md")
    print("새 수치를 쓰려면 이 파일에 재현 함수를 먼저 추가하십시오.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
