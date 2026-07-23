# -*- coding: utf-8 -*-
"""
index_calc.py — HBM 반도체 테마 지수: 구성종목 및 지수 산출 (Methodology Book 3장)
====================================================================
담당 범위: 3.1 최종 구성종목(입력값으로 받음) · 3.2 지수 산출식 · 3.3 산출 예시(검증) ·
           3.4 기준시가총액(제수) 수정

이 파일이 "받는" 것과 "하는" 것
    받는 것 (다른 파트 담당자가 만들어 넘겨주는 값 — 2장 로직은 여기서 다시 만들 필요 없음)
        - 최종 구성종목 12개 (앵커2·핵심7·위성3) — 2.2.2/2.3 담당자 결과물 (3.1 표)
        - 종목별 목표 비중(weight) — 2.3 비중 산정 담당자 결과물
        - 종목별 유동시가총액(ff_market_cap), 상장주식수, 종가 — pykrx 등으로 수집

    하는 것 (이 파일의 역할)
        - 위 입력을 받아 실제 "지수 숫자(포인트)"를 계산
        - 리밸런싱마다 구성이 바뀌어도 지수가 튀지 않게(불연속 없이) 이어붙이는 로직
        - Methodology 3.3의 손계산 예시(1000.00 -> 1015.40)와 코드 결과가 일치하는지 검증

용어가 나올 때마다 쉬운 말 설명을 주석으로 붙였습니다.
"""

from __future__ import annotations
import pandas as pd


# ============================================================
# CONFIG
# ============================================================
BASE_DATE = "2020-06-15"     # 기준일 — 이 날짜의 지수를 아래 값으로 둔다
BASE_INDEX_LEVEL = 1000.00   # 기준지수 — 지수의 "시작 눈금"


# ============================================================
# 0. 입력 데이터 불러오기
# ============================================================
def load_constituents(path: str) -> pd.DataFrame:
    """팀 공유 형식의 CSV를 읽습니다.
    종목코드는 반드시 문자열로 읽어야 합니다 — dtype 지정을 안 하면 판다스가 숫자로 인식해서
    '005930' 같은 앞자리 0이 '5930'으로 사라집니다.
    최소 컬럼: 코드, 종목명, bucket(anchor/core/satellite), weight, ff_market_cap, price
    """
    return pd.read_csv(path, dtype={"코드": str, "code": str})


# ============================================================
# 1. IIF(지수포함가중치) 산정 — "목표 비중대로 지수가 움직이게" 만드는 보정계수
# ============================================================
# 지수는 원래 유동시가총액이 큰 종목일수록 비중이 커지는 게 기본(2장에서 이미 그렇게 설계)
# 이지만, 팀이 정한 목표 비중(weight, 40%/60% 배분 + 각종 상한 반영 후 최종값)을 '정확히'
# 반영하려면 종목별로 유동시총을 살짝 보정해줘야 합니다. 그 보정계수가 IIF입니다.
#
#   IIF_i = weight_i x Σ(전체 유동시총) / 유동시총_i
#
# 이렇게 정하면 리밸런싱 '그 날'은 (유동시총 원본 그대로 더한 값) = M(t0) 이 되고,
# 그 다음날부터는 IIF·유동시총·주식수를 리밸런싱일 값으로 고정한 채 주가만 움직이면서
# 지수가 목표 비중대로 반응하게 됩니다.

def calc_iif(target_weights: pd.Series, ff_mcap_at_rebal: pd.Series) -> pd.Series:
    """target_weights: 종목별 목표 비중 (합계 1.0)
    ff_mcap_at_rebal: 리밸런싱일 기준 종목별 유동시가총액"""
    total_raw = ff_mcap_at_rebal.sum()
    return target_weights * total_raw / ff_mcap_at_rebal


# ============================================================
# 2. 지수 산출식 — 3.2절
# ============================================================
# I(t) = ( M(t) / B(t) ) x 1000.00
#   M(t) : 비교시가총액 = Σ IIF_i x 유동시총_i(리밸런싱일 고정) x [ 오늘 종가 / 리밸런싱일 종가 ]
#   B(t) : 기준시가총액(제수, divisor) — 리밸런싱/기업행위가 있어도 지수가 안 튀도록 관리하는 값

