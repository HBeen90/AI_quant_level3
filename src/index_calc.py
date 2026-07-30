# -*- coding: utf-8 -*-
"""
index_calc.py - HBM 반도체 테마 지수: 구성종목 및 지수 산출 (Methodology Book 3장)
====================================================================
담당 범위: 3.1 최종 구성종목(입력값으로 받음) · 3.2 지수 산출식 · 3.3 산출 예시(검증) ·
           3.4 기준시가총액(제수) 수정 · 주식교부 합병(share-swap) 처리

이 파일이 "받는" 것과 "하는" 것
    받는 것 (다른 파트 담당자가 만들어 넘겨주는 값 - 2장 로직은 여기서 다시 만들 필요 없음)
        - 최종 구성종목 - 2.2.2/2.3 담당자 결과물 (3.1 표).
          v2(2026-07-23 개정)부터 고정 정원이 폐지되어 **종목 수는 가변**이며
          규칙 통과 종목 전원이 편입된다. 이 파일은 종목 수를 가정하지 않는다.
          (__main__ 데모의 12종목은 구판 3.3 손계산 예시 검증용으로만 유지)
        - 종목별 목표 비중(weight) - 2.3 비중 산정 담당자 결과물
        - 종목별 유동시가총액(ff_market_cap), 상장주식수, 종가 - pykrx 등으로 수집

    하는 것 (이 파일의 역할)
        - 위 입력을 받아 실제 "지수 숫자(포인트)"를 계산
        - 리밸런싱마다 구성이 바뀌어도 지수가 튀지 않게(불연속 없이) 이어붙이는 로직
        - 주식교부 합병(피합병사 편출 + 존속회사 주식수 증가)을 최종 목표비중
          이벤트로 변환 — simulate_index(소연)가 그대로 소비할 수 있는 형태
        - Methodology 3.3의 손계산 예시(1000.00 -> 1015.40)와 코드 결과가 일치하는지 검증

"""

from __future__ import annotations
import pandas as pd


# ============================================================
# CONFIG
# ============================================================
BASE_DATE = "2020-06-15"     # 기준일 - 이 날짜의 지수를 아래 값으로 둔다
BASE_INDEX_LEVEL = 1000.00   # 기준지수 - 지수의 "시작 눈금"


# ============================================================
# 0. 입력 데이터 불러오기
# ============================================================
def load_constituents(path: str) -> pd.DataFrame:
    """팀 공유 형식의 CSV를 읽습니다.

    종목코드는 반드시 문자열로 읽어야 합니다 - dtype 지정을 안 하면 판다스가 숫자로 인식해서
    '005930' 같은 앞자리 0이 '5930'으로 사라집니다.
    최소 컬럼: 코드, 종목명, bucket(anchor/core/satellite), weight, ff_market_cap
    (종가는 이 CSV가 아니라 별도 price_panel로 받는다 - 실제 인계 CSV엔 price 컬럼 없음)
    """
    return pd.read_csv(path, dtype={"코드": str, "code": str})


# ============================================================
# 종목 구성 일치 확인 (방어용)
# ============================================================
# 아래 함수들은 종목별 Series 여러 개를 곱하거나 나누는데, 그중 하나에서라도
# 종목이 하나 빠져있으면(예: 편출된 종목을 실수로 price_now에서만 안 지움)
# 판다스가 에러 없이 그 종목을 NaN 처리하고, sum()이 NaN을 조용히 0 취급해서
# "그럴듯하지만 틀린" 숫자가 나온다. 이를 막기 위해 계산 전에 종목 구성이
# 정확히 같은지 확인한다.

def _require_same_tickers(*series: pd.Series) -> None:
    tickers = [set(s.index) for s in series]
    if any(t != tickers[0] for t in tickers):
        raise ValueError(f"입력 Series들의 종목 구성이 서로 다릅니다: {[sorted(t) for t in tickers]}")


