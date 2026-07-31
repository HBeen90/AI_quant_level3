# -*- coding: utf-8 -*-
"""규칙 층별 Ablation 회귀 테스트 (합성 데이터, 네트워크 불요).

고정하려는 성질

  1. 캡을 켠 경로가 엔진 기준선과 정확히 일치한다
     ― 층 비교의 기준점이 틀리면 나머지가 전부 무의미하다.
  2. 캡을 끄면 `cap` 이벤트가 사라지고 회전율이 줄어든다
     ― 토글이 실제로 동작하는지.
  3. 비앵커가 없는 구성은 **산출 불가로 보고**된다(예외로 죽지 않는다)
     ― '산출 불가'도 결과이므로 표에 남아야 한다.
  4. 군 마스킹이 원본 스냅샷을 변형하지 않는다.
  5. Sharpe 는 CAGR/연변동성이며 무위험수익률을 차감하지 않는다
     ― 절대 수준 인용을 막기 위해 정의를 고정한다.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "analysis"))

from backtest.backtest import (ann_vol, build_event_schedule, cagr,  # noqa: E402
                               simulate_index)
from src.rebalance import ANCHOR, CORE, SAT, ConfigV2  # noqa: E402

import ablation_study as ab  # noqa: E402

COLS = ["ticker", "name", "group", "exposure", "mem_ratio", "float_mcap",
        "eligible"]
TOL = 1e-12


def _fixture(n_days: int = 320, seed: int = 5):
    rng = np.random.default_rng(seed)
    tickers = ["000001", "000002"] + [f"10000{i}" for i in range(1, 4)] \
        + [f"20000{i}" for i in range(1, 3)]
    days = pd.bdate_range("2024-06-17", periods=n_days)
    px = pd.DataFrame(
        100000 * np.exp(np.cumsum(rng.normal(0.0, 0.012, (n_days, len(tickers))),
                                  axis=0)),
        index=days, columns=tickers)
    # 앵커 하나를 구간 내내 상승시켜 30% 트리거를 넘긴다. 캡이 발동하지
    # 않는 픽스처로는 캡 토글을 시험할 수 없다(무발동을 '토글 성공'으로
    # 오인하게 된다).
    px["000001"] = px["000001"] * np.exp(np.linspace(0.0, np.log(6.0), n_days))
    rows = [("000001", "앵커1", ANCHOR, np.nan, 0.95, 300e12, True),
            ("000002", "앵커2", ANCHOR, np.nan, 0.90, 250e12, True)]
    for i in range(1, 4):
        rows.append((f"10000{i}", f"핵심{i}", CORE, 0.55, np.nan,
                     (26 - 5 * i) * 1e12, True))
    for i in range(1, 3):
        rows.append((f"20000{i}", f"위성{i}", SAT, 0.10, 0.80, 6e12, True))
    snap = pd.DataFrame(rows, columns=COLS)
    return px, {px.index[0]: snap, px.index[160]: snap}


ALL = {ANCHOR, CORE, SAT}


def test_cap_on_path_matches_engine_baseline():
    px, snaps = _fixture()
    cfg = ConfigV2.with_policy("mid")
    engine = simulate_index(px, build_event_schedule(px, snaps, cfg=cfg)[0])
    ev = ab.build_events(px, ab._mask(snaps, ALL), cfg, use_cap=True)
    bt = simulate_index(px, ev)
    assert float((bt["turnover"] - engine["turnover"]).abs().max()) < TOL
    assert float((bt["level"] / engine["level"] - 1.0).abs().max()) < TOL


def test_cap_off_removes_cap_events_and_lowers_turnover():
    px, snaps = _fixture()
    cfg = ConfigV2.with_policy("mid")
    on = ab.build_events(px, ab._mask(snaps, ALL), cfg, use_cap=True)
    off = ab.build_events(px, ab._mask(snaps, ALL), cfg, use_cap=False)
    assert [e for e in on if e["reason"] == "cap"], "기준 경로에 캡이 없다"
    assert not [e for e in off if e["reason"] == "cap"]
    bt_on = simulate_index(px, on)
    bt_off = simulate_index(px, off)
    assert float(bt_off["turnover"].sum()) < float(bt_on["turnover"].sum())


def test_anchor_only_is_reported_not_raised():
    """비앵커가 없으면 예외로 죽지 않고 '산출 불가'로 보고돼야 한다."""
    px, snaps = _fixture()
    r = ab.run_layer(px, snaps, {ANCHOR}, "none", False)
    assert isinstance(r, dict) and "infeasible" in r
    assert "비앵커" in r["infeasible"]


def test_infeasible_row_survives_in_table():
    """표에서 사라지면 규칙 C 의 존재 이유가 보이지 않는다."""
    px, snaps = _fixture()
    t = ab.table(px, snaps, [("앵커만", {ANCHOR}, "none", False),
                             ("전부", ALL, "mid", True)])
    assert len(t) == 2
    assert t.iloc[0]["비고"].startswith("산출 불가")
    assert pd.isna(t.iloc[0]["CAGR"])
    assert not pd.isna(t.iloc[1]["CAGR"])


def test_mask_does_not_mutate_input():
    px, snaps = _fixture()
    before = snaps[px.index[0]]["eligible"].tolist()
    ab._mask(snaps, {ANCHOR})
    assert snaps[px.index[0]]["eligible"].tolist() == before


def test_sharpe_is_cagr_over_vol_without_riskfree():
    px, snaps = _fixture()
    r = ab.run_layer(px, snaps, ALL, "mid", True)
    lv = r["bt"]["level"]
    assert abs(r["Sharpe(rf=0)"] - cagr(lv) / ann_vol(lv)) < 1e-12
