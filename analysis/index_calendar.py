# -*- coding: utf-8 -*-
"""
index_calendar.py - 방법론 3장 일정 조문의 결정론적 구현
========================================================
조문(README/methodology 3장)
    · 종목 선정일 : 매년 5월·11월 말 마지막 영업일
    · 정기변경일  : 6월·12월 선물·옵션 만기일(둘째 목요일) 익주 첫 영업일
    · 발표일      : 정기변경일 2영업일 전(개편일) 종가로 확정·사전 공표

원칙
----
휴장일 캘린더를 별도로 들고 있지 않는다. **실제 거래일 인덱스(가격 패널의
index)를 유일한 캘린더로 삼는다** - pykrx가 준 거래일이 곧 진실이며,
공휴일 테이블을 손으로 관리하면서 생기는 불일치를 원천 차단한다.

'둘째 목요일'은 달력 연산(휴장 무관)으로 구하고, 그 뒤 '익주 첫 영업일'과
'2영업일 전'만 거래일 인덱스에 투영한다. 만기일 자체가 휴장이어도 익주
월요일 기준은 변하지 않으므로 조문 해석이 안전하다.
"""
from __future__ import annotations

import datetime as _dt
import os

import pandas as pd


def as_of_today() -> pd.Timestamp:
    """분석 기준 '오늘'.

    스크립트마다 날짜를 하드코딩하면 다음 달에 조용히 낡는다(실제로 세 파일에
    2026-07-26 이 박혀 있었다). 여기 한 곳으로 모으고, 재현이 필요할 때는
    환경변수로 고정한다(PowerShell):  $env:INDEX_ASOF = "2026-07-26"
    """
    v = os.environ.get("INDEX_ASOF")
    return pd.Timestamp(v) if v else pd.Timestamp(_dt.date.today())

SELECTION_MONTHS = (5, 11)      # 종목 선정일이 있는 달
REBALANCE_MONTHS = (6, 12)      # 정기변경일이 있는 달
ANNOUNCE_LAG = 2                # 개편일 = 정기변경일 D-2 영업일


def second_thursday(year: int, month: int) -> pd.Timestamp:
    """해당 월의 둘째 목요일(선물·옵션 만기일). 달력 연산 - 휴장 무관."""
    first = pd.Timestamp(year=year, month=month, day=1)
    # Monday=0 ... Thursday=3
    offset = (3 - first.dayofweek) % 7
    return first + pd.Timedelta(days=offset + 7)


def next_week_monday(day: pd.Timestamp) -> pd.Timestamp:
    """주어진 날짜가 속한 주의 '다음 주 월요일'(달력)."""
    return day + pd.Timedelta(days=7 - day.dayofweek)


def rebalance_dates(trading_days: pd.DatetimeIndex,
                    months: tuple = REBALANCE_MONTHS) -> list:
    """정기변경 시행일 = 만기일 익주 첫 '거래일'.

    trading_days : 실제 거래일 인덱스(가격 패널 index). 유일한 캘린더.
    반환 : 거래일 인덱스에 실재하는 Timestamp 리스트(오름차순, 중복 제거).
    """
    td = pd.DatetimeIndex(trading_days).sort_values()
    out: list = []
    for y in range(td[0].year, td[-1].year + 1):
        for m in months:
            target = next_week_monday(second_thursday(y, m))
            pos = td.searchsorted(target, side="left")     # 첫 거래일 >= 월요일
            if pos < len(td):
                out.append(td[pos])
    return sorted(set(d for d in out if td[0] <= d <= td[-1]))


def selection_dates(trading_days: pd.DatetimeIndex,
                    months: tuple = SELECTION_MONTHS) -> list:
    """종목 선정일 = 5월·11월의 마지막 거래일."""
    td = pd.DatetimeIndex(trading_days).sort_values()
    s = pd.Series(td, index=td)
    out = []
    for y in range(td[0].year, td[-1].year + 1):
        for m in months:
            sel = s[(s.index.year == y) & (s.index.month == m)]
            if len(sel):
                out.append(sel.index[-1])
    return sorted(out)


def announce_date(trading_days: pd.DatetimeIndex,
                  rebal_date: pd.Timestamp,
                  lag: int = ANNOUNCE_LAG) -> pd.Timestamp:
    """개편일(사전 공표일) = 정기변경일 lag 영업일 전."""
    td = pd.DatetimeIndex(trading_days).sort_values()
    i = td.get_loc(pd.Timestamp(rebal_date))
    if i - lag < 0:
        raise ValueError(f"{rebal_date.date()} 의 D-{lag} 영업일이 데이터 구간 밖")
    return td[i - lag]


def pair_selection_to_rebalance(trading_days: pd.DatetimeIndex) -> list:
    """(종목 선정일, 정기변경 시행일) 짝. 선정일 직후의 첫 시행일과 맺는다.

    5월 말 선정 -> 6월 시행, 11월 말 선정 -> 12월 시행.
    스냅샷 파일 계보(selection_date)와 시행일 매핑의 단일 근거로 쓴다.
    """
    rebs = rebalance_dates(trading_days)
    pairs = []
    for s in selection_dates(trading_days):
        nxt = [r for r in rebs if r > s]
        if nxt:
            pairs.append((s, nxt[0]))
    return pairs


if __name__ == "__main__":
    # 거래일 캘린더 없이 영업일(주말만 제외)로 형태 확인 - 실사용은 pykrx 거래일
    td = pd.bdate_range("2020-01-01", "2026-12-31")
    print("정기변경일(영업일 근사):")
    for d in rebalance_dates(td):
        print("  ", d.date(), d.day_name(), "| 개편일", announce_date(td, d).date())
    print("\n선정일-시행일 짝:")
    for s, r in pair_selection_to_rebalance(td):
        print("  ", s.date(), "->", r.date())
