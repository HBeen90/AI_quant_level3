# -*- coding: utf-8 -*-
"""
app.py - HBM 테마 지수 운영 대시보드 (Streamlit)
================================================
실행
    pip install streamlit altair
    streamlit run app.py

설계 원칙
---------
1) **얇은 앱.** 계산은 전부 기존 모듈(src/ · backtest/ · analysis/)이 한다.
   이 파일은 입력 위젯과 표시만 담당한다. 대시보드가 별도의 계산 경로를
   가지면 그 순간 '엔진과 대시보드가 다른 답을 내는' 사고가 시작된다.
2) **데이터 없이도 켜진다.** 백테스트 결과가 아직 없어도 용량 역산기와
   PIT 데모는 즉시 동작한다 - 지금 상태에서 바로 쓸 수 있어야 한다.
3) **fail-closed 승계.** 결측·계약 위반은 조용히 넘기지 않고 화면에 띄운다.
4) **차트는 표와 함께.** 색만으로 정보를 전달하지 않는다(접근성).
"""
from __future__ import annotations

import os
import sys

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

st.set_page_config(page_title="HBM 테마 지수 대시보드", layout="wide",
                   initial_sidebar_state="expanded")

# ── 검증된 팔레트 (dataviz 표준, light 모드 4슬롯 통과) ────────────────────
C_BLUE, C_ORANGE, C_AQUA, C_YELLOW = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"
CAT4 = [C_BLUE, C_ORANGE, C_AQUA, C_YELLOW]
SEQ_BLUE = ["#cfe0f7", "#8ab4ec", "#4a90e0", "#1c5aa8"]   # 순서형(버퍼 폭)
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e6e5e0"


def _base(ch: alt.Chart) -> alt.Chart:
    """공통 축·격자 스타일 - 격자는 후퇴시키고 데이터를 앞세운다."""
    return ch.configure_axis(
        grid=True, gridColor=GRID, gridOpacity=0.7, domainColor=GRID,
        tickColor=GRID, labelColor=MUTED, titleColor=MUTED,
        labelFontSize=11, titleFontSize=11,
    ).configure_legend(labelColor=INK, titleColor=MUTED, symbolType="stroke",
                       symbolStrokeWidth=3).configure_view(stroke=None)


# ──────────────────────────────────────────────────────────────────────────
# 사이드바
# ──────────────────────────────────────────────────────────────────────────
st.sidebar.title("HBM 테마 지수")
st.sidebar.caption("4조 · 규칙 기반 가변 편입 (v2.2)")
PAGE = st.sidebar.radio("화면", [
    "① 파이프라인 상태",
    "② 용량 역산기",
    "③ PIT vs FROZEN",
    "④ 백테스트 결과",
    "⑤ 버퍼 정책 비교",
    "⑥ PR vs TR",
])
OUTDIR = st.sidebar.text_input("백테스트 산출 폴더", "out/backtest")
st.sidebar.divider()
st.sidebar.caption("④~⑥은 `run_backtest.py` 실행 후 활성화됩니다.\n"
                   "①~③은 데이터 없이 동작합니다.")


def _candidate_dirs() -> list:
    """산출물이 있을 수 있는 폴더.

    OUTDIR 과 **그 형제 폴더**만 본다. 레포 기본 경로까지 뒤지면, 사용자가
    없는 폴더를 지정했을 때도 데이터를 찾아내 "데이터 없음" 안내가
    작동하지 않는다(그 안내는 합성 수치 오용을 막는 장치다).

    PR/TR 병기는 계열이 섞이지 않도록 `out/backtest_tr/` 로 분리 저장되므로
    형제 폴더까지는 봐야 한다 - 한 곳만 보면 **파일이 있는데도 "없습니다"**
    라고 표시한다(2026-08-01 실제 발생).
    """
    base = os.path.dirname(os.path.abspath(OUTDIR))
    name = os.path.basename(os.path.abspath(OUTDIR))
    sibs = [OUTDIR]
    if name.startswith("backtest"):
        sibs.append(os.path.join(base, "backtest_tr"))
    seen, out = set(), []
    for d in sibs:
        a = os.path.abspath(d)
        if a not in seen:
            seen.add(a)
            out.append(d)
    return out