# ============================================================
# 1. IIF(지수포함가중치) 산정 - "목표 비중대로 지수가 움직이게" 만드는 보정계수
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
    _require_same_tickers(target_weights, ff_mcap_at_rebal)
    total_raw = ff_mcap_at_rebal.sum()
    return target_weights * total_raw / ff_mcap_at_rebal


# ============================================================
# 2. 지수 산출식 - 3.2절
# ============================================================
# I(t) = ( M(t) / B(t) ) x 1000.00
#   M(t) : 비교시가총액 = Σ IIF_i x 유동시총_i(리밸런싱일 고정) x [ 오늘 종가 / 리밸런싱일 종가 ]
#   B(t) : 기준시가총액(제수, divisor) - 리밸런싱/기업행위가 있어도 지수가 안 튀도록 관리하는 값

def calc_market_cap_M(ff_mcap_at_rebal: pd.Series, iif: pd.Series,
                       price_now: pd.Series, price_at_rebal: pd.Series) -> float:
    """오늘(price_now)과 리밸런싱일(price_at_rebal) 종가의 비율만큼만 반영해서
    비교시가총액 M(t)를 구합니다. (유동시총·주식수·IIF는 리밸런싱일 값으로 고정)"""
    _require_same_tickers(ff_mcap_at_rebal, iif, price_now, price_at_rebal)
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
# 3. 기준시가총액(제수) 수정 - 3.4절, 리밸런싱 사이의 기업행위 처리
# ============================================================
def adjust_divisor_for_dividend(B_t: float, M_ex: float,
                                dividend_M: float) -> float:
    """배당락일 제수 보정 - TR(총수익) 지수 경로. 제6조.

    부호 규약을 명확히 한다(리뷰 지적의 핵심이자 실수하기 쉬운 지점).

        M_ex        : **배당락일 종가**로 계산한 비교시가총액.
                      이미 배당락 하락이 반영된 값이다.
        dividend_M  : 배당 총액을 M 단위로 환산한 값 (양수)
                      = Σ IIF_i x FF_i x S_i x DPS_i
                      = Σ iif_i x ff_mcap_i x (dps_i / p0_i)

        B(t+1) = B(t) x M_ex / (M_ex + dividend_M)

    제수가 **작아지므로** 지수는 배당락 하락분만큼 올라가 상쇄된다.
    즉 TR 지수는 배당락일에 점프하지 않는다.

    주의 - PR 지수에서 배당락 하락은 오류가 아니라 정의다.
    가격지수(Price Return)는 배당을 제외하므로 배당락일 하락을 그대로
    반영하는 것이 정상 동작이다. 이 함수는 PR을 '고치는' 것이 아니라
    별도의 TR 계열을 만드는 것이다. 두 계열은 병기하며, 어느 쪽을 공식
    지수로 삼을지는 위원회 결정 사항이다.

    문헌에 자주 보이는 `B x (M - D) / M` 형태는 M을 **배당락 직전**
    시가총액으로 둔 표기다. M_pre = M_ex + D 를 대입하면 위 식과 같다.
    엔진은 배당락일 종가만 갖고 있으므로 M_ex 기준을 채택한다.
    """
    if dividend_M < 0:
        raise ValueError("dividend_M 은 양수여야 한다(배당 총액). "
                         "자본환급·특별배당은 3.4 제수 조정 경로로 처리할 것 - "
                         "여기에 넣으면 이중반영이 된다.")
    denom = M_ex + dividend_M
    if denom <= 0:
        raise ValueError(f"배당 보정 분모 비양수: M_ex={M_ex}, D={dividend_M}")
    return B_t * M_ex / denom


def adjust_base_market_cap(B_t: float, M_t: float, delta_M: float) -> float:
    """유상증자·합병·상장폐지처럼 리밸런싱 '사이'에 주식수가 갑자기 바뀌는 사건이 생기면,
    그 변화 자체 때문에 지수가 점프하지 않도록 제수(B)를 같이 조정합니다.
        B(t+1) = B(t) x ( M(t) ± ΔM(t+1) ) / M(t)
    ΔM: 변동 주식수 x (발행가액 또는 전일종가) - 사건으로 인한 비교시가총액 변동분"""
    return B_t * (M_t + delta_M) / M_t


