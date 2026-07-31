# -*- coding: utf-8 -*-
"""월간 캡 퇴화 판정 회귀 테스트.

고정하려는 사실은 하나다 ― **캡은 종목 수가 적으면 상한이 아니라 균등비중
리밸런싱으로 동작한다.** 이 성질이 조용히 바뀌면 "월말 캡이 쏠림을 25%로
누른다"는 발표 문장이 사실과 어긋나므로 테스트로 못 박는다.
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "analysis"))

from src.rebalance import ConfigV2  # noqa: E402

import cap_feasibility as cf  # noqa: E402

CFG = ConfigV2()


def test_three_names_always_collapse_to_equal_weight():
    """3종목에서는 어떤 초기 비중이든 캡 결과가 정확히 1/3씩이다."""
    for w in ([0.34, 0.33, 0.33], [0.50, 0.30, 0.20],
              [0.40, 0.35, 0.25], [0.36, 0.32, 0.32]):
        changed, degenerate, tno, mx = cf.is_degenerate(pd.Series(w), CFG)
        assert changed, f"캡이 발동하지 않았다: {w}"
        assert degenerate, f"균등비중으로 퇴화하지 않았다: {w}"
        assert abs(mx - 1 / 3) < 1e-9, f"캡 후 최대비중이 25%가 아닌 1/3이어야: {mx}"
        assert tno > 0


def test_already_equal_three_names_does_not_fire():
    """이미 균등이면 발동하지 않는다 ― 퇴화의 도달점이 균등비중임을 보인다."""
    changed, _, tno, _ = cf.is_degenerate(pd.Series([1 / 3, 1 / 3, 1 / 3]), CFG)
    assert not changed
    assert tno < 1e-12


def test_five_names_cap_actually_caps():
    """5종목에서는 캡이 상한으로 작동해 최대비중이 25% 이하로 눌린다."""
    changed, degenerate, _, mx = cf.is_degenerate(
        pd.Series([0.45, 0.25, 0.15, 0.10, 0.05]), CFG)
    assert changed
    assert not degenerate
    assert mx <= 0.25 + 1e-9, f"캡 후 최대비중이 25%를 넘었다: {mx}"


def test_threshold_table_shape_and_monotone_verdict():
    tbl = cf.threshold_table(range(2, 7), trials=400, seed=3, cfg=CFG)
    assert list(tbl["종목수"]) == [2, 3, 4, 5, 6]
    deg = tbl.set_index("종목수")["퇴화율(발동 중)"]
    assert deg.loc[2] == 1.0 and deg.loc[3] == 1.0      # 항상 퇴화
    assert deg.loc[5] == 0.0 and deg.loc[6] == 0.0      # 퇴화 없음
    assert 0.0 < deg.loc[4] < 1.0                       # 경계


def test_min_safe_n_is_five():
    """현행 하한 5종목이 퇴화가 사라지는 최소 종목 수와 일치한다."""
    assert cf.min_safe_n(CFG, trials=600, seed=7) == 5


def test_degenerate_weights_sum_to_one():
    for n in (2, 3, 4, 5, 6):
        rng = np.random.default_rng(n)
        w = pd.Series(rng.dirichlet(np.ones(n) * 1.5))
        changed, _, _, _ = cf.is_degenerate(w, CFG)
        adj, _ = __import__("src.rebalance", fromlist=["monitor"]).monitor(
            w / w.sum(), CFG)
        assert abs(float(adj.sum()) - 1.0) < 1e-12
