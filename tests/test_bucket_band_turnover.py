# -*- coding: utf-8 -*-
"""버킷 밴드 회전율 계량(analysis/bucket_band_turnover.py) 회귀 테스트.

합성 가격만 쓴다(pykrx 불필요). 고정하려는 성질은 네 가지다.

  1. 밴드 없는 반사실 경로가 엔진 기준선을 **정확히** 재현한다
     ― 같은 규칙을 두 번 구현했으므로, 여기서 갈리면 계량 자체가 무의미하다.
  2. 밴드=무한대는 기준선과 동일하다 ― 트리거가 조용히 회전율을 만들지 않는다.
  3. 좁은 밴드는 실제로 발동하고 회전율을 **늘린다**
     ― 계수기가 항상 0을 돌려주면 "비용 없음"이라는 틀린 결론이 나온다.
  4. 복원은 버킷 합계만 되돌리고 군 내부 상대비중을 보존한다.
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "analysis"))

from backtest.backtest import build_event_schedule, simulate_index  # noqa: E402
from src.rebalance import ANCHOR, CORE, SAT, ConfigV2  # noqa: E402

import bucket_band_turnover as bbt  # noqa: E402

COLS = ["ticker", "name", "group", "exposure", "mem_ratio", "float_mcap",
        "eligible"]
TOL = 1e-12


def _prices(n_days: int = 300, surge: str = "100001", mult: float = 6.0,
            seed: int = 7) -> pd.DataFrame:
    """한 핵심 종목만 구간 내내 오르는 패널 ― 앵커 버킷 비중을 끌어내린다."""
    rng = np.random.default_rng(seed)
    tickers = ["000001", "000002"] + [f"10000{i}" for i in range(1, 7)] \
        + [f"20000{i}" for i in range(1, 4)]
    days = pd.bdate_range("2024-06-17", periods=n_days)
    px = pd.DataFrame(
        100000 * np.exp(np.cumsum(rng.normal(0.0, 0.008, (n_days, len(tickers))),
                                  axis=0)),
        index=days, columns=tickers)
    px[surge] = px[surge] * np.exp(np.linspace(0.0, math.log(mult), n_days))
    return px


def _snapshot() -> pd.DataFrame:
    rows = [("000001", "앵커1", ANCHOR, np.nan, np.nan, 300e12, True),
            ("000002", "앵커2", ANCHOR, np.nan, np.nan, 250e12, True)]
    for i in range(1, 7):
        rows.append((f"10000{i}", f"핵심{i}", CORE, 0.55, np.nan,
                     (34 - 5 * i) * 1e12, True))
    for i in range(1, 4):
        rows.append((f"20000{i}", f"위성{i}", SAT, 0.20, 0.85,
                     (8 - 2 * i) * 1e12, True))
    return pd.DataFrame(rows, columns=COLS)


def _fixture():
    px = _prices()
    snaps = {px.index[0]: _snapshot(), px.index[130]: _snapshot()}
    return px, snaps, ConfigV2.with_policy("mid")


# ----------------------------------------------------------------------
def test_no_band_replay_matches_engine_baseline():
    px, snaps, cfg = _fixture()
    engine = simulate_index(px, build_event_schedule(px, snaps, cfg=cfg)[0])
    cf_events, _ = bbt.replay(px, snaps, cfg, band=None)
    cf = simulate_index(px, cf_events)
    assert float((cf["turnover"] - engine["turnover"]).abs().max()) < TOL
    assert float((cf["level"] / engine["level"] - 1.0).abs().max()) < TOL


def test_infinite_band_never_fires():
    px, snaps, cfg = _fixture()
    events, _ = bbt.replay(px, snaps, cfg, band=math.inf)
    assert not [e for e in events if e["reason"] == "band"]


def test_narrow_band_fires_and_costs_turnover():
    px, snaps, cfg = _fixture()
    base = simulate_index(px, build_event_schedule(px, snaps, cfg=cfg)[0])
    events, _ = bbt.replay(px, snaps, cfg, band=0.03)
    bt = simulate_index(px, events)
    assert [e for e in events if e["reason"] == "band"], "밴드가 한 번도 발동하지 않았다"
    assert float(bt["turnover"].sum()) > float(base["turnover"].sum()) + 1e-9


def test_wider_band_fires_no_more_than_narrow():
    px, snaps, cfg = _fixture()
    n3 = len([e for e in bbt.replay(px, snaps, cfg, band=0.03)[0]
              if e["reason"] == "band"])
    n10 = len([e for e in bbt.replay(px, snaps, cfg, band=0.10)[0]
               if e["reason"] == "band"])
    assert n10 <= n3


def test_restore_preserves_intra_bucket_proportions():
    w = pd.Series({"a1": 0.30, "a2": 0.20, "c1": 0.30, "c2": 0.20})
    g = pd.Series({"a1": ANCHOR, "a2": ANCHOR, "c1": CORE, "c2": SAT})
    out = bbt._restore_bucket(w, g, 0.40)
    assert abs(out[["a1", "a2"]].sum() - 0.40) < 1e-12
    assert abs(out.sum() - 1.0) < 1e-12
    assert abs(out["a1"] / out["a2"] - w["a1"] / w["a2"]) < 1e-12
    assert abs(out["c1"] / out["c2"] - w["c1"] / w["c2"]) < 1e-12


def test_measure_selfchecks_pass_and_table_shape():
    px, snaps, cfg = _fixture()
    res = bbt.measure(px, snaps, cfg, [0.03, 0.05, 0.10])
    assert res["selfcheck"]["turnover_max_diff"] < TOL
    assert len(res["table"]) == 4                      # 기준선 + 밴드 3종
    assert res["table"].iloc[0]["밴드 발동"] == 0
