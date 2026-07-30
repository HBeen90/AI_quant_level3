# -*- coding: utf-8 -*-
"""배당 수집기의 오프라인 로직 회귀 테스트.

네트워크(pykrx DPS 조회)는 검증 대상이 아니다. 검증 대상은 **날짜를 어떻게
고르는가**이며, 여기가 틀리면 배당락일이 하루씩 밀린 TR 계열이 조용히 나온다.
달력을 추측하지 않고 실제 거래일 인덱스에서만 뽑는지를 고정한다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from analysis.collect_dividends import date_plan, last_trading_day

PASS = []


def ok(name):
    PASS.append(name)
    print(f"[OK] {name}")


def idx(dates):
    return pd.DatetimeIndex(pd.to_datetime(dates))


def test_last_trading_day_picks_actual_session():
    """12/31이 휴장이면 그 전 거래일을 골라야 한다. 달력 마지막 날이 아니다."""
    i = idx(["2020-12-28", "2020-12-29", "2020-12-30"])   # 12/31 휴장
    assert last_trading_day(i, 2020, 12) == pd.Timestamp("2020-12-30")
    ok("12월 마지막 거래일 - 휴장일 아닌 실제 세션 선택")


def test_last_trading_day_absent_returns_none():
    """해당 월 거래일이 없으면 None. 임의로 인접 월을 끌어오면 안 된다."""
    i = idx(["2020-06-15", "2020-07-01"])
    assert last_trading_day(i, 2020, 12) is None
    ok("거래일 없는 달 - None 반환(인접 월 대체 금지)")


def test_date_plan_marks_unusable_years():
    """배당락일이나 조회일이 패널 밖이면 '사용가능=False' 로 표시해야 한다.

    조용히 건너뛰면 그 해 배당이 통째로 빠진 TR 계열이 나오고, 아무도
    모른다. 표에 남겨야 사람이 본다.
    """
    i = idx(["2020-12-30", "2021-06-30"])          # FY2020만 완비
    plan = date_plan(i, [2020, 2021])
    row20 = plan[plan["fiscal_year"] == 2020].iloc[0]
    row21 = plan[plan["fiscal_year"] == 2021].iloc[0]
    assert bool(row20["사용가능"])
    assert row20["ex_date"] == pd.Timestamp("2020-12-30")
    assert row20["dps_query_date"] == pd.Timestamp("2021-06-30")
    assert not bool(row21["사용가능"]), row21      # 2021-12 거래일 없음
    ok("사업연도별 날짜 계획 - 사용 불가 연도를 표에 명시")


def test_query_date_is_after_ex_date():
    """DPS 조회일은 배당락일보다 뒤여야 한다.

    FY Y 배당액은 Y+1년 3월 주총에서 확정된다. 조회일이 배당락일보다 앞서면
    확정 전 값을 읽는 것이라 금액이 틀린다.
    """
    i = idx([f"2020-12-{d}" for d in (28, 29, 30)]
            + [f"2021-06-{d}" for d in (28, 29, 30)])
    plan = date_plan(i, [2020])
    r = plan.iloc[0]
    assert r["dps_query_date"] > r["ex_date"], r
    assert r["dps_query_date"].year == r["ex_date"].year + 1
    ok("DPS 조회일이 배당락일 이후 · 이듬해 - 확정 전 값 조회 차단")


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
    print(f"\n{len(PASS)}/{len(PASS)} 배당 수집기 테스트 통과")
