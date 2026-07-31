# -*- coding: utf-8 -*-
"""구간별 Robustness 회귀 테스트 (합성 시계열, 네트워크 불요).

무엇을 고정하는가
  이 모듈의 값어치는 계산식이 아니라 **구간을 어떻게 나눴는가**에 있다.
  우리 지수의 성과를 보고 자르면 데이터 스누핑이므로, 그러지 않았다는
  성질을 테스트로 못 박는다.

  1. 캘린더 경계가 코드 상수이고 지수 데이터를 참조하지 않는다
  2. 앵커 대용이 규칙 0 종목으로만 구성된다(선정 판단의 결과가 아님)
  3. 구간 분할이 전 구간을 빠짐없이 덮는다(구간 누락 = 조용한 은폐)
  4. 불연속 구간에서 MDD 를 산출하지 않는다(의미 없는 수치 방지)
  5. 포착률 정의가 '지수 평균 / 대용 평균'이다
"""
from __future__ import annotations

import inspect
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "analysis"))

import regime_robustness as rr  # noqa: E402


def _series(n: int = 900, seed: int = 3):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2020-06-15", periods=n)
    lv = pd.Series(1000 * np.exp(np.cumsum(rng.normal(0.0008, 0.02, n))),
                   index=idx)
    px = pd.DataFrame({
        "005930": 70000 * np.exp(np.cumsum(rng.normal(0.0004, 0.015, n))),
        "000660": 90000 * np.exp(np.cumsum(rng.normal(0.0006, 0.018, n))),
    }, index=idx)
    return lv, px


# ------------------------------------------------ 1. 구간 정의의 독립성
def test_calendar_regimes_are_constants():
    """경계가 상수로 박혀 있어야 한다. 함수가 계산하면 스누핑 여지가 생긴다."""
    assert isinstance(rr.CALENDAR_REGIMES, list)
    assert len(rr.CALENDAR_REGIMES) >= 3
    for a, b, name, note in rr.CALENDAR_REGIMES:
        assert pd.Timestamp(a) < pd.Timestamp(b)
        assert name and note


def test_calendar_table_does_not_read_index_to_choose_bounds():
    """calendar_table 소스에 경계를 데이터에서 유도하는 코드가 없어야 한다."""
    src = inspect.getsource(rr.calendar_table)
    for bad in ("cummax", "idxmax", "idxmin", "quantile", "sort_values"):
        assert bad not in src, f"경계 선택에 데이터 의존 흔적: {bad}"


def test_anchor_proxy_uses_only_rule_zero_tickers():
    assert set(rr.ANCHORS) == {"005930", "000660"}
    src = inspect.getsource(rr.anchor_proxy)
    assert "ANCHORS" in src


def test_calendar_regimes_cover_the_whole_sample():
    """구간 사이에 빈틈이 있으면 성과가 조용히 누락된다."""
    bounds = [(pd.Timestamp(a), pd.Timestamp(b))
              for a, b, _, _ in rr.CALENDAR_REGIMES]
    for (a1, b1), (a2, _) in zip(bounds, bounds[1:]):
        gap = (a2 - b1).days
        assert gap <= 1, f"구간 사이 {gap}일 공백: {b1.date()} -> {a2.date()}"


# ------------------------------------------------ 2. 산출
def test_calendar_table_has_total_row():
    lv, _ = _series()
    t = rr.calendar_table(lv)
    assert (t["구간"] == "전 구간").any(), "전 구간 대조행이 없다"


def test_drawdown_split_is_exhaustive():
    """상승 + 조정 거래일 합이 전체와 같아야 한다."""
    lv, px = _series()
    t = rr.drawdown_table(lv, rr.anchor_proxy(px))
    assert int(t["거래일"].sum()) == len(lv)


def test_drawdown_table_does_not_report_mdd():
    """불연속 구간의 MDD 는 의미가 없으므로 내지 않는다."""
    lv, px = _series()
    t = rr.drawdown_table(lv, rr.anchor_proxy(px))
    assert t["MDD"].isna().all()
    assert t["비고"].str.contains("불연속").all()


def test_capture_ratio_definition():
    lv, px = _series()
    proxy = rr.anchor_proxy(px)
    t = rr.capture_table(lv, proxy)
    assert len(t) == 2
    for _, r in t.iterrows():
        if pd.notna(r.get("포착률")):
            assert abs(r["포착률"]
                       - r["지수 평균수익률"] / r["대용 평균수익률"]) < 1e-12


def test_capture_splits_all_nonzero_days():
    """부호가 0 이 아닌 날은 상승·하락 중 하나에 속해야 한다."""
    lv, px = _series()
    proxy = rr.anchor_proxy(px).reindex(lv.index).ffill()
    pr = proxy.pct_change()
    lr = lv.pct_change()
    both = pd.DataFrame({"i": lr, "p": pr}).dropna()
    nonzero = int((np.sign(both["p"]) != 0).sum())
    t = rr.capture_table(lv, proxy)
    assert int(t["일수"].sum()) == nonzero