def calc_market_cap_M(ff_mcap_at_rebal: pd.Series, iif: pd.Series,
                       price_now: pd.Series, price_at_rebal: pd.Series) -> float:
    """오늘(price_now)과 리밸런싱일(price_at_rebal) 종가의 비율만큼만 반영해서
    비교시가총액 M(t)를 구합니다. (유동시총·주식수·IIF는 리밸런싱일 값으로 고정)"""
    price_ratio = price_now / price_at_rebal
    return float((iif * ff_mcap_at_rebal * price_ratio).sum())


def calc_index_level(M_t: float, B: float) -> float:
    """I(t) = (M(t)/B) x 1000.00"""
    return (M_t / B) * BASE_INDEX_LEVEL


def set_divisor(M_t0: float, level_t0: float) -> float:
    """리밸런싱 시점에, 지수가 직전 레벨(level_t0)에서 끊기지 않고 이어지도록
    만드는 제수(divisor)를 구합니다.  B = M(t0) x 1000 / level_t0
    (아주 처음 기준일이면 level_t0 = 1000 이므로 B(기준일) = M(기준일) 이 됩니다.)"""
    return M_t0 * BASE_INDEX_LEVEL / level_t0


def rebalance(prev_level: float, new_weights: pd.Series, ff_mcap_at_rebal: pd.Series) -> tuple[pd.Series, float]:
    """반기 정기변경(2.4절)일마다 이 함수를 호출하세요.
    직전 지수 레벨에서 끊기지 않게, 새 구성·비중에 맞는 IIF와 새 제수(B)를 만들어 돌려줍니다."""
    iif = calc_iif(new_weights, ff_mcap_at_rebal)
    M_t0 = float((iif * ff_mcap_at_rebal).sum())  # 이론상 ff_mcap_at_rebal.sum()과 같아야 함 (검증용)
    B = set_divisor(M_t0, prev_level)
    return iif, B


# ============================================================
# 3. 기준시가총액(제수) 수정 — 3.4절, 리밸런싱 사이의 기업행위 처리
# ============================================================
def adjust_base_market_cap(B_t: float, M_t: float, delta_M: float) -> float:
    """유상증자·합병·상장폐지처럼 리밸런싱 '사이'에 주식수가 갑자기 바뀌는 사건이 생기면,
    그 변화 자체 때문에 지수가 점프하지 않도록 제수(B)를 같이 조정합니다.
        B(t+1) = B(t) x ( M(t) ± ΔM(t+1) ) / M(t)
    ΔM: 변동 주식수 x (발행가액 또는 전일종가) — 사건으로 인한 비교시가총액 변동분"""
    return B_t * (M_t + delta_M) / M_t


# ============================================================
# 4. 여러 날짜에 걸친 지수 시계열 만들기 — 실제 사용 시 이 함수를 확장해서 쓰세요
# ============================================================
def build_daily_series(price_panel: pd.DataFrame, reconstitution_events: list[dict]) -> pd.Series:
    """
    price_panel : index=날짜, columns=종목코드, values=종가.
                  pykrx 등으로 전체 기간·전체 후보종목 종가를 미리 받아둔 표.
    reconstitution_events : 반기마다 한 건씩, 시간순으로 정렬된 리스트.
                  [{"date": "2020-06-15", "weights": pd.Series(...), "ff_mcap": pd.Series(...)}, ...]
                  (2.2.2/2.3 담당자의 결과물 — 반기마다 새로 나온 12종목·비중을 그대로 여기 넣으면 됨)

    반환: 날짜별 지수 레벨 pd.Series (기준일부터 마지막 리밸런싱 구간의 끝까지)

    ※ 지금은 하나의 뼈대입니다. 실제로 쓰려면:
        1) price_panel을 pykrx.stock.get_market_ohlcv 등으로 채우고
        2) reconstitution_events를 2.2.2/2.3 담당자의 결과(CSV/DataFrame)로 채운 뒤
        3) 이 함수를 그대로 호출하면 됩니다.
    """
    levels: dict = {}
    level = BASE_INDEX_LEVEL
    iif = ff_at_rebal = price_at_rebal = weights = None

    for i, event in enumerate(reconstitution_events):
        rebal_date = event["date"]
        weights = event["weights"]
        ff_mcap = event["ff_mcap"]

        price_at_rebal = price_panel.loc[rebal_date, weights.index]
        iif, B = rebalance(level, weights, ff_mcap)
        ff_at_rebal = ff_mcap
        levels[rebal_date] = level  # 리밸런싱 당일은 직전 레벨 그대로 (여기서 점프 없음)

        next_date = reconstitution_events[i + 1]["date"] if i + 1 < len(reconstitution_events) else price_panel.index[-1]
        period_dates = price_panel.loc[rebal_date:next_date].index[1:]  # 리밸런싱일 다음날부터

        for d in period_dates:
            price_now = price_panel.loc[d, weights.index]
            M_t = calc_market_cap_M(ff_at_rebal, iif, price_now, price_at_rebal)
            level = calc_index_level(M_t, B)
            levels[d] = level

    return pd.Series(levels).sort_index()