# ============================================================
# 3-1. 주식교부 합병(share-swap) 처리
# ============================================================
# simulate_index는 "날짜별 최종 목표비중" 이벤트만 소비할 수 있고, 주식교부
# 합병(피합병사 편출 + 존속회사 주식수 증가)을 그 이벤트 형태로 바꿔주는 계산은
# index_calc(제수) 영역으로 남겨져 있었다. 그 변환을 여기서 만든다.
#
# 핵심 아이디어: 주식교부 합병은 피합병사 주주가 존속회사 주식을 받는 거래이므로,
# 피합병사가 갖고 있던 비중은 사라지는 게 아니라 그대로 존속회사로 넘어간다.
# 나머지 종목 비중은 이 거래와 무관하므로 건드릴 이유가 없다. 즉:
#   새 존속회사 비중 = 기존 존속회사 비중 + 기존 피합병사 비중
#   나머지 종목 비중 = 그대로, 피합병사는 제외
# 이렇게 비중을 "이동"만 시키면 합계가 항상 정확히 1.0로 유지되어 재정규화가
# 필요 없다.

def calc_current_weights(ff_mcap_at_rebal: pd.Series, iif: pd.Series,
                          price_now: pd.Series, price_at_rebal: pd.Series) -> pd.Series:
    """지금 이 상태(iif)로, 오늘(price_now) 시점 각 종목의 실제 비중을 역산한다.
    (calc_market_cap_M과 같은 식이지만, 합계를 내기 전 종목별 값을 비중으로 돌려준다.
    합병처럼 리밸런싱일이 아닌 중간 날짜에도 "지금 비중"이 필요해서 별도로 둔다.)"""
    _require_same_tickers(ff_mcap_at_rebal, iif, price_now, price_at_rebal)
    contribution = iif * ff_mcap_at_rebal * (price_now / price_at_rebal)
    return contribution / contribution.sum()


def apply_merger(weights: pd.Series, target: str, acquirer: str) -> pd.Series:
    """주식교부 합병 발생 시 최종 목표비중을 만든다.
    target(피합병사) 비중을 그대로 acquirer(존속회사)로 넘기고 target은 제외한다.
    반환값은 simulate_index의 목표비중 이벤트나 이 파일의 rebalance()에 그대로
    넣을 수 있는 형태(합계 1.0인 pd.Series)다."""
    if target == acquirer:
        raise ValueError(f"target과 acquirer가 같습니다({target}) — 합병이 성립하지 않습니다")
    if target not in weights.index or acquirer not in weights.index:
        raise ValueError(f"target({target}) 또는 acquirer({acquirer})가 현재 구성종목에 없습니다")
    new_weights = weights.drop(target).copy()
    new_weights[acquirer] = new_weights[acquirer] + weights[target]
    return new_weights


# ============================================================
# 4. 여러 날짜에 걸친 지수 시계열 만들기 — 실제 사용 시 이 함수를 확장해서 쓰세요