def _read(name: str, ts: bool = False) -> pd.DataFrame | None:
    """산출 CSV 로더. ts=True 이면 인덱스를 날짜로 파싱한다.

    정책 비교표처럼 인덱스가 문자열인 파일에 parse_dates 를 걸면 조용히
    이상한 인덱스가 만들어지므로 호출부에서 명시한다(fail-closed 승계).
    """
    # 산출물이 한 폴더에 있지 않다. PR/TR 병기는 계열이 섞이지 않도록
    # `out/backtest_tr/` 로 분리해 저장하는데, 화면이 OUTDIR 한 곳만 보면
    # **파일이 있는데도 "없습니다"** 라고 표시한다(2026-08-01 실제 발생).
    # 존재하는 곳을 찾는다 - 경로를 화면에 박아 두면 또 낡는다.
    p = None
    for d in _candidate_dirs():
        q = os.path.join(d, name)
        if os.path.exists(q):
            p = q
            break
    if p is None:
        return None
    df = pd.read_csv(p, index_col=0)
    if ts:
        df.index = pd.to_datetime(df.index)
    return df


def _need(name: str, how: str) -> None:
    st.info(f"`{OUTDIR}/{name}` 이 없습니다.\n\n생성 명령:\n```\n{how}\n```")


# ==========================================================================
if PAGE.startswith("①"):
    st.title("파이프라인 상태")
    st.caption("무엇이 끝났고 무엇이 비어 있는가 - 한 화면 요약")

    # 상태를 하드코딩하지 않는다. 이 화면이 몇 세대 전 상태를 보여준 적이
    # 있어(판정 원장 1/13회분·진행 8%) 발표에서 그대로 뜰 뻔했다.
    # 산출물에서 세면 낡지 않는다.
    def _count(pat):
        import glob
        return len(glob.glob(os.path.join(HERE, pat)))

    def _exists(name):
        """산출물 존재 확인. **OUTDIR 기준**으로 본다.

        경로를 "out/backtest/..." 로 박아 두면 사이드바에서 산출 폴더를
        바꿨을 때 화면이 엉뚱한 곳을 보고 "미실행"이라고 표시한다.
        같은 유형의 결함이 ⑥ 화면에서 이미 한 번 나왔다.
        """
        return any(os.path.exists(os.path.join(d, name))
                   for d in _candidate_dirs())

    n_snap = _count("data/snapshots/snapshot_*.csv")
    ledger_pct = min(100, int(round(n_snap / 13 * 100))) if n_snap else 0
    ledger_state = f"{n_snap}/13회분" if n_snap < 13 else "13/13 완료"

    steps = pd.DataFrame([
        ("universe.py", "기초 유니버스 필터", "임효빈", "빈 파일", 0),
        ("selection.py", "규칙 0/A/C 판정", "노민수", "완료", 100),
        ("weighting.py", "40/60 배분·상한·IIF", "노민수", "완료", 100),
        ("rebalance.py", "히스테리시스·수시변경", "김소연", "완료", 100),
        ("backtest.py", "이벤트 스케줄·지표", "김소연", "완료", 100),
        ("index_calc.py", "IIF·제수·TR", "김인서", "동치 1e-15", 100),
        ("판정 원장(PIT)", "시점별 심사 스냅샷", "팀", ledger_state, ledger_pct),
    ], columns=["모듈", "역할", "담당", "상태", "진행"])
    st.dataframe(steps, use_container_width=True, hide_index=True,
                 column_config={"진행": st.column_config.ProgressColumn(
                     "진행", min_value=0, max_value=100, format="%d%%")})

    st.subheader("병목")
    if n_snap < 13:
        st.warning(
            f"**PIT 심사 스냅샷이 {n_snap}회분뿐입니다.** 성과 보고·순방향 "
            "재생·실측 용량이 함께 막혀 있습니다.")
    else:
        st.success(
            "**PIT 심사 스냅샷 13회분이 채워졌습니다.** 성과·회전율·벤치마크 "
            "수치가 해제됐고, 계보 등급은 `METADATA_VERIFIED`입니다"
            "(DART 원문 215건 독립 재수집 전수 일치).\n\n"
            "남은 병목은 데이터가 아니라 **문서-구현 정합**입니다 - "
            "아래 미결 표 참조.")

    c1, c2, c3 = st.columns(3)
    c1.metric("회귀 테스트", "자동화됨", "tests/run_all.py")
    c2.metric("지수 산출 동치성", "2.6e-15", "TR 포함")
    c3.metric("PIT 스냅샷", f"{n_snap}회", f"{n_snap - 13:+d}회")

    st.subheader("미결 항목")
    open_items = [
        ("유동비율 원천 단일화", "DART 기준 사용 중", "KRX 공식 유동비율"),
        ("방법론 규칙 3(위성군) 조문", "3요건이 코드에만 있음",
         "docs/rule_c_clause_draft_20260731.md 의결"),
        ("방법론 벤치마크 조항", "문서에 '벤치마크' 0건",
         "docs/benchmark_chapter_draft_20260731.md 의결"),
        ("40/60 버킷 규정", "13회 중 1회만 실현", "안 A 또는 B 의결"),
        ("후보발굴 운영 실행", "동결 CSV 없음",
         "candidate_discovery.py --discover"),
    ]
    if not _exists("rule_c_sensitivity.csv"):
        open_items.append(("규칙 C 기여도 실측", "미실행",
                           "rule_c_sensitivity.py"))
    for f, label, script in (
            ("ablation_cumulative.csv", "층별 ablation", "ablation_study.py"),
            ("regime_calendar.csv", "구간별 robustness", "regime_robustness.py"),
            ("concentration_daily.csv", "집중도 계량",
             "concentration_replication.py"),
            ("bucket_band_turnover.csv", "버킷 밴드 비용",
             "bucket_band_turnover.py"),
            ("frequency_sensitivity.csv", "재가중 주기 민감도",
             "frequency_sensitivity.py")):
        if not _exists(f):
            open_items.append((label, "미실행", script))
    st.dataframe(pd.DataFrame(open_items,
                              columns=["미결", "왜 못 닫는가", "필요한 것"]),
                 use_container_width=True, hide_index=True)

    st.subheader("해소된 항목 (2026-07-31)")
    st.dataframe(pd.DataFrame([
        ("PIT 판정 원장", "215행 · 33종목 · FY2019~2025"),
        ("계보 검증", "L09_PARTIAL → METADATA_VERIFIED (불일치 0건)"),
        ("성과 보고 수치", "FINAL 게이트 통과 · 인용 가능"),
        ("생존편향 대조", "소멸 종목 382건 전수 · 후보 0건"),
        ("버킷 드리프트 점검", "실측 완료 · 안 C 는 캡과 양립 불가로 보류"),
        ("버퍼 27/67 근거", "정본 확정 (다중 seed) · 실측 무차이 병기"),
    ], columns=["항목", "결과"]), use_container_width=True, hide_index=True)

