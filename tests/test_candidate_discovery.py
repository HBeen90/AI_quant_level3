# -*- coding: utf-8 -*-
"""후보발굴 strict 로더·검증 게이트 회귀 테스트 (네트워크 불요).

계약 §7 의 게이트 1~4·6 을 고정한다. 발굴 자체(`discover`)는 네트워크가
필요하므로 여기서 시험하지 않는다 ― **재현 경로만 시험한다.** 백테스트가
소비하는 것이 재현 경로이므로 검증 가치는 그쪽에 있다.

게이트가 '통과만' 하면 검증이 아니다. 각 게이트마다 **위반 사례를 만들어
실제로 걸리는지**까지 확인한다.
"""
from __future__ import annotations

import os
import sys
import tempfile

import pandas as pd
import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "analysis"))

import candidate_discovery as cd  # noqa: E402


def _good(**over) -> pd.DataFrame:
    rows = [
        {"selection_date": "2026-05-29", "ticker": "000001", "name": "가",
         "listing_date": "2025-11-03", "listed_asof": "true",
         "source_rcp_no": "20260316000000", "disclosed_at": "2026-03-16",
         "keyword_version": "kw_v1", "hbm_hits": 7, "process_hits": 3,
         "discovery_reason": "hbm_hits>=5", "review_status": "PART2_PENDING"},
        {"selection_date": "2026-05-29", "ticker": "000002", "name": "나",
         "listing_date": "2020-01-02", "listed_asof": "true",
         "source_rcp_no": "20260320000000", "disclosed_at": "2026-03-20",
         "keyword_version": "kw_v1", "hbm_hits": 2, "process_hits": 0,
         "discovery_reason": "임계 미달", "review_status": "SCREENED_OUT"},
    ]
    df = pd.DataFrame(rows)[cd.SCHEMA]
    for k, v in over.items():
        df.loc[0, k] = v
    return df


def _write(df: pd.DataFrame) -> str:
    d = tempfile.mkdtemp()
    p = os.path.join(d, "candidate_discovery_2026-05-29.csv")
    df.to_csv(p, index=False, encoding="utf-8-sig", lineterminator="\n")
    return p


# ------------------------------------------------------ 게이트 1 (스키마)
def test_valid_file_loads():
    df = cd.load_frozen(_write(_good()))
    assert len(df) == 2
    assert list(df.columns)[:len(cd.SCHEMA)] == cd.SCHEMA
    assert df["ticker"].iloc[0] == "000001"          # 앞자리 0 보존


def test_missing_column_is_rejected():
    bad = _good().drop(columns=["process_hits"])
    with pytest.raises(ValueError, match="스키마 누락"):
        cd.load_frozen(_write(bad))


def test_unknown_status_is_rejected():
    with pytest.raises(ValueError, match="review_status"):
        cd.load_frozen(_write(_good(review_status="MAYBE")))


def test_duplicate_key_is_rejected():
    df = _good()
    df = pd.concat([df, df.iloc[[0]]], ignore_index=True)
    with pytest.raises(ValueError, match="중복"):
        cd.load_frozen(_write(df))


# ------------------------------------------------------ 게이트 4 (동결)
def test_new_status_in_frozen_file_is_rejected():
    """실행 중 상태가 남은 파일은 동결본이 아니다 - fail-closed."""
    with pytest.raises(ValueError, match="NEW"):
        cd.load_frozen(_write(_good(review_status="NEW")))


# ------------------------------------------------------ 게이트 2 (PIT)
def test_disclosure_after_selection_date_is_flagged():
    df = cd.load_frozen(_write(_good(disclosed_at="2026-06-30")))
    errs = cd.validate_pit(df)
    assert any("게이트2" in e for e in errs)


def test_clean_file_has_no_pit_errors():
    assert cd.validate_pit(cd.load_frozen(_write(_good()))) == []


# ------------------------------------------------------ 게이트 3 (상장 정합)
def test_listed_asof_contradiction_is_flagged():
    """상장일이 심사일 이후인데 listed_asof=true 면 모순."""
    df = cd.load_frozen(_write(_good(listing_date="2026-12-01")))
    errs = cd.validate_pit(df)
    assert any("게이트3" in e for e in errs)


def test_listed_asof_must_be_boolean_text():
    df = cd.load_frozen(_write(_good(listed_asof="yes")))
    errs = cd.validate_pit(df)
    assert any("true/false" in e for e in errs)


# ------------------------------------------------------ 게이트 6 (커버리지)
def test_coverage_reports_missing_selection_dates():
    """미실시 시점이 표에서 사라지면 후보군 고정이 조용히 숨는다."""
    p = _write(_good())
    out = os.path.dirname(p)
    rep = cd.coverage_report(out, selection_dates=["2026-05-29", "2025-11-28"])
    assert len(rep) == 2
    missing = rep[rep["selection_date"] == "2025-11-28"].iloc[0]
    assert missing["상태"] == "후보발굴 미실시"
    assert missing["후보수"] == 0


# ------------------------------------------------------ 원장 대조
def test_compare_to_ledger_only_returns_pending_outside_universe():
    df = cd.load_frozen(_write(_good()))
    new = cd.compare_to_ledger(df)
    # 000001 은 판정원장 33종목 밖이고 PART2_PENDING 이므로 신규 후보
    assert list(new["ticker"]) == ["000001"]
    # SCREENED_OUT 인 000002 는 후보가 아니므로 제외돼야 한다
    assert "000002" not in set(new["ticker"])
