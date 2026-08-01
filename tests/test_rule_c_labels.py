# -*- coding: utf-8 -*-
"""규칙 C 완화 대상 선별의 표기 결합 회귀 테스트 (합성, 네트워크 불요).

무엇을 고정하는가

  2026-08-02 에 실제로 발생한 사고를 못 박는다. `rule_c_sensitivity.py` 가
  완화 대상을 **탈락사유 문자열** 로 골랐는데, 위원회 안건 1-4 로 표기가
  정정되자(`공정/위원회 미확인` -> `규칙 C 요건②③ 미충족`) 도구가 대상을
  하나도 못 찾고도 **죽지 않고 그럴듯한 숫자를 냈다.** 등록 클레임의
  CAGR 기여도가 13.50%p 에서 8.04%p 로 조용히 붕괴했다.

  세 가지를 고정한다.

  1. 신·구 표기를 **모두** 인식한다 (한쪽만 알면 같은 사고가 재발한다)
  2. 세대가 **섞이면 중단**한다 - 섞인 스냅샷의 측정값은 어느 조문 상태의
     값인지 말할 수 없다. 지수 레벨은 무해해도 이 도구는 직접 오염된다.
  3. 완화 대상이 0건이면 **0 을 결과로 보고하지 않는다** (fail-closed)
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "analysis"))

import rule_c_sensitivity as rc  # noqa: E402

OLD, NEW = "공정/위원회 미확인", "규칙 C 요건②③ 미충족"


def _snap(label: str) -> pd.DataFrame:
    """요건① 통과 3종목 중 1개 적격 · 1개 규칙C 미충족 · 1개 다른 사유."""
    return pd.DataFrame([
        ("000001", "적격위성", "satellite", 0.10, 0.85, 6e12, True, ""),
        ("000002", "미충족", "", np.nan, 0.80, 5e12, False, label),
        ("000003", "자료부족", "", np.nan, 0.75, 4e12, False, "자료불충분(ADV60)"),
        ("000004", "저메모리", "", np.nan, 0.40, 3e12, False, "메모리향 미달"),
    ], columns=["ticker", "name", "group", "exposure", "mem_ratio",
                "float_mcap", "eligible", "탈락사유"])


def _snaps(labels: list) -> dict:
    base = pd.Timestamp("2020-06-15")
    return {base + pd.DateOffset(months=6 * i): _snap(l)
            for i, l in enumerate(labels)}


# ------------------------------------------------ 1. 두 표기 모두 인식
@pytest.mark.parametrize("label", [OLD, NEW])
def test_both_label_generations_are_recognized(label):
    s = _snaps([label] * 3)
    out = rc.relax_rule_c(s)
    for d in out:
        f = out[d].set_index("ticker")
        assert bool(f.loc["000002", "eligible"]), f"{label} 를 인식하지 못했다"
        assert f.loc["000002", "group"] == "satellite"
        # 다른 사유·저메모리는 건드리지 않는다
        assert not bool(f.loc["000003", "eligible"])
        assert not bool(f.loc["000004", "eligible"])


def test_unknown_label_is_not_flipped():
    """모르는 표기를 함부로 완화하면 다른 하드 탈락이 풀린다."""
    s = _snaps(["공정 미확인(구구버전)"] * 2)
    out = rc.relax_rule_c(s)
    for d in out:
        assert not out[d].set_index("ticker").loc["000002", "eligible"]


# ------------------------------------------------ 2. 세대 혼재 = 중단
def test_mixed_label_generations_abort():
    """앞 5개 신 표기 · 뒤 8개 구 표기 - 2026-08-02 실제 상태."""
    s = _snaps([NEW] * 5 + [OLD] * 8)
    with pytest.raises(SystemExit) as e:
        rc.check_label_generation(s)
    assert "섞여" in str(e.value)


@pytest.mark.parametrize("label", [OLD, NEW])
def test_single_generation_passes(label):
    rep = rc.check_label_generation(_snaps([label] * 13))
    assert len(rep) == 13
    assert set(rep["표기 세대"]) == {label}
    assert (rep["규칙C 사유"] == 1).all()
    assert (rep["기타 사유"] == 1).all()      # 자료불충분 1건만


def test_generation_report_counts_candidates():
    rep = rc.check_label_generation(_snaps([OLD] * 2))
    # 요건①(mem_ratio>=0.70) 통과·부적격 = 미충족 1 + 자료부족 1
    assert (rep["요건①통과·부적격"] == 2).all()


# ------------------------------------------------ 3. 집계도 두 표기 인식
@pytest.mark.parametrize("label", [OLD, NEW])
def test_report_counts_unmet_for_both_labels(label, tmp_path):
    led = pd.DataFrame({"ticker": ["000001", "000002", "000003"],
                        "name": ["a", "b", "c"],
                        "fiscal_year": [2024] * 3,
                        "judgment_status": ["FINAL"] * 3})
    p = tmp_path / "led.csv"
    led.to_csv(p, index=False)
    rep = rc.rule_c_report(_snaps([label] * 2), str(p))
    assert (rep["요건(2)(3) 미충족"] == 1).all(), f"{label} 미집계"
    assert (rep["요건(1) 통과"] == 3).all()