# ==========================================================================
elif PAGE.startswith("②"):
    from analysis.capacity_v2 import (capacity_implied_cap, evaluate_fixed_cap,
                                      implied_cap_table)

    st.title("용량 역산기")
    st.caption("'위성 상한 5%' 같은 고정 퍼센트가 실제로 며칠에 해당하는지 "
               "되짚고, 허용일수에서 상한을 역산합니다")

    c1, c2, c3 = st.columns(3)
    aum = c1.number_input("AUM (억원)", 100, 100000, 3000, step=100)
    part = c2.slider("시장 참여율", 0.02, 0.30, 0.10, 0.01,
                     help="시장 일평균거래대금 중 지수 추종 매매가 차지해도 "
                          "충격이 제한적이라 보는 비율. 관행상 10~20%.")
    days = c3.slider("허용 소요일수 (거래일)", 1, 30, 5,
                     help="정기변경 매매를 며칠 안에 소화할 것인가 - "
                          "고정 퍼센트 대신 이 값을 조문에 넣는 방식")

    st.subheader("역질문 - 검토 중인 고정 상한은 며칠인가")
    cap = st.slider("검토 중인 고정 상한", 0.01, 0.30, 0.05, 0.01, format="%.2f")
    advs = [15.0, 45.0, 120.0, 500.0]
    ev = evaluate_fixed_cap(cap, advs, aum, part)
    ev.columns = ["ADV60(억)", "소요일수"]
    ev["허용일수 대비"] = ev["소요일수"] / days

    bar = alt.Chart(ev).mark_bar(
        color=C_ORANGE, cornerRadiusTopRight=4, cornerRadiusBottomRight=4,
        height=22).encode(
        y=alt.Y("ADV60(억):O", title="ADV60 (억원)", sort=None),
        x=alt.X("소요일수:Q", title="전량 매매 소요일수 (거래일)"),
        tooltip=[alt.Tooltip("ADV60(억):O"),
                 alt.Tooltip("소요일수:Q", format=".1f"),
                 alt.Tooltip("허용일수 대비:Q", format=".1fx")])
    rule = alt.Chart(pd.DataFrame({"x": [days]})).mark_rule(
        color=INK, strokeDash=[4, 3], strokeWidth=2).encode(x="x:Q")
    txt = bar.mark_text(align="left", dx=6, color=INK, fontSize=11).encode(
        text=alt.Text("소요일수:Q", format=".0f일"))
    st.altair_chart(_base((bar + txt + rule).properties(height=180)),
                    use_container_width=True)
    st.caption(f"점선 = 허용일수 {days}거래일. 막대가 점선을 넘으면 그 종목은 "
               f"상한 {cap:.0%}까지 채우면 안 됩니다.")
    st.dataframe(ev.style.format({"소요일수": "{:.1f}일",
                                  "허용일수 대비": "{:.1f}배",
                                  "ADV60(억)": "{:,.0f}"}),
                 use_container_width=True, hide_index=True)

    worst = ev.iloc[0]
    if worst["소요일수"] > days:
        st.error(
            f"**ADV60 {worst['ADV60(억)']:.0f}억 종목은 상한 {cap:.0%}에서 "
            f"{worst['소요일수']:.0f}거래일**이 필요합니다 (허용 {days}일의 "
            f"{worst['허용일수 대비']:.0f}배). 같은 퍼센트가 ADV에 따라 "
            f"{ev['소요일수'].max() / ev['소요일수'].min():.0f}배 차이 납니다 - "
            "고정 퍼센트는 '얼마나 안전한가'를 말해주지 않습니다.")
    else:
        st.success(f"검토 중인 상한 {cap:.0%}는 모든 ADV 시나리오에서 "
                   f"허용일수 {days}일 이내입니다.")

    st.subheader("대안 - 허용일수에서 역산한 상한")
    st.latex(r"w_{max,i} = \frac{ADV60_i \times 참여율 \times 허용일수}{AUM}")
    tbl = implied_cap_table(advs, [500.0, 1000.0, 3000.0, 10000.0],
                            days_list=(3, 5, 10, 20), participation=part)
    st.dataframe(
        tbl.style.format({c: "{:.2%}" for c in tbl.columns if "상한" in c}),
        use_container_width=True, hide_index=True)

    st.info(
        "허용일수는 운영 목표라서 시장이 변해도 그대로이고, 상한이 자동으로 "
        "따라옵니다. 고정 퍼센트를 매번 재량으로 고치면 **재현성 원칙**이 "
        "깨집니다.\n\n"
        f"현행 조문: 위성 개별 15% · 합계 18%. AUM {aum:,.0f}억·ADV 15억 "
        f"종목이 15%까지 차면 전량 매매에 "
        f"{0.15 * aum / (15 * part):.0f}거래일 - 정기변경 주기(약 125거래일)와 "
        "비교해 보십시오.\n\n"
        "**상한 신설·변경은 방법론 개정 절차(7.3) 사안입니다.** 이 화면은 "
        "위원회 상정용 진단 수치를 만들 뿐 조문을 바꾸지 않습니다.")

