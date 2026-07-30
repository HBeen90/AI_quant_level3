# -*- coding: utf-8 -*-
"""2026-07-23 확정 판정 PDF와 엔진의 횡단면 재현을 고정한다."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

try:
    import pytest  # noqa: E402
    _raises = pytest.raises
except ImportError:                      # tests/run_all.py 경로 - pytest 불필요
    import re as _re
    from contextlib import contextmanager

    @contextmanager
    def _raises(exc, match=None):
        try:
            yield
        except exc as e:
            if match and not _re.search(match, str(e)):
                raise AssertionError(f"예외 메시지 불일치: {e}")
        else:
            raise AssertionError(f"{exc.__name__} 미발생")

from analysis.verify_judgment_snapshot import (  # noqa: E402
    ALIGNMENT_GROUP_EXCEPTIONS,
    EXPECTED_AS_OF,
    load_snapshot,
    run_verification,
    verify_ledger_alignment,
    verify_source_hashes,
)


def test_final_source_bundle_is_intact():
    meta = verify_source_hashes()
    assert meta["status"] == "FINAL_CROSS_SECTION"
    assert len(meta["files"]) == 5
    print("[OK] PDF 2종·확정 스냅샷·FINAL 원장·인계 CSV 해시 일치")


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


def test_snapshot_numeric_values_match_final_ledger():
    snapshot = load_snapshot()
    assert verify_ledger_alignment(snapshot) == 0.0
    print("[OK] 횡단면 HBM 노출도·메모리향 비율이 FY2025 FINAL 원장과 일치")


def test_judgment_group_checked_with_documented_exception():
    """판정군 대조: 322310 예외만 허용, 미등록 충돌은 실패해야 한다."""
    assert ALIGNMENT_GROUP_EXCEPTIONS == set()
    snapshot = load_snapshot()
    # 현재 데이터(322310 예외 포함)는 통과해야 한다
    assert verify_ledger_alignment(snapshot) == 0.0
    # 미등록 종목의 판정군을 바꾸면 반드시 실패해야 한다
    broken = snapshot.copy()
    broken.loc[broken["ticker"] == "042700", "expected_group"] = "미편입"
    with _raises(ValueError, match="판정군"):
        verify_ledger_alignment(broken)
    print("[OK] 판정군 대조 - 문서화된 예외만 허용, 미등록 충돌은 차단")


if __name__ == "__main__":
    test_final_source_bundle_is_intact()
    test_33_to_7_selection_reproduces()
    test_published_weights_reproduce_within_rounding()
    test_snapshot_is_not_historical_pit()
    test_snapshot_numeric_values_match_final_ledger()
    test_judgment_group_checked_with_documented_exception()
    print("\n6/6 확정 판정 스냅샷 테스트 통과")
