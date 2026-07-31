# -*- coding: utf-8 -*-
"""규칙 C 기여도 측정 회귀 테스트 (합성 데이터, 네트워크 불요).

고정하려는 성질

  1. 사유 표기 **신·구 양쪽**을 인식한다
     ― 2026-07-31 에 `공정/위원회 미확인` -> `규칙 C 요건②③ 미충족` 로
     정정했다. 한쪽만 보면 스냅샷 재생성 전후로 집계가 조용히 0 이 되고,
     그 빈 결과를 "미충족 없음"으로 오독하게 된다. 실제로 이 문자열이
     감사 결론을 한 번 뒤집었으므로 테스트로 못 박는다.
  2. 완화는 요건 ②③ **단독** 미충족만 대상으로 한다
     ― ADV·시총 등 다른 사유가 함께 붙은 종목은 실제 결격이므로 전환하면
     안 된다.
  3. 완화는 `eligible` 과 `group` 을 함께 바꾼다(둘 중 하나만 바뀌면
     선정 경로에서 조용히 누락된다).
  4. 원본 스냅샷을 변형하지 않는다(사본 반환).
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "analysis"))

from src.rebalance import ANCHOR, CORE, SAT  # noqa: E402

import rule_c_sensitivity as rc  # noqa: E402

COLS = ["ticker", "name", "group", "exposure", "mem_ratio", "float_mcap",
        "eligible", "탈락사유"]


def _snap(reason: str) -> pd.DataFrame:
    """위성 임계 통과 3종목 - 1건 적격, 1건 요건②③ 미충족, 1건 복합 사유."""
    rows = [
        ("000001", "앵커", ANCHOR, np.nan, 0.95, 300e12, True, ""),
        ("100001", "적격위성", SAT, 0.05, 0.80, 5e12, True, ""),
        ("100002", "요건미충족", SAT, 0.00, 0.75, 4e12, False, reason),
        ("100003", "유동성결격", SAT, 0.00, 0.72, 3e12, False,
         f"ADV60<10억;{reason}"),
        ("200001", "핵심", CORE, 0.55, 0.30, 8e12, True, ""),
    ]
    return pd.DataFrame(rows, columns=COLS)


def _snaps(reason: str) -> dict:
    d = pd.Timestamp("2024-06-17")
    return {d: _snap(reason)}


# ------------------------------------------------ 1. 표기 호환
def test_new_reason_string_is_recognized():
    s = _snaps("규칙 C 요건②③ 미충족")
    out = rc.relax_rule_c(s)[pd.Timestamp("2024-06-17")]
    assert bool(out.loc[out.ticker.eq("100002"), "eligible"].iloc[0])


def test_legacy_reason_string_is_recognized():
    """구 표기 동결 스냅샷도 그대로 인식돼야 한다."""
    s = _snaps("공정/위원회 미확인")
    out = rc.relax_rule_c(s)[pd.Timestamp("2024-06-17")]
    assert bool(out.loc[out.ticker.eq("100002"), "eligible"].iloc[0])


def test_both_strings_registered():
    assert "규칙 C 요건②③ 미충족" in rc.RULE_C_UNMET
    assert "공정/위원회 미확인" in rc.RULE_C_UNMET


# ------------------------------------------------ 2. 단독 사유만
def test_compound_reason_is_not_relaxed():
    """다른 결격 사유가 함께 붙은 종목은 전환 대상이 아니다."""
    for r in ("규칙 C 요건②③ 미충족", "공정/위원회 미확인"):
        out = rc.relax_rule_c(_snaps(r))[pd.Timestamp("2024-06-17")]
        row = out.loc[out.ticker.eq("100003")]
        assert not bool(row["eligible"].iloc[0]), f"복합 사유가 전환됨: {r}"


# ------------------------------------------------ 3. 두 컬럼 동시 변경
def test_relax_sets_group_and_eligible_together():
    out = rc.relax_rule_c(_snaps("규칙 C 요건②③ 미충족"))[pd.Timestamp("2024-06-17")]
    row = out.loc[out.ticker.eq("100002")]
    assert bool(row["eligible"].iloc[0])
    assert row["group"].iloc[0] == SAT


# ------------------------------------------------ 4. 원본 불변
def test_relax_does_not_mutate_input():
    s = _snaps("규칙 C 요건②③ 미충족")
    before = s[pd.Timestamp("2024-06-17")]["eligible"].tolist()
    rc.relax_rule_c(s)
    after = s[pd.Timestamp("2024-06-17")]["eligible"].tolist()
    assert before == after


# ------------------------------------------------ 집계 보고
def test_report_counts_requirement_layers():
    s = _snaps("규칙 C 요건②③ 미충족")
    led = os.path.join(HERE, "data", "verdict_ledger.csv")
    rep = rc.rule_c_report(s, led)
    r = rep.iloc[0]
    assert r["요건(1) 통과"] == 3           # mem_ratio>=0.70 인 3종목
    assert r["요건(2)(3) 통과"] == 1        # 적격은 1종목
    assert r["요건(2)(3) 미충족"] == 1      # 단독 사유 1종목