# ==========================================================================
elif PAGE.startswith("③"):
    from analysis.build_pit_snapshots import as_of_ledger, screen, to_snapshot
    from analysis.demo_why_pit import FYS, build_ledger, fake_facts, run
    from analysis.index_calendar import pair_selection_to_rebalance
    from src.rebalance import BUFFER_POLICIES

    st.title("PIT vs FROZEN")
    st.caption("'오늘 값을 과거에 복사하면 안 되나?' - 같은 엔진, 다른 입력")

    with st.spinner("13회 정기변경 판정 재생 중..."):
        led = build_ledger()
        td = pd.bdate_range("2020-01-01", "2026-12-31")
        pairs = [(s, r) for s, r in pair_selection_to_rebalance(td)
                 if r <= pd.Timestamp("2026-07-26")]
        sel_dates = [s for s, _ in pairs]
        rows = []
        for mode in ("FROZEN", "PIT"):
            for pol in BUFFER_POLICIES:
                r = run(mode, pol, sel_dates, led)
                rows.append({"입력": mode, "버퍼정책": pol,
                             "유지선": f"{BUFFER_POLICIES[pol]['hold_core']:.2f}/"
                                      f"{BUFFER_POLICIES[pol]['hold_sat']:.2f}",
                             "편출입 합": r["편출입 합"],
                             "편입": r["편입"], "편출": r["편출"],
                             "평균 종목수": round(r["평균 종목수"], 1)})
        res = pd.DataFrame(rows)

    c1, c2 = st.columns(2)
    fz = int(res[res["입력"] == "FROZEN"]["편출입 합"].max())
    pt = res[res["입력"] == "PIT"]["편출입 합"]
    c1.metric("FROZEN - 6년 편출입", f"{fz}회", "정책 4안 전부 동일",
              delta_color="off")
    c2.metric("PIT - 6년 편출입", f"{int(pt.min())}~{int(pt.max())}회",
              "정책마다 다름")

    # 버퍼 폭은 순서형(none→wide)이므로 순서형 단일 색상 램프를 쓴다.
    # (범주형 8색을 순서 있는 값에 쓰면 순서 정보가 사라진다)
    pol_order = list(BUFFER_POLICIES)
    ch = alt.Chart(res).mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4,
                                 stroke="#fcfcfb", strokeWidth=2).encode(
        x=alt.X("입력:N", sort=["FROZEN", "PIT"], title=None,
                axis=alt.Axis(labelFontSize=13, labelFontWeight="bold")),
        xOffset=alt.XOffset("버퍼정책:N", sort=pol_order),
        y=alt.Y("편출입 합:Q", title="6년 누적 편출입 (회)"),
        color=alt.Color("버퍼정책:N", sort=pol_order,
                        scale=alt.Scale(domain=pol_order, range=SEQ_BLUE),
                        legend=alt.Legend(title="버퍼 폭 (좁음→넓음)")),
        tooltip=["입력", "버퍼정책", "유지선", "편입", "편출", "평균 종목수"])
    st.altair_chart(_base(ch.properties(height=300)), use_container_width=True)

    st.dataframe(res, use_container_width=True, hide_index=True)

    st.error(
        "**FROZEN은 6년 동안 편출입이 0회입니다.** 판정값이 안 변하니 매 회차 "
        "같은 종목이 뽑히고, 버퍼 정책 4안이 전부 같은 답을 냅니다.\n\n"
        "→ 회전율 0. 버퍼룰이 '아무것도 안 한 것'으로 측정됩니다.\n"
        "→ 27/67 대 25/65를 비교할 근거가 **생성되지 않습니다**.\n"
        "→ 백테스트는 에러 없이 끝나고 그래프도 예쁘게 나옵니다. "
        "측정하는 게 없을 뿐입니다 - 실패한 티가 안 나는 게 가장 위험합니다.")

    st.subheader("실제로 움직인 종목 (PIT / mid)")
    from src.rebalance import ConfigV2, select_v2
    prev, log = set(), []
    cfg = ConfigV2.with_policy("mid")
    for s, r in pairs:
        pit = as_of_ledger(led, s)
        snap = to_snapshot(screen(fake_facts(pit["ticker"].tolist()), pit))
        cur = set(select_v2(snap, prev_members=prev, cfg=cfg)["members"]["ticker"])
        if prev and (cur - prev or prev - cur):
            log.append({"시행일": r.date(), "종목수": len(cur),
                        "편입": ", ".join(sorted(cur - prev)) or "-",
                        "편출": ", ".join(sorted(prev - cur)) or "-"})
        prev = cur
    st.dataframe(pd.DataFrame(log), use_container_width=True, hide_index=True)
    st.caption("G* = 성장형(HBM 붐으로 임계값 통과) · B*/T* = 경계 진동. "
               "노출도 궤적은 예시이며 실제 종목 판정값이 아닙니다. "
               "변동이 6월에만 나타나는 이유 - 사업보고서가 연 1회(3월)뿐이라 "
               "12월 심사는 같은 사업연도 자료를 봅니다.")

