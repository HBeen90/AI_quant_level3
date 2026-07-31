# -*- coding: utf-8 -*-
"""집중도·복제 가능성 계량 회귀 테스트 (합성 데이터, pykrx 불필요).

고정하려는 성질

  집중도
    1. 균등 n종목의 유효종목수는 정확히 n 이다 (HHI 정의 자기검증)
    2. 앵커 쏠림 구성에서 유효종목수 < 명목 종목수 ― 명목만 보고하면
       숨는 사실을 지표가 실제로 드러내는가
    3. 일별 집중도의 최대비중은 리셋 시점 최대비중 이상이다
       ― 드리프트 구간을 빠뜨리면 이 부등식이 깨진다

  복제 가능성
    4. 제약이 전혀 없으면(지연 0 · 참여율 무한 · 비용 0) tracker 는
       지수와 일치한다 ― 복제기가 지수를 재현조차 못 하면 그 위에서 잰
       추종오차는 제약의 효과가 아니라 버그다
    5. 체결 지연을 늘리면 추종오차가 줄지 않는다
    6. ADV 제약을 조이면 목표 도달이 늦어진다
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "analysis"))

from backtest.backtest import build_event_schedule, simulate_index  # noqa: E402
from src.rebalance import ANCHOR, CORE, SAT, ConfigV2  # noqa: E402

import concentration_replication as cr  # noqa: E402

COLS = ["ticker", "name", "group", "exposure", "mem_ratio", "float_mcap",
        "eligible"]


def _fixture(n_days: int = 260, seed: int = 3):
    rng = np.random.default_rng(seed)
    tickers = ["000001", "000002"] + [f"10000{i}" for i in range(1, 5)] \
        + [f"20000{i}" for i in range(1, 3)]
    days = pd.bdate_range("2024-06-17", periods=n_days)
    px = pd.DataFrame(
        100000 * np.exp(np.cumsum(rng.normal(0.0, 0.010, (n_days, len(tickers))),
                                  axis=0)),
        index=days, columns=tickers)
    rows = [("000001", "앵커1", ANCHOR, np.nan, np.nan, 300e12, True),
            ("000002", "앵커2", ANCHOR, np.nan, np.nan, 250e12, True)]
    for i in range(1, 5):
        rows.append((f"10000{i}", f"핵심{i}", CORE, 0.55, np.nan,
                     (30 - 5 * i) * 1e12, True))
    for i in range(1, 3):
        rows.append((f"20000{i}", f"위성{i}", SAT, 0.20, 0.85, 6e12, True))
    snap = pd.DataFrame(rows, columns=COLS)
    snaps = {px.index[0]: snap, px.index[125]: snap}
    return px, snaps, ConfigV2.with_policy("mid")


def _events(px, snaps, cfg):
    return build_event_schedule(px, snaps, cfg=cfg)[0]


# ---------------------------------------------------------------- 집중도
def test_effective_n_of_equal_weights_equals_n():
    w = pd.Series(np.repeat(0.2, 5), index=list("abcde"))
    c = cr.concentration(w)
    assert abs(c["유효종목수"] - 5.0) < 1e-12
    assert abs(c["HHI"] - 0.2) < 1e-12


def test_effective_n_below_nominal_when_skewed():
    w = pd.Series({"a": 0.45, "b": 0.40, "c": 0.05, "d": 0.05, "e": 0.05})
    c = cr.concentration(w)
    assert c["n"] == 5
    assert c["유효종목수"] < 3.0            # 명목 5종목이지만 실질은 3 미만
    assert abs(c["5%초과합계"] - 0.85) < 1e-12


def test_bucket_totals_sum_to_one():
    px, snaps, cfg = _fixture()
    ev = _events(px, snaps, cfg)
    g = {pd.Timestamp(d): snaps[d].set_index("ticker")["group"] for d in snaps}
    daily = cr.concentration_history(px, ev, g)
    tot = daily[["앵커합계", "핵심합계", "위성합계"]].sum(axis=1)
    assert float((tot - 1.0).abs().max()) < 1e-9


def test_daily_max_weight_at_least_reset_max_weight():
    px, snaps, cfg = _fixture()
    ev = _events(px, snaps, cfg)
    g = {pd.Timestamp(d): snaps[d].set_index("ticker")["group"] for d in snaps}
    daily = cr.concentration_history(px, ev, g)
    reset_max = max(float(e["target_weights"].max()) for e in ev)
    assert float(daily["최대비중"].max()) >= reset_max - 1e-12


# ---------------------------------------------------- 복제 가능성
def test_unconstrained_replication_matches_index():
    px, snaps, cfg = _fixture()
    ev = _events(px, snaps, cfg)
    bt = simulate_index(px, ev, base=1000.0)
    trk = cr.replicate(px, ev, adv=None, lag=0, cost_bp=0.0)
    rel = (trk["tracker_level"] / bt["level"] - 1.0).abs().max()
    assert float(rel) < 1e-10, f"제약 없는 복제가 지수와 다르다: {rel:.2e}"


def test_execution_lag_does_not_reduce_tracking_error():
    px, snaps, cfg = _fixture()
    ev = _events(px, snaps, cfg)
    bt = simulate_index(px, ev, base=1000.0)
    te = []
    for lag in (0, 1, 3):
        trk = cr.replicate(px, ev, adv=None, lag=lag, cost_bp=0.0)
        m = cr.tracking_metrics(bt["level"], trk["tracker_level"])
        te.append(float(m["추종오차(연율)"]))
    assert te[1] >= te[0] - 1e-12 and te[2] >= te[1] - 1e-12


def test_tight_adv_constraint_delays_target():
    px, snaps, cfg = _fixture()
    ev = _events(px, snaps, cfg)
    loose = pd.Series(1e15, index=px.columns)
    tight = pd.Series(1e9, index=px.columns)
    n_loose = int((~cr.replicate(px, ev, adv=loose, aum_krw=3000e8,
                                 lag=1)["target_reached"]).sum())
    n_tight = int((~cr.replicate(px, ev, adv=tight, aum_krw=3000e8,
                                 lag=1)["target_reached"]).sum())
    assert n_tight > n_loose
