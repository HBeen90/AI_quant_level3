# -*- coding: utf-8 -*-
"""버킷 드리프트 계량기의 오프라인 회귀 테스트.

검증 대상은 두 가지다.

  (1) 폐쇄형 수용량 식이 weighting.allocate 와 **같은 답**을 내는가
      이 스크립트의 모든 표는 "규정 40%가 왜 85%가 되었는가"를 폐쇄형으로
      설명한다. 식이 엔진과 어긋나면 설명이 틀린 것이고, 표는 그럴듯한
      숫자로 남아 아무도 눈치채지 못한다. 그래서 두 경로를 실제로 붙인다.

  (2) 재현한 비중 경로가 엔진 지수와 어긋날 때 **멈추는가**
      비중 경로는 엔진 밖에서 다시 굴린 것이라 조용히 갈라질 수 있다.
      틀린 경로로 만든 버킷 표는 '비슷하지만 다른 지수'의 숫자다.

네트워크·실데이터는 쓰지 않는다.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from analysis.bucket_drift import (binding_constraint, daily_path,
                                   feasible_min_composition,
                                   nonanchor_capacity, predicted_anchor,
                                   verify_path_reproduces_index)
from backtest.backtest import make_event, simulate_index
from src import weighting as W
from src.rebalance import ConfigV2

# 희소 조항·핵심군 부재 로그는 이 테스트가 **일부러 만드는 상황**이다.
# 통과 표시를 밀어내지 않도록 이 프로세스에서만 낮춘다.
logging.getLogger(W.__name__).setLevel(logging.ERROR)

PASS = []


def ok(name):
    PASS.append(name)
    print(f"[OK] {name}")


def _alloc_anchor(na: int, nc: int, ns: int) -> float:
    """weighting.allocate 가 실제로 낸 앵커 버킷 합계(유동시총 동일 가정)."""
    groups = np.array(["앵커"] * na + ["핵심"] * nc + ["위성"] * ns)
    fmc = np.full(len(groups), 1e12)
    w = W.allocate(groups, fmc)
    return float(w[groups == "앵커"].sum())


def test_capacity_matches_engine():
    """폐쇄형 예측 == 엔진 실현. 실제 관측된 4가지 구성 전부에서 확인한다.

    (2,0,1)=85% (2,1,1)=67% (2,1,3)=64% (2,4,1)=40% 는 백테스트 13회가
    실제로 지난 구성이다. 여기가 맞아야 개정안의 인과 설명이 성립한다.
    """
    for na, nc, ns in [(2, 0, 1), (2, 1, 1), (2, 1, 3), (2, 4, 1),
                       (2, 3, 1), (2, 5, 0), (3, 0, 2)]:
        pred = predicted_anchor(nc, ns)
        real = _alloc_anchor(na, nc, ns)
        assert abs(pred - real) < 1e-9, (na, nc, ns, pred, real)
    ok("폐쇄형 수용량 == weighting.allocate 실현치 (7개 구성)")


def test_capacity_is_monotone_in_counts():
    """비앵커가 늘면 수용량은 줄지 않는다. 줄면 식의 부호가 뒤집힌 것이다."""
    for nc in range(0, 6):
        for ns in range(0, 6):
            assert nonanchor_capacity(nc + 1, ns) >= nonanchor_capacity(nc, ns)
            assert nonanchor_capacity(nc, ns + 1) >= nonanchor_capacity(nc, ns)
    ok("수용량 단조성(핵심·위성 종목 수 증가 시 비감소)")


def test_satellite_group_cap_is_the_ceiling_without_core():
    """핵심군이 0이면 개별 상한을 무한히 풀어도 60%에 못 닿는다.

    개정 방향이 여기서 갈린다 - '상한을 완화하자'는 처방이 8개 회차에서
    무효라는 사실을 고정한다.
    """
    assert nonanchor_capacity(0, 100) <= W.SAT_TOTAL_CAP + 1e-12
    assert nonanchor_capacity(0, 100) < 1.0 - W.ANCHOR_W
    assert "핵심군 부재" in binding_constraint(0, 1)
    ok("핵심군 부재 시 위성 합계 상한이 천장 - 상한 완화로 해결 불가")


def test_feasible_minimum_is_self_consistent():
    """실현가능 최소 구성이 실제로 실현가능하고, 그보다 작으면 불가능하다."""
    best = feasible_min_composition()
    assert nonanchor_capacity(best["핵심"], best["위성"]) >= 1.0 - W.ANCHOR_W - 1e-12
    assert best["앵커"] * W.ANCHOR_CAP >= W.ANCHOR_W - 1e-12
    for na in range(1, 9):
        for nc in range(0, 9):
            for ns in range(0, 9):
                if na + nc + ns >= best["종목수"]:
                    continue
                feasible = (na * W.ANCHOR_CAP >= W.ANCHOR_W - 1e-12
                            and nonanchor_capacity(nc, ns)
                            >= 1.0 - W.ANCHOR_W - 1e-12)
                assert not feasible, (na, nc, ns)
    ok(f"실현가능 최소 구성 {best['종목수']}종목 - 자기정합(더 작은 구성 전무)")


def test_feasible_minimum_exceeds_declared_floor():
    """현행 하한(5)이 실현가능 하한보다 낮다는 사실을 고정한다.

    이 단언이 깨지면 파라미터(상한·40/60·하한) 중 하나가 바뀐 것이다.
    그때는 개정안 문서의 인과 설명도 같이 고쳐야 한다 - 조용히 지나가면 안 된다.
    """
    best = feasible_min_composition()
    floor = ConfigV2().min_constituents
    assert best["종목수"] > floor, (
        f"실현가능 하한 {best['종목수']} <= 선언 하한 {floor} - "
        "파라미터가 바뀌었으면 docs/버킷규정_개정안.md 도 갱신할 것")
    ok(f"선언 하한 {floor} < 실현가능 하한 {best['종목수']} (규칙 간 모순 고정)")


def _toy():
    """3종목 · 6거래일 합성 패널과 단일 정기변경 이벤트."""
    idx = pd.bdate_range("2024-01-01", periods=6)
    px = pd.DataFrame({
        "000001": [100, 102, 101, 105, 104, 108],
        "000002": [50, 51, 52, 51, 53, 52],
        "000003": [20, 20, 21, 22, 21, 23]}, index=idx, dtype=float)
    w = pd.Series({"000001": 0.5, "000002": 0.3, "000003": 0.2})
    return px, [make_event(idx[0], "regular", w)]


def test_path_verification_passes_on_engine_events():
    px, events = _toy()
    lvl = simulate_index(px, events, base=1000.0, mode="pr")["level"]
    err = verify_path_reproduces_index(px, events, lvl)
    assert err < 1e-12, err
    ok("재현 비중 경로 == 엔진 지수 계열 (합성 패널)")


def test_path_verification_fails_closed_on_mismatch():
    """비중이 다른 이벤트로 만든 경로는 반드시 거부되어야 한다.

    통과시키면 '다른 지수의 버킷 표'를 사실로 발표하게 된다.
    """
    px, events = _toy()
    lvl = simulate_index(px, events, base=1000.0, mode="pr")["level"]
    wrong = [make_event(px.index[0], "regular",
                        pd.Series({"000001": 0.2, "000002": 0.3,
                                   "000003": 0.5}))]
    try:
        verify_path_reproduces_index(px, wrong, lvl)
        raise AssertionError("어긋난 경로인데 통과함")
    except SystemExit:
        pass
    ok("경로 불일치 - fail-closed (다른 지수의 표 발행 차단)")


def test_daily_path_buckets_sum_to_one():
    """군별 합계는 매일 1.0이어야 한다. 어긋나면 군 매핑이 새는 것이다."""
    px, events = _toy()
    snap = pd.DataFrame({"ticker": ["000001", "000002", "000003"],
                         "group": ["anchor", "core", "satellite"]})
    path = daily_path(px, events, {px.index[0]: snap})
    tot = path["앵커"] + path["핵심"] + path["위성"]
    assert float((tot - 1.0).abs().max()) < 1e-12, tot
    assert len(path) == len(px)
    ok("일별 버킷 합계 1.0 유지(군 매핑 누락 없음)")


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
    print(f"\n{len(PASS)}/{len(PASS)} 버킷 드리프트 테스트 통과")