# ==========================================================================
elif PAGE.startswith("④"):
    st.title("백테스트 결과")
    bt = _read("index_level.csv", ts=True)
    if bt is None:
        _need("index_level.csv",
              "python analysis/run_backtest.py --snapshots data/snapshots "
              "--prices-cache out/px.csv --policy all")
        st.stop()

    lv = bt["level"]
    yrs = (lv.index[-1] - lv.index[0]).days / 365.25
    dd = lv / lv.cummax() - 1
    c = st.columns(5)
    c[0].metric("누적수익률", f"{lv.iloc[-1] / lv.iloc[0] - 1:.2%}")
    c[1].metric("CAGR", f"{(lv.iloc[-1] / lv.iloc[0]) ** (1 / yrs) - 1:.2%}")
    c[2].metric("연변동성", f"{lv.pct_change().std() * np.sqrt(252):.2%}")
    c[3].metric("MDD", f"{dd.min():.2%}")
    c[4].metric("연율화 회전율", f"{bt['turnover'].sum() / yrs:.2%}")

    d = lv.reset_index()
    d.columns = ["date", "level"]
    line = alt.Chart(d).mark_line(color=C_BLUE, strokeWidth=2).encode(
        x=alt.X("date:T", title=None),
        y=alt.Y("level:Q", title="지수 (기준 1,000)",
                scale=alt.Scale(zero=False)),
        tooltip=[alt.Tooltip("date:T", title="일자"),
                 alt.Tooltip("level:Q", format=",.2f", title="지수")])
    ev = bt[bt["turnover"] > 0].reset_index()
    ev.columns = ["date"] + list(bt.columns)
    marks = alt.Chart(ev).mark_point(
        size=64, filled=True, color=C_ORANGE, stroke="#fcfcfb",
        strokeWidth=2).encode(
        x="date:T", y="level:Q",
        tooltip=[alt.Tooltip("date:T", title="일자"), "reason:N",
                 alt.Tooltip("turnover:Q", format=".4f", title="편도 회전율")])
    st.altair_chart(_base((line + marks).properties(height=340)),
                    use_container_width=True)
    st.caption("주황 점 = 리밸런싱 이벤트(정기·수시·캡). 마우스를 올리면 사유와 "
               "회전율이 나옵니다.")

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("회전율 분해")
        el = _read("event_log.csv")
        if el is not None and "reason" in el.columns:
            g = el.groupby("reason")["one_way_turnover"].sum() \
                .sort_values(ascending=False).reset_index()
            ch = alt.Chart(g).mark_bar(cornerRadiusTopRight=4,
                                       cornerRadiusBottomRight=4,
                                       height=24).encode(
                y=alt.Y("reason:N", title=None, sort="-x"),
                x=alt.X("one_way_turnover:Q", title="편도 회전율 누적"),
                color=alt.Color("reason:N", scale=alt.Scale(range=CAT4),
                                legend=None),
                tooltip=["reason", alt.Tooltip("one_way_turnover:Q",
                                               format=".4f")])
            lab = ch.mark_text(align="left", dx=6, color=INK,
                               fontSize=11).encode(
                text=alt.Text("one_way_turnover:Q", format=".3f"))
            st.altair_chart(_base((ch + lab).properties(height=140)),
                            use_container_width=True)
            st.dataframe(g, use_container_width=True, hide_index=True)
    with c2:
        st.subheader("교체 이력")
        hist = _read("change_history.csv")
        if hist is not None:
            st.dataframe(hist, use_container_width=True, hide_index=True,
                         height=260)

    cov = _read("coverage_report.csv")
    if cov is not None and len(cov):
        st.subheader("가격 커버리지 경고")
        st.warning("아래 종목·구간에 가격 결측이 있습니다. 상장 전이면 해당 "
                   "시점 스냅샷에서 제외하고, 수집 실패면 재수집하십시오. "
                   "엔진은 결측을 임의 보정하지 않습니다.")
        st.dataframe(cov, use_container_width=True, hide_index=True)

