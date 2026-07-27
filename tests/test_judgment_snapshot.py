# -*- coding: utf-8 -*-
"""2026-07-23 확정 판정 PDF와 엔진의 횡단면 재현을 고정한다."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from analysis.verify_judgment_snapshot import (  # noqa: E402
    EXPECTED_AS_OF,
    load_snapshot,
    run_verification,
    verify_source_hashes,
)


def test_final_source_bundle_is_intact():
    meta = verify_source_hashes()
    assert meta["status"] == "FINAL_CROSS_SECTION"
    assert len(meta["files"]) == 4
    print("[OK] PDF 2종·확정 스냅샷·인계 CSV 해시 일치")


def test_33_to_7_selection_reproduces():
    result = run_verification()
    assert result["candidate_count"] == 33
    assert result["selected_count"] == 7
    assert result["group_counts"] == {"앵커": 2, "핵심": 4, "위성": 1}
    assert set(result["selected_tickers"]) == {
        "005930", "000660", "042700", "089030",
        "003160", "348210", "112290",
    }
    print("[OK] 확정 33종목 -> 7종목(2/4/1) 판정 전량 재현")


def test_published_weights_reproduce_within_rounding():
    result = run_verification()
    assert abs(result["reported_weight_sum"] - 1.0) < 1e-12
    assert result["max_weight_error_pp"] < 0.005
    print(f"[OK] 비중 합계 100%, 최대 오차 "
          f"{result['max_weight_error_pp']:.6f}%p")


def test_snapshot_is_not_historical_pit():
    snapshot = load_snapshot()
    assert set(snapshot["as_of"]) == {EXPECTED_AS_OF}
    assert "disclosed_at" not in snapshot.columns
    assert "fiscal_year" not in snapshot.columns
    print("[OK] 2026-07-23 횡단면과 역사적 PIT 원장 경계 유지")


if __name__ == "__main__":
    test_final_source_bundle_is_intact()
    test_33_to_7_selection_reproduces()
    test_published_weights_reproduce_within_rounding()
    test_snapshot_is_not_historical_pit()
    print("\n4/4 확정 판정 스냅샷 테스트 통과")
