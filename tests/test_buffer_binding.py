# -*- coding: utf-8 -*-
"""버퍼 발동 진단 회귀 테스트 - '정책 4안 동일'을 버그와 구분하는 장치.

정책 비교표가 네 행 모두 같은 숫자를 낼 때, 그것이 정상인지 배선 오류인지는
발동 건수로만 갈린다. 이 파일은 그 계수기가 실제로 셀 줄 아는지를 검증한다.
계수기가 항상 0을 돌려주면 진단은 '언제나 정상'이라고 답하게 되므로,
0이 나와야 하는 경우와 0이 아니어야 하는 경우를 **둘 다** 고정한다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from src.rebalance import (ANCHOR, CORE, SAT, ConfigV2, buffer_binding,
                           select_v2)

PASS = []


def ok(name):
    PASS.append(name)
    print(f"[OK] {name}")


def snap(rows):
    df = pd.DataFrame(rows, columns=["ticker", "name", "group", "exposure",
                                     "mem_ratio", "float_mcap", "eligible"])
    df["eligible"] = df["eligible"].astype(bool)
    return df


ANCHORS = [("000001", "앵커1", ANCHOR, np.nan, np.nan, 300e12, True),
           ("000002", "앵커2", ANCHOR, np.nan, np.nan, 250e12, True)]


def test_no_binding_when_above_entry():
    """값이 신규 기준 위면 발동 0 - 표본이 조용한 정상 상태."""
    rows = ANCHORS + [("100001", "핵심1", CORE, 0.45, np.nan, 30e12, True)]
    b = buffer_binding(snap(rows), {"100001"}, ConfigV2.with_policy("mid"))
    assert len(b) == 1 and not b["binding"].any(), b
    ok("신규 기준 위 - 버퍼 발동 0건")


def test_binding_inside_band():
    """신규 기준 아래 · 유지 기준 위 = 버퍼가 실제로 붙잡은 관측."""
    rows = ANCHORS + [("100001", "경계", CORE, 0.28, np.nan, 30e12, True)]
    cfg = ConfigV2.with_policy("mid")            # hold_core 0.27
    b = buffer_binding(snap(rows), {"100001"}, cfg)
    assert bool(b["binding"].iloc[0]), b
    assert "100001" in set(select_v2(snap(rows), {"100001"}, cfg)["members"]["ticker"])
    ok("버퍼 구간(0.27~0.30) 기존 종목 - 발동 1건 + 편입 유지")


def test_none_policy_never_binds():
    """버퍼 없음(hold=entry)은 정의상 발동할 수 없다 - 계수기 오탐 방지."""
    rows = ANCHORS + [("100001", "경계", CORE, 0.28, np.nan, 30e12, True)]
    cfg = ConfigV2.with_policy("none")
    b = buffer_binding(snap(rows), {"100001"}, cfg)
    assert not b["binding"].any()
    assert "100001" not in set(
        select_v2(snap(rows), {"100001"}, cfg)["members"]["ticker"])
    ok("정책 none - 발동 0건이면서 실제로 편출 (버퍼 부재 확인)")


def test_new_entrant_not_counted():
    """신규는 버퍼 대상이 아니다 - 기존 종목만 유지 임계값을 받는다."""
    rows = ANCHORS + [("100001", "신규경계", CORE, 0.28, np.nan, 30e12, True)]
    b = buffer_binding(snap(rows), set(), ConfigV2.with_policy("wide"))
    assert len(b) == 0, b
    ok("신규 종목은 버퍼 집계 대상 제외")


def test_hard_drop_beats_buffer():
    """eligible=False는 버퍼가 못 구한다 - 집계에서도 빠져야 한다."""
    rows = ANCHORS + [("100001", "탈락", CORE, 0.28, np.nan, 30e12, False)]
    b = buffer_binding(snap(rows), {"100001"}, ConfigV2.with_policy("wide"))
    assert len(b) == 0, b
    ok("하드 탈락 종목은 버퍼 집계 대상 제외")


def test_satellite_uses_mem_ratio():
    """위성은 mem_ratio로 판정 - exposure가 낮아도 발동 판정은 mem_ratio 기준."""
    rows = ANCHORS + [("100001", "핵심1", CORE, 0.60, np.nan, 30e12, True),
                      ("200001", "위성", SAT, 0.02, 0.68, 5e12, True)]
    cfg = ConfigV2.with_policy("mid")            # hold_sat 0.67
    b = buffer_binding(snap(rows), {"200001"}, cfg)
    r = b[b["ticker"] == "200001"].iloc[0]
    assert r["metric"] == "mem_ratio" and bool(r["binding"]), b
    ok("위성군 - mem_ratio 기준으로 발동 판정")


def test_select_v2_carries_diagnostic():
    """select_v2 반환에 진단이 실려야 백테스트 계층이 집계할 수 있다."""
    rows = ANCHORS + [("100001", "경계", CORE, 0.28, np.nan, 30e12, True)]
    out = select_v2(snap(rows), {"100001"}, ConfigV2.with_policy("mid"))
    assert "buffer_binding" in out and int(out["buffer_binding"]["binding"].sum()) == 1
    ok("select_v2 반환에 buffer_binding 동봉")


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
    print(f"\n{len(PASS)}/{len(PASS)} 버퍼 진단 테스트 통과")