# ==========================================================================
elif PAGE.startswith("⑤"):
    st.title("버퍼 정책 비교")
    st.caption("유지 임계값 27/67 사후 점검 - 패시브 배점")
    tbl = _read("policy_comparison.csv")
    if tbl is None:
        _need("policy_comparison.csv",
              "python analysis/run_backtest.py --snapshots data/snapshots "
              "--policy all")
        st.warning(
            "**주의** - 실측 백테스트 없이 회전율·CAGR 수치를 발표에 쓰지 "
            "마십시오. 27/67은 현행 확정 운영값이며 회차별 재량 변경이 "
            "금지됩니다. 이 비교표는 최소 2회차 실측 축적 후 방법론 개정 "
            "절차(7.3)에서만 사후 검토 자료로 사용합니다.")
        st.stop()

    d = tbl.reset_index().rename(columns={"index": "정책"})

    NEED = {"연율화회전율(편도)", "편입 건수", "편출 건수", "CAGR(30bp)",
            "평균 종목수"}
    if {"none", "mid"} <= set(tbl.index) and NEED <= set(tbl.columns):
        base, mid = tbl.loc["none"], tbl.loc["mid"]
        churn = lambda r: int(r["편입 건수"] + r["편출 건수"])   # noqa: E731
        c = st.columns(4)
        c[0].metric(
            "회전율 (mid)", f"{mid['연율화회전율(편도)']:.2%}",
            f"{mid['연율화회전율(편도)'] - base['연율화회전율(편도)']:+.2%} vs none",
            delta_color="inverse")
        c[1].metric("편출입 (mid)", f"{churn(mid)}회",
                    f"{churn(mid) - churn(base):+d}회 vs none",
                    delta_color="inverse")
        c[2].metric("CAGR 30bp (mid)", f"{mid['CAGR(30bp)']:.2%}",
                    f"{mid['CAGR(30bp)'] - base['CAGR(30bp)']:+.2%} vs none")
        c[3].metric("평균 종목수 (mid)", f"{mid['평균 종목수']:.1f}",
                    f"{mid['평균 종목수'] - base['평균 종목수']:+.1f}")
        st.caption("회전율·편출입은 낮을수록 좋으므로 감소를 녹색으로 표시합니다. "
                   "네 지표를 함께 보고 고르며, CAGR 단독으로 고르지 않습니다.")

    st.dataframe(d, use_container_width=True, hide_index=True)

    if {"연율화회전율(편도)", "CAGR(30bp)"} <= set(d.columns):
        pts = alt.Chart(d).mark_point(
            size=180, filled=True, stroke="#fcfcfb", strokeWidth=2).encode(
            x=alt.X("연율화회전율(편도):Q", title="연율화 편도 회전율",
                    scale=alt.Scale(zero=False)),
            y=alt.Y("CAGR(30bp):Q", title="비용 차감 후 CAGR (30bp)",
                    scale=alt.Scale(zero=False)),
            color=alt.Color("정책:N", sort=["none", "narrow", "mid", "wide"],
                            scale=alt.Scale(range=SEQ_BLUE),
                            legend=alt.Legend(title="버퍼 폭 (좁음→넓음)")),
            tooltip=list(d.columns))
        lab = pts.mark_text(dy=-16, color=INK, fontSize=12,
                            fontWeight="bold").encode(text="정책:N")
        st.altair_chart(_base((pts + lab).properties(height=340)),
                        use_container_width=True)
        st.caption("왼쪽 아래로 갈수록 회전율이 낮고, 위로 갈수록 비용 차감 "
                   "성과가 좋습니다. 좌상단이 유리하지만 **CAGR 단독으로 "
                   "고르지 않습니다** - 데이터 스누핑 방지.")

    st.info(
        "**패시브 배점 원칙**: 연율화 회전율 · 편출입 횟수 · 평균 종목 수 · "
        "비용 차감 후 순수익률을 함께 봅니다. 과거 CAGR이 가장 높은 정책을 "
        "고르면 데이터 스누핑 편향이 생깁니다.\n\n"
        "채택된 유지선은 회차별 재량으로 바꾸지 않으며, 변경은 방법론 개정 "
        "절차(7.3)로만 합니다 - 재현성 원칙.")

