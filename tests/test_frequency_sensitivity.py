# -*- coding: utf-8 -*-
"""재가중 주기 민감도 회귀 테스트 (합성 데이터, pykrx 불필요).

고정하려는 성질

  1. 재가중 없는 반사실이 엔진 기준선을 정확히 재현한다
     ― 여기서 갈리면 주기 비교 자체가 무의미하다.
  2. 재가중을 켜면 `reweight` 이벤트가 실제로 생기고 회전율이 늘어난다
     ― 계수기가 항상 0이면 "분기 재가중은 공짜"라는 틀린 결론이 나온다.
  3. 재가중 목표는 정기변경 리셋값과 같은 배분 규칙을 쓴다
     ― 재가중이 조문과 다른 비중을 만들면 그것은 별개의 방법론 변경이다.
  4. 재가중일은 정기변경일과 겹치지 않는다(이벤트 이중 계상 방지).
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

import frequency_sensitivity as fs  # noqa: E402

COLS = ["ticker", "name", "group", "exposure", "mem_ratio", "float_mcap",
        "eligible"]
TOL = 1e-12


def _fixture(n_days: int = 420, seed: int = 11):
    rng = np.random.default_rng(seed)
    tickers = ["000001", "000002"] + [f"10000{i}" for i in range(1, 4)] \
        + [f"20000{i}" for i in range(1, 3)]
    days = pd.bdate_range("2024-06-17", periods=n_days)
    px = pd.DataFrame(
        100000 * np.exp(np.cumsum(rng.normal(0.0, 0.011, (n_days, len(tickers))),
                                  axis=0)),
        index=days, columns=tickers)
    rows = [("000001", "앵커1", ANCHOR, np.nan, np.nan, 300e12, True),
            ("000002", "앵커2", ANCHOR, np.nan, np.nan, 250e12, True)]
    for i in range(1, 4):
        rows.append((f"10000{i}", f"핵심{i}", CORE, 0.55, np.nan,
                     (28 - 5 * i) * 1e12, True))
    for i in range(1, 3):
        rows.append((f"20000{i}", f"위성{i}", SAT, 0.20, 0.85, 6e12, True))
    snap = pd.DataFrame(rows, columns=COLS)
    return px, {px.index[0]: snap, px.index[200]: snap}, ConfigV2.with_policy("mid")


def test_no_reweight_replay_matches_engine():
    px, snaps, cfg = _fixture()
    engine = simulate_index(px, build_event_schedule(px, snaps, cfg=cfg)[0])
    ev, _ = fs.replay(px, snaps, cfg, reweight=False)
    bt = simulate_index(px, ev)
    assert float((bt["turnover"] - engine["turnover"]).abs().max()) < TOL
    assert float((bt["level"] / engine["level"] - 1.0).abs().max()) < TOL


def test_reweight_creates_events_and_costs_turnover():
    px, snaps, cfg = _fixture()
    ev0, _ = fs.replay(px, snaps, cfg, reweight=False)
    ev1, log = fs.replay(px, snaps, cfg, reweight=True)
    bt0, bt1 = simulate_index(px, ev0), simulate_index(px, ev1)
    assert [e for e in ev1 if e["reason"] == "reweight"], "재가중이 한 번도 발동하지 않았다"
    assert len(log) > 0
    assert float(bt1["turnover"].sum()) > float(bt0["turnover"].sum()) + 1e-9


def test_reweight_target_matches_regular_allocation_rule():
    """같은 유동시총이면 재가중 목표 == 정기변경 목표. 조문 일관성."""
    px, snaps, cfg = _fixture()
    ev, _ = fs.replay(px, snaps, cfg, reweight=True)
    reg = [e for e in ev if e["reason"] == "regular"][0]["target_weights"]
    rw = [e for e in ev if e["reason"] == "reweight"][0]["target_weights"]
    assert set(rw.index) == set(reg.index)
    assert abs(float(rw.sum()) - 1.0) < 1e-9
    # 버킷 합계는 수용량 규칙이 정하므로 두 경로에서 동일해야 한다
    g = snaps[px.index[0]].set_index("ticker")["group"]
    a_reg = float(reg[g.reindex(reg.index).eq(ANCHOR)].sum())
    a_rw = float(rw[g.reindex(rw.index).eq(ANCHOR)].sum())
    assert abs(a_reg - a_rw) < 1e-9


def test_reweight_dates_never_collide_with_regular():
    px, snaps, cfg = _fixture()
    ev, _ = fs.replay(px, snaps, cfg, reweight=True)
    reg = {e["effective_date"] for e in ev if e["reason"] == "regular"}
    rw = {e["effective_date"] for e in ev if e["reason"] == "reweight"}
    assert not (reg & rw)