# ============================================================
def build_daily_series(price_panel: pd.DataFrame, reconstitution_events: list[dict]) -> pd.Series:
    """
    price_panel : index=날짜, columns=종목코드, values=종가.
                  pykrx 등으로 전체 기간·전체 후보종목 종가를 미리 받아둔 표.
    reconstitution_events : 반기마다 한 건씩, 시간순으로 정렬된 리스트.
                  [{"date": "2020-06-15", "weights": pd.Series(...), "ff_mcap": pd.Series(...)}, ...]
                  (2.2.2/2.3 담당자의 결과물 - 반기마다 확정된 가변 구성·비중을 그대로 넣으면 됨)

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
# 4b. 정기 + 수시를 한 경로로 - 3.4 제수 조정을 시계열에 연결
# ============================================================
# build_daily_series 는 정기변경 전용이라, 수시편출·기업행위가 오면 호출자가
# 매번 저수준 조합을 손으로 써야 했다(접합 검증 노트 3-2 제안). 그 참조
# 구현(tests/test_index_calc_equivalence.py 대조 B)을 여기 함수로 올린다.
#
# 조문 대응
#   정기변경  : IIF 재산정 + 제수 리셋   -> rebalance()
#   수시편출  : IIF 재배분 없음. ΔM = -(편출 종목 기여분) 으로 제수만 조정
#               -> 잔여 종목의 상대비중이 '기계적으로' 상승 (3장 수시변경 조문)
#   기업행위  : ΔM = 변동 주식수 x 발행가액(또는 전일종가) - 호출자가 제공
#
# 소연 파트의 '드리프트 후 정규화' 이벤트와 수학적으로 동치임은 동치성 대조
# 테스트 B가 1e-15 수준으로 실증한다.

def build_index_series(price_panel: pd.DataFrame,
                       reconstitution_events: list[dict],
                       adhoc_events: list[dict] | None = None) -> pd.DataFrame:
    """정기변경 + 수시편출 + 기업행위 + 배당(TR)을 하나의 시계열로 산출한다.

    price_panel : index=날짜, columns=종목코드, values=종가
    reconstitution_events : [{"date", "weights"(Series), "ff_mcap"(Series)}, ...]
    adhoc_events : [{"date", "kind": ..., ...}, ...]
        · exclusion    : {"tickers": [코드, ...]}
                         ΔM 을 엔진이 계산한다(편출 종목의 당일 기여분, 음수).
                         대체 편입은 하지 않는다 - 무대체 원칙.
        · share_change : {"ticker": 코드, "delta_M": float}
                         유상증자·주식교부 합병 등. 호출자가 ΔM 을 준다.
                         제수(B)만 조정하고 해당 종목의 유동시총 기준을 그대로
                         두면, 다음날부터 M(t) 계산이 옛 크기로 되돌아가서
                         "가격 변화 없이 지수가 영구적으로 어긋나는" 왜곡이
                         생긴다. 그래서 ticker의 ff·기준가(p0)도 오늘 시점
                         기준으로 함께 재설정한다 - 그래야 내일부터 그 종목의
                         기여분이 새 크기 기준으로 계속 이어진다.
        · dividend     : {"dps": pd.Series}  ← TR 계열 전용
                         배당락일 주당 배당금. 엔진이 M 단위로 환산해
                         adjust_divisor_for_dividend 로 제수를 보정한다.
                         **PR 계열을 만들 때는 이 이벤트를 넣지 않는다** -
                         PR에서 배당락 하락은 오류가 아니라 정의다.

    반환 : DataFrame[level, n_members, event]  (날짜 오름차순)

    fail-closed : 활성 구성종목의 가격이 결측·비양수면 0%로 대체하지 않고
    예외를 던진다. 지수는 '재현되는 숫자'라 조용한 보정이 가장 위험하다.
    """
    if not reconstitution_events:
        raise ValueError("정기변경 이벤트가 최소 1건 필요합니다")
    recon = sorted(reconstitution_events, key=lambda e: pd.Timestamp(e["date"]))
    adhoc: dict = {}
    for e in (adhoc_events or []):
        adhoc.setdefault(pd.Timestamp(e["date"]), []).append(e)

    start = pd.Timestamp(recon[0]["date"])
    dates = price_panel.index[price_panel.index >= start]
    recon_by_date = {pd.Timestamp(e["date"]): e for e in recon}

    # 이벤트 날짜가 price_panel의 실제 거래일과 하나라도 안 맞으면(공휴일·주말
    # 착오, 날짜 포맷 실수 등) 그 이벤트는 for d in dates 루프에서 영영 안
    # 걸리고 조용히 무시된다 - members가 끝까지 비어서 level=1000, n_members=0
    # 껍데기 결과가 에러 없이 나올 수 있다. 미리 막는다.
    all_event_dates = list(recon_by_date) + list(adhoc)
    missing = sorted({d.date() for d in all_event_dates if d not in price_panel.index})
    if missing:
        raise ValueError(f"이벤트 날짜가 price_panel 거래일에 없습니다(공휴일/주말 "
                         f"착오 등 확인): {missing}")

    rows: list = []
    level = BASE_INDEX_LEVEL
    iif = ff = p0 = None
    members: list = []
    B = None

    for d in dates:
        tag = ""
        # ① 먼저 '기존 구성'으로 그날 지수를 갱신한다. 정기변경일에도 마찬가지 -
        #    조문상 구성은 개편일 종가로 확정하고 정기변경일 '종가에' 적용하므로,
        #    당일 장중 수익률은 직전 구성의 것이다. (simulate_index 와 동일 순서)
        px_now = M = None
        if members:
            px_now = price_panel.loc[d, members]
            _check_prices(px_now, d, "산출일")
            M = calc_market_cap_M(ff[members], iif[members], px_now, p0[members])
            level = calc_index_level(M, B)

        if d in recon_by_date:                       # ② 정기변경 - 제수 리셋
            if adhoc.get(d):
                raise ValueError(
                    f"{d.date()} 정기변경일과 수시 이벤트가 겹칩니다. 조문상 "
                    "'편출을 선반영한 최종 구성 이벤트 하나로 원자 처리'이므로 "
                    "reconstitution_events 쪽에 미리 병합해 넣으십시오 "
                    "(backtest.build_event_schedule 과 동일 규칙).")
            e = recon_by_date[d]
            w = e["weights"]
            ff = e["ff_mcap"].reindex(w.index)
            if ff.isna().any() or (ff <= 0).any():
                raise ValueError(f"{d.date()} 유동시총 결측·비양수: "
                                 f"{ff.index[ff.isna() | (ff <= 0)].tolist()}")
            members = list(w.index)
            p0 = price_panel.loc[d, members]
            _check_prices(p0, d, "정기변경일")
            iif, B = rebalance(level, w, ff)         # 직전 레벨에서 이어붙임
            rows.append({"date": d, "level": level,
                         "n_members": len(members), "event": "regular"})
            continue

        for e in adhoc.get(d, []):                   # ③ 수시 - 제수만 조정
            if not members:
                raise ValueError(f"{d.date()} 구성 확정 전 수시 이벤트")
            if e["kind"] == "exclusion":
                gone = [t for t in e["tickers"] if t in members]
                if not gone:
                    continue
                if len(gone) == len(members):
                    raise ValueError(f"{d.date()} 전 종목 편출 - 산출 불가")
                contrib = float((iif[gone] * ff[gone]
                                 * px_now[gone] / p0[gone]).sum())
                B = adjust_base_market_cap(B, M, -contrib)   # ΔM < 0
                members = [t for t in members if t not in gone]
                tag = (tag + "+exclusion").strip("+")
            elif e["kind"] == "share_change":
                ticker = e["ticker"]
                if ticker not in members:
                    raise ValueError(f"{d.date()} share_change 대상 종목"
                                     f"({ticker})이 현재 구성에 없습니다")
                delta_M = float(e["delta_M"])
                old_contrib = float(iif[ticker] * ff[ticker]
                                    * px_now[ticker] / p0[ticker])
                new_contrib = old_contrib + delta_M
                if new_contrib <= 0:
                    raise ValueError(f"{d.date()} share_change 결과 {ticker} "
                                     f"기여분이 비양수({new_contrib})")
                B = adjust_base_market_cap(B, M, delta_M)
                # 오늘을 새 기준일로 재설정 - 그래야 다음날부터 이 종목의
                # 기여분이 new_contrib에서 가격 변화만큼만 움직인다.
                ff = ff.copy()
                p0 = p0.copy()
                ff[ticker] = new_contrib / iif[ticker]
                p0[ticker] = px_now[ticker]
                tag = (tag + "+share_change").strip("+")
            elif e["kind"] == "dividend":                # TR 계열
                raw_dps = e["dps"].reindex(members)
                dps = pd.to_numeric(raw_dps, errors="coerce")
                bad_numeric = raw_dps.notna() & dps.isna()
                if bad_numeric.any():
                    raise ValueError(f"{d.date()} 배당 숫자 변환 실패: "
                                     f"{dps.index[bad_numeric].tolist()}")
                dps = dps.fillna(0.0)
                if (dps < 0).any():
                    raise ValueError(f"{d.date()} 음수 배당: "
                                     f"{dps.index[dps < 0].tolist()}")
                # 배당 총액의 M 환산 = Σ iif_i x ff_i x (dps_i / p0_i)
                div_M = float((iif[members] * ff[members]
                               * dps / p0[members]).sum())
                if div_M > 0:
                    B = adjust_divisor_for_dividend(B, M, div_M)
                    # 제수가 바뀌었으므로 당일 레벨을 다시 계산한다(배당락 상쇄)
                    level = calc_index_level(M, B)
                    tag = (tag + "+dividend").strip("+")
            else:
                raise ValueError(f"알 수 없는 수시 이벤트 kind: {e['kind']}")

        rows.append({"date": d, "level": level,
                     "n_members": len(members), "event": tag})

    return pd.DataFrame(rows).set_index("date")


def _check_prices(px: pd.Series, d, where: str) -> None:
    bad = px.index[px.isna() | (px <= 0)]
    if len(bad):
        raise ValueError(f"{where} {pd.Timestamp(d).date()} 활성 구성종목 "
                         f"가격 결측·비양수: {bad.tolist()} (0% 대체 금지)")


# ============================================================
# 데모 - Methodology 3.1/3.3 예시로 검증 (두 가지 계산법이 서로 일치하는지 교차 확인)
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

    # --- 방법 A: 손계산과 똑같은 방식 (비중 x 등락률의 합) - 빠른 검증용 ---
    simple_return_pct = float((weights * return_pct).sum())
    simple_level = BASE_INDEX_LEVEL * (1 + simple_return_pct / 100)

    # --- 방법 B: 정식 산출식 (IIF + 제수 B) - 실제 파이프라인에서 쓸 방식 ---
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

    # --- 주식교부 합병 맛보기: 심텍(피합병사) -> 하나마이크론(존속회사), 순수 예시 ---
    target, acquirer = codes[3], codes[4]
    level_before = formal_level  # 합병 시행일(= price_now 시점) 직전 지수

    w_now = calc_current_weights(ff_mcap_at_rebal, iif, price_now, price_at_rebal)
    w_merged = apply_merger(w_now, target=target, acquirer=acquirer)

    ff_mcap_post = ff_mcap_at_rebal.drop(target).copy()
    ff_mcap_post[acquirer] += ff_mcap_at_rebal[target]  # 존속회사가 피합병사 몫만큼 커짐(예시)
    price_post = price_now.drop(target)                 # 합병 시행일 종가 = 새 리밸런싱 기준가

    iif2, B2 = rebalance(prev_level=level_before, new_weights=w_merged, ff_mcap_at_rebal=ff_mcap_post)
    level_after = calc_index_level(calc_market_cap_M(ff_mcap_post, iif2, price_post, price_post), B2)

    print(f"\n(합병 예시) {names[3]} -> {names[4]} 합병 처리 직전 지수: {level_before:.6f}")
    print(f"(합병 예시) 합병 반영 직후(같은 날) 지수        : {level_after:.6f}")
    assert abs(level_before - level_after) < 1e-6, "합병 반영 전후 지수가 끊깁니다"
    print("[확인] 합병을 반영해도 그 시점 지수는 점프하지 않습니다(연속성 유지 — target 비중이 그대로 acquirer로 이동했을 뿐).")