# ==========================================================================
else:
    st.title("PR vs TR")
    st.caption("가격지수와 총수익지수 병기 - 제6조")
    d = _read("index_level_pr_tr.csv", ts=True)
    if d is None:
        _need("index_level_pr_tr.csv",
              "python analysis/run_backtest.py --snapshots data/snapshots "
              "--mode both --dividends data/dividends.csv")
        st.info(
            "**PR에서 배당락 하락은 오류가 아니라 정의입니다.** 가격지수는 "
            "배당을 제외하므로 배당락일 하락을 그대로 반영하는 것이 정상 "
            "동작입니다. 고칠 대상이 아니라 TR을 별도 계열로 병기할 "
            "대상이며, 어느 쪽을 공식 지수로 삼을지는 위원회 결정 사항입니다.\n\n"
            "제수 기반 TR과 수익률 기반 TR이 동일한 답을 낸다는 것은 이미 "
            "실증했습니다 - 최대 상대차 **2.56e-15** "
            "(`tests/test_tr_equivalence.py`). 남은 것은 배당 데이터뿐입니다: "
            "`ex_date, ticker, dps` (DART 「현금·현물배당결정」 공시의 "
            "배당기준일 권장 - pykrx DPS는 연간 스냅샷이라 배당락일 역산이 "
            "위험합니다).")
        st.stop()

    long = d.reset_index().melt("index", var_name="계열", value_name="지수")
    long.columns = ["date", "계열", "지수"]
    ch = alt.Chart(long).mark_line(strokeWidth=2).encode(
        x=alt.X("date:T", title=None),
        y=alt.Y("지수:Q", title="지수 (기준 1,000)",
                scale=alt.Scale(zero=False)),
        color=alt.Color("계열:N", scale=alt.Scale(
            domain=["PR", "TR"], range=[C_BLUE, C_ORANGE])),
        tooltip=[alt.Tooltip("date:T", title="일자"), "계열:N",
                 alt.Tooltip("지수:Q", format=",.2f")])
    st.altair_chart(_base(ch.properties(height=340)), use_container_width=True)

    yrs = (d.index[-1] - d.index[0]).days / 365.25
    gap = float((d["TR"].iloc[-1] / d["PR"].iloc[-1]) ** (1 / yrs) - 1)
    c1, c2, c3 = st.columns(3)
    c1.metric("PR CAGR", f"{(d['PR'].iloc[-1] / d['PR'].iloc[0]) ** (1/yrs) - 1:.2%}")
    c2.metric("TR CAGR", f"{(d['TR'].iloc[-1] / d['TR'].iloc[0]) ** (1/yrs) - 1:.2%}")
    c3.metric("연환산 배당 기여도", f"{gap:.2%}")
    st.dataframe(d.tail(20), use_container_width=True)