# ============================================================
# 데모 — Methodology 3.1/3.3 예시로 검증 (두 가지 계산법이 서로 일치하는지 교차 확인)
# ============================================================
if __name__ == "__main__":
    codes = ["005930", "000660", "042700", "222800", "067310",
              "095340", "098120", "066410", "155650", "357780", "014680", "240810"]
    names = ["삼성전자", "SK하이닉스", "한미반도체", "심텍", "하나마이크론",
              "ISC", "테크윙", "엠케이전자", "와이씨켐", "솔브레인", "한솔케미칼", "원익IPS"]

    # 2.3 담당자에게서 "받았다"고 가정하는 입력값 (Methodology 3.1 표 그대로)
    weights = pd.Series(
        [0.24, 0.16, 0.16875, 0.075, 0.05625, 0.045, 0.0375, 0.03, 0.01875, 0.0675, 0.06, 0.04125],
        index=codes,
    )
    ff_mcap_at_rebal = pd.Series(
        [3_000_000, 2_000_000, 45_000, 20_000, 15_000, 12_000, 10_000, 8_000, 5_000, 18_000, 16_000, 11_000],
        index=codes,
    )
    # Methodology 3.3의 "다음 거래일 등락률" 예시
    return_pct = pd.Series(
        [1.5, 2.5, 3.0, 1.0, 2.0, 1.0, 2.0, 3.0, 2.0, 0.0, -2.0, -1.0], index=codes
    )

    # --- 방법 A: 손계산과 똑같은 방식 (비중 x 등락률의 합) — 빠른 검증용 ---
    simple_return_pct = float((weights * return_pct).sum())
    simple_level = BASE_INDEX_LEVEL * (1 + simple_return_pct / 100)

    # --- 방법 B: 정식 산출식 (IIF + 제수 B) — 실제 파이프라인에서 쓸 방식 ---
    price_at_rebal = pd.Series(100_000, index=codes)          # 기준가는 임의값(비율만 의미 있음)
    price_now = price_at_rebal * (1 + return_pct / 100)

    iif, B = rebalance(prev_level=BASE_INDEX_LEVEL, new_weights=weights, ff_mcap_at_rebal=ff_mcap_at_rebal)
    M_t = calc_market_cap_M(ff_mcap_at_rebal, iif, price_now, price_at_rebal)
    formal_level = calc_index_level(M_t, B)

    print("=== 종목별 비중 (Methodology 3.1과 대조) ===")
    print(pd.DataFrame({"종목": names, "비중(%)": (weights.values * 100).round(2)}).to_string(index=False))

    print(f"\n방법 A (가중등락률, 손계산 검증용): {simple_return_pct:.2f}%  ->  {simple_level:.2f}")
    print(f"방법 B (IIF + 제수, 정식 산출식)   : {formal_level:.2f}")
    print("(Methodology 3.3 예시값             : +1.54%  ->  1015.40)")

    assert abs(simple_level - 1015.40) < 0.01, "방법 A가 문서 예시와 다릅니다"
    assert abs(formal_level - 1015.40) < 0.01, "방법 B(정식 산출식)가 문서 예시와 다릅니다"
    assert abs(simple_level - formal_level) < 0.01, "두 계산 방식의 결과가 서로 다릅니다"
    print("\n[확인] 두 계산 방식 모두 Methodology 3.3 예시와 정확히 일치합니다.")

    # --- 3.4 제수 조정 맛보기: 리밸런싱 사이에 기업행위(예: 유상증자)가 있으면 이렇게 처리 ---
    toy_delta_M = 50_000  # 예: 유상증자로 시총이 500억(단위: 억원 기준 5) 늘었다고 가정
    B_adjusted = adjust_base_market_cap(B, M_t, toy_delta_M)
    print(f"\n(3.4 참고) 기업행위로 ΔM={toy_delta_M:,}이 생겼다면 제수는 {B:,.0f} -> {B_adjusted:,.0f} 로 조정")
    print("이렇게 조정해야 다음날 지수가 '주가 변동'만 반영하고 '구성 변화'로는 점프하지 않습니다.")
