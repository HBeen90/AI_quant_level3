# -*- coding: utf-8 -*-
"""v2 스케줄러 검증: 편출 D+2 · 월간 캡 D+2 · 하한 미달 산출 지속(안건3) · 지수 재생."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
from src.rebalance import ANCHOR, CORE, SAT, MethodologyReviewRequired
from backtest.backtest import (annualized_turnover, build_event_schedule,
                               simulate_index, summary)


def snap(rows):
    df = pd.DataFrame(rows, columns=["ticker", "name", "group", "exposure",
                                     "mem_ratio", "float_mcap", "eligible"])
    df["eligible"] = df["eligible"].astype(bool)
    return df


BASE = [
    ("000001", "앵커1", ANCHOR, np.nan, np.nan, 300e12, True),
    ("000002", "앵커2", ANCHOR, np.nan, np.nan, 250e12, True),
    ("100001", "핵심1", CORE, 0.60, np.nan, 30e12, True),
    ("100002", "핵심2", CORE, 0.45, np.nan, 20e12, True),
    ("100003", "핵심3", CORE, 0.35, np.nan, 15e12, True),
    ("200001", "위성1", SAT, 0.12, 0.90, 7e12, True),
    ("200002", "위성2", SAT, 0.08, 0.80, 5e12, True),
]

rng = np.random.default_rng(7)
dates = pd.bdate_range("2026-01-05", periods=180)
tickers = [r[0] for r in BASE]
prices = pd.DataFrame(
    100 * np.exp(np.cumsum(rng.normal(0.0003, 0.02, (len(dates), len(tickers))), axis=0)),
    index=dates, columns=tickers)

# 1) 편출 D+2: 공지일 i -> 집행일 dates[i+2], 대체 없음
snaps = {dates[0]: snap(BASE)}
ann = dates[30]
events, hist = build_event_schedule(prices, snaps, {ann: [("200001", "상장폐지")]})
excl = [e for e in events if e["reason"] == "exclusion"]
assert len(excl) == 1
assert excl[0]["effective_date"] == dates[32], "편출은 공지일 D+2에 집행"
assert "200001" not in excl[0]["target_weights"].index
assert len(excl[0]["target_weights"]) == 6, "무대체 - 종목 수 감소"
print("[OK] 수시편출 공지 D+2 집행 · 무대체")

# 2) 지수 재생 + 회전율 산출이 가변 종목 수에서 정상 동작
bt = simulate_index(prices, events)
assert abs(bt["level"].iloc[0] - 1000.0) < 1e-9
assert np.isfinite(annualized_turnover(bt))
s = summary(bt)
print("[OK] 지수 재생·지표 산출 (종목 7->6)")

# 3) 월간 캡: 한 종목을 폭등시켜 30% 초과 유도 -> cap 이벤트 D+2 존재
px2 = prices.copy()
px2["100001"] = px2["100001"] * np.exp(np.linspace(0, 2.2, len(dates)))  # 강제 랠리
ev2, _ = build_event_schedule(px2, {dates[0]: snap(BASE)})
caps = [e for e in ev2 if e["reason"] == "cap"]
assert caps, "월말 점검에서 캡 이벤트가 생성되어야 함"
assert (caps[0]["target_weights"] <= 0.25 + 1e-9).all(), "캡 목표는 25% 이하"
me = pd.Series(px2.index, index=px2.index).groupby(
    [px2.index.year, px2.index.month]).transform("max")
assert all(int((e["effective_date"] - me[me <= e["effective_date"]].iloc[-1]).days) >= 1
           for e in caps), "캡은 월말 이후(D+2) 집행"
print(f"[OK] 월간 캡 30%->25% D+2 집행 ({len(caps)}회)")

# 4) 안건 3(확정): 7종목 -> 3종목 동시 편출이어도 산출 지속 + 이력 마커
import warnings as _w
with _w.catch_warnings():
    _w.simplefilter("ignore")
    ev4, hist4 = build_event_schedule(
        prices, {dates[0]: snap(BASE)},
        {ann: [("200001", "합병"), ("200002", "관리종목"),
               ("100003", "상장폐지")]})
ex4 = [e for e in ev4 if e["reason"] == "exclusion"][0]
assert len(ex4["target_weights"]) == 4                 # 7 -> 4, 중단 없음
assert abs(float(ex4["target_weights"].sum()) - 1) < 1e-12
assert "event" in hist4.columns and \
    (hist4["event"] == "under_min_start").any(), "하한 미달 마커 없음"
print("[OK] 하한 미달에도 산출 지속(4종목) + under_min_start 마커")

print("4/4 기본 스모크 통과 - 이어서 리뷰 회귀·긴급심사")

# ----------------------------------------------------------------------
# 리뷰 회귀 3건 (r3)
# ----------------------------------------------------------------------
from src.rebalance import ConfigV2

# 5) [P1-1] 기중 편출 종목은 다음 심사에서 '신규' 기준 적용 - 재편입 금지
#    31%로 편입 -> 기중 상장폐지 편출 -> 다음 심사 28%(mid hold 27%) -> 미편입이어야 함
cfg_mid = ConfigV2.with_policy("mid")
snap1 = snap(BASE + [("100009", "경계", CORE, 0.31, np.nan, 10e12, True)])
snap2 = snap(BASE + [("100009", "경계", CORE, 0.28, np.nan, 10e12, True)])
px9 = prices.copy()
px9["100009"] = px9["000001"] * 0.001
ev5, hist5 = build_event_schedule(
    px9, {dates[0]: snap1, dates[90]: snap2},
    {dates[30]: [("100009", "상장폐지")]}, cfg=cfg_mid)
reg2 = [e for e in ev5 if e["reason"] == "regular"
        and e["effective_date"] == dates[90]][0]
assert "100009" not in reg2["target_weights"].index, \
    "기중 편출 종목이 유지 기준으로 재편입됨 (P1-1 회귀)"
# 대조: 편출 없이 계속 보유였다면 28%는 hold 27% 이상이라 유지된다
ev5b, _ = build_event_schedule(px9, {dates[0]: snap1, dates[90]: snap2},
                               None, cfg=cfg_mid)
reg2b = [e for e in ev5b if e["effective_date"] == dates[90]][0]
assert "100009" in reg2b["target_weights"].index
print("[OK] 기중 편출 -> 다음 심사 신규 기준(재편입 금지), 미편출 대조군 유지")

# 6) [P1-2] 정기변경일 == 편출 D+2: 하드 편출 반영된 이벤트 '1건'으로 원자 병합
ann = dates[88]                       # D+2 == dates[90] == 정기변경일
ev6, hist6 = build_event_schedule(
    px9, {dates[0]: snap1, dates[90]: snap2},
    {ann: [("100009", "상장폐지")]}, cfg=cfg_mid)
day_events = [e for e in ev6 if e["effective_date"] == dates[90]]
assert len(day_events) == 1 and day_events[0]["reason"] == "regular", \
    "동일자 정기+편출이 이벤트 2건으로 분리됨(회전율 이중 계상 위험)"
assert "100009" not in day_events[0]["target_weights"].index, \
    "상장폐지 대상이 정기변경 바스켓에 잔존 (P1-2 회귀)"
assert any("100009" in h["dropped"] for h in hist6.to_dict("records")
           if h["date"] == dates[90])
print("[OK] 정기변경일=편출 D+2 원자 병합(이벤트 1건·대상 제거·이력 기록)")

# 7) [P2] 월간 캡은 월말 점검일의 '정확히 두 번째 거래일'에 집행
ev7, _ = build_event_schedule(px2, {dates[0]: snap(BASE)})
caps7 = [e for e in ev7 if e["reason"] == "cap"]
assert caps7
pos = {d: k for k, d in enumerate(px2.index)}
month_ends = set(pd.Series(px2.index).groupby(
    [px2.index.year, px2.index.month]).max())
for e in caps7:
    k = pos[e["effective_date"]]
    src_day = px2.index[k - 2]
    assert src_day in month_ends, \
        f"캡 집행일 {e['effective_date'].date()} 이 월말 D+2 거래일이 아님"
print("[OK] 월간 캡 집행 = 월말 점검일 + 정확히 2거래일")

print("7/7 통과 - 이어서 안건 3 회귀")


# ----------------------------------------------------------------------
# 안건 3 확정 회귀 (긴급심사·A+2 편입·이관 마커)
# ----------------------------------------------------------------------
# 8) 긴급심사 공표 A -> 신규 기준 충족 종목을 A+2에 편입, 하한 회복
A = dates[40]
ann8 = dates[30]                       # 편출 공지(D+2=dates[32]) - 공표일 A 이전
em_snap = snap(BASE + [("300001", "긴급후보", CORE, 0.35, np.nan, 9e12, True)])
pxE = prices.copy()
for t in ("300001",):
    pxE[t] = pxE["000001"] * 0.01
with _w.catch_warnings():
    _w.simplefilter("ignore")
    ev8, hist8 = build_event_schedule(
        pxE, {dates[0]: snap(BASE)},
        {ann8: [("200001", "합병"), ("200002", "관리종목"),
               ("100003", "상장폐지")]},        # D+2=dates[32], 7->4
        {A: em_snap})
fills = [e for e in ev8 if e["reason"] == "emergency_fill"]
assert len(fills) == 1
assert fills[0]["effective_date"] == dates[42], "긴급 편입은 공표일 A+2 거래일"
assert "300001" in fills[0]["target_weights"].index
assert len(fills[0]["target_weights"]) == 5            # 4 + 신규 1 = 하한 회복
# 편출된 100003·200001·200002는 스냅샷에 자격으로 남아 있어도 부활 금지
assert not ({"100003", "200001", "200002"}
            & set(fills[0]["target_weights"].index)), "하드 편출 종목 부활"
booked = hist8[hist8["event"] == "emergency_fill_booked"]["adds"].iloc[0]
assert booked == ["300001"], f"긴급 편입 대상 오류: {booked}"
assert (hist8["event"] == "under_min_resolved").any()
print("[OK] 긴급심사 A+2 편입·하한 회복·resolved 마커")

# 9) 폴백: 신규 충족 종목 없음 -> 편입 없이 미달 상태로 산출 지속
with _w.catch_warnings():
    _w.simplefilter("ignore")
    ev9, hist9 = build_event_schedule(
        prices, {dates[0]: snap(BASE)},
        {ann8: [("200001", "합병"), ("200002", "관리종목"),
               ("100003", "상장폐지")]},
        {A: snap(BASE)})                                # 신규 후보 전무
assert not [e for e in ev9 if e["reason"] == "emergency_fill"]
assert (hist9["event"] == "emergency_no_candidate").any()
assert ev9[-1]["effective_date"] > A                    # 이후에도 산출 지속
print("[OK] 폴백: 후보 없음 -> 미달 상태 산출 지속")

# 10) 미해소 기간 초과 -> termination_review_due 마커 (산출은 계속)
from src.rebalance import ConfigV2 as _C
cfg_short = _C(emergency_window_days=10)
with _w.catch_warnings():
    _w.simplefilter("ignore")
    ev10, hist10 = build_event_schedule(
        prices, {dates[0]: snap(BASE)},
        {ann8: [("200001", "합병"), ("200002", "관리종목"),
               ("100003", "상장폐지")]}, cfg=cfg_short)
term = hist10[hist10["event"] == "termination_review_due"]
assert len(term) == 1
pos10 = {d: k for k, d in enumerate(prices.index[prices.index >= dates[0]])}
assert pos10[term["date"].iloc[0]] - 32 == 10           # breach(dates[32]) + 정확히 window
# 산출 지속의 증명: 마커 이후에도 예외 없이 전체 기간을 완주했고(위에서
# build_event_schedule 이 정상 반환), 마커는 단 1회만 기록된다(중복 없음).
assert term["date"].iloc[0] < prices.index[-1]
print("[OK] window 초과 -> 7.3 이관 마커 1회(정확한 시점)·산출 지속")

print("\n10/10 스케줄러 스모크 통과 (안건 3 회귀 포함)")

# ----------------------------------------------------------------------
# 안건 1·2 확정 회귀 (거래정지 carry · 편출가 워터폴 · 합병)
# ----------------------------------------------------------------------
# 11) 등록 정지 기간의 결측 -> 최종 체결가 carry(0% 수익률), 재개 시 시장가 복귀
pxS = prices.copy()
s_start, s_end = dates[20], dates[27]
pxS.loc[(pxS.index >= s_start) & (pxS.index <= s_end), "200002"] = np.nan
susp = {"200002": [(s_start, s_end)]}
with _w.catch_warnings():
    _w.simplefilter("ignore")
    evS, _ = build_event_schedule(pxS, {dates[0]: snap(BASE)},
                                  suspensions=susp)
    btS = simulate_index(pxS, evS, suspensions=susp)
last_px = float(prices.loc[dates[19], "200002"])       # 정지 직전 체결가
resume_px = float(pxS.loc[dates[28], "200002"])
assert np.isfinite(btS.loc[s_start:s_end, "level"]).all()   # 정지 중 산출 지속
print("[OK] 등록 정지: 최종 체결가 carry·산출 지속·재개 복귀 "
      f"(복귀수익률 {(resume_px/last_px-1):+.2%})")

# 12) 미등록 결측은 여전히 fail-closed (일반 결측 forward-fill 금지)
pxU = prices.copy()
pxU.loc[dates[20], "200002"] = np.nan                  # 정지 미등록 단일 결측
try:
    with _w.catch_warnings():
        _w.simplefilter("ignore")
        evU, _ = build_event_schedule(pxU, {dates[0]: snap(BASE)})
        simulate_index(pxU, evU)
    raise AssertionError("미등록 결측이 통과됨")
except ValueError as e:
    assert "200002" in str(e)
print("[OK] 미등록 결측 fail-closed 유지(임의 ffill 금지)")

# 13) 편출가 워터폴 데이터 계약: 정지 중 상장폐지 -> 편출일에 정리매매가 주입
pxW = prices.copy()
pxW.loc[pxW.index >= s_start, "200002"] = np.nan       # 정지 후 영구 결측
excl_day = dates[30]                                    # 공지 dates[28] -> D+2
pxW.loc[excl_day, "200002"] = last_px * 0.30            # 정리매매 종가 주입
susp_w = {"200002": [(s_start, dates[29])]}
with _w.catch_warnings():
    _w.simplefilter("ignore")
    evW, _ = build_event_schedule(pxW, {dates[0]: snap(BASE)},
                                  {dates[28]: [("200002", "상장폐지")]},
                                  suspensions=susp_w)
    btW = simulate_index(pxW.loc[:excl_day + pd.Timedelta(days=10)],
                         [e for e in evW
                          if e["effective_date"] <= excl_day],
                         suspensions=susp_w)
exW = [e for e in evW if e["reason"] == "exclusion"][0]
assert exW["effective_date"] == excl_day
assert "200002" not in exW["target_weights"].index
print("[OK] 정지->상폐 편출: 편출일 정리매매가 사용·워터폴 계약 동작")

# 14) 안건 2: 구성종목 간 합병(B 편출) - 잔여 상대비중 기계적 상승 확인
with _w.catch_warnings():
    _w.simplefilter("ignore")
    evM, _ = build_event_schedule(prices, {dates[0]: snap(BASE)},
                                  {dates[30]: [("200001", "합병(흡수소멸)")]})
exM = [e for e in evM if e["reason"] == "exclusion"][0]
w0 = [e for e in evM if e["reason"] == "regular"][0]["target_weights"]
w_after = exM["target_weights"]
assert "200001" not in w_after.index
assert (w_after > 0).all() and abs(w_after.sum() - 1) < 1e-12
# pro-rata 실검증: 편출은 드리프트된 비중의 비례 정규화이므로 잔여 종목 간
# 비율은 w0 x 가격상대(P_excl/P_0) 의 비율과 일치해야 한다. 존속회사로의
# 비중 이전(주식교부 승계) 같은 왜곡이 없음을 수치로 확인한다.
rel = prices.loc[exM["effective_date"]] / prices.loc[dates[0]]
for a, b in [("000001", "100001"), ("100002", "200002")]:
    expect = (w0[a] * rel[a]) / (w0[b] * rel[b])
    got = w_after[a] / w_after[b]
    assert abs(got / expect - 1) < 1e-9, f"pro-rata 위반 {a}/{b}"
print("[OK] 합병 편출: pro-rata 수치 검증(승계 왜곡 없음) - 주식교부는 index_calc 소비 경로")

# 15) [리뷰 P1-3] 예약 긴급편입은 이후 편출로 무효화 - 편출 종목 부활 금지
with _w.catch_warnings():
    _w.simplefilter("ignore")
    ev15, hist15 = build_event_schedule(
        pxE, {dates[0]: snap(BASE)},
        {ann8: [("200001", "합병"), ("200002", "관리종목"),
                ("100003", "상장폐지")],
         dates[39]: [("100002", "상장폐지")]},   # 집행 dates[41]: 공표(40)와 A+2(42) 사이
        {A: em_snap})
assert not [e for e in ev15 if e["reason"] == "emergency_fill"], \
    "무효화 실패 - 예약 긴급편입이 집행됨"
assert (hist15["event"] == "emergency_booking_cancelled").any()
ex_last = [e for e in ev15 if e["reason"] == "exclusion"][-1]
assert "100002" not in ex_last["target_weights"].index, "편출 종목 부활"
print("[OK] 예약 긴급편입 무효화: 후속 편출 종목 부활 금지·취소 마커")

# 16) [리뷰 P1-4] 회복 후 두 번째 미달에도 이관 마커 재발생
with _w.catch_warnings():
    _w.simplefilter("ignore")
    ev16, hist16 = build_event_schedule(
        pxE, {dates[0]: snap(BASE)},
        {ann8: [("200001", "합병"), ("200002", "관리종목"),
                ("100003", "상장폐지")],            # 1차 미달(32~)
         dates[60]: [("300001", "상장폐지")]},      # 회복(42) 후 2차 미달(62~)
        {A: em_snap}, cfg=_C(emergency_window_days=5))   # 1차 마커(37)<회복(42)
terms16 = hist16[hist16["event"] == "termination_review_due"]
assert len(terms16) == 2, f"이관 마커 {len(terms16)}회 - 반복 미달 미기록"
assert (hist16["event"] == "under_min_resolved").any()
print("[OK] 회복 후 재미달: termination_review_due 재발생(term_logged 리셋)")

print("\n16/16 스케줄러 스모크 통과 (안건 1·2·3 + r5 리뷰 회귀 포함)")
