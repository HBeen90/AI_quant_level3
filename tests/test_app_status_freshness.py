# -*- coding: utf-8 -*-
"""대시보드 ① 화면 상태 표시 회귀 테스트 (streamlit 불요).

무엇을 고정하는가
  이 화면은 파이프라인 상태를 **하드코딩**하고 있었다. 그래서 판정 원장이
  13회분으로 채워진 뒤에도 "1/13회분 · 진행 8%"를 계속 표시했고, 발표에서
  대시보드를 켜면 몇 세대 전 상태가 그대로 뜰 뻔했다.

  화면 수치가 실제와 어긋나는 것은 이 프로젝트가 반복해서 겪은 사고
  유형이다(`DASHBOARD_NUMBER_AUDIT.md`). 그래서 상태를 **산출물에서 세도록**
  바꿨고, 그 성질을 여기서 고정한다.

  streamlit 없이 소스를 정적으로 검사한다 ― 화면 렌더링이 아니라
  "하드코딩이 다시 들어왔는가"를 보는 것이 목적이다.
"""
from __future__ import annotations

import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

APP = os.path.join(HERE, "app.py")


def _src() -> str:
    with open(APP, encoding="utf-8") as f:
        return f.read()


def test_snapshot_count_is_computed_not_hardcoded():
    """스냅샷 회차를 glob 으로 세는가."""
    s = _src()
    assert 'glob.glob(os.path.join(HERE, pat))' in s or "_count(" in s
    assert '_count("data/snapshots/snapshot_*.csv")' in s


def test_no_stale_progress_literals():
    """과거에 박혀 있던 상태 문자열이 되살아나지 않았는가."""
    s = _src()
    for bad in ('"1/13회분"', "'1/13회분'", '"-12회"', "'-12회'"):
        assert bad not in s, f"낡은 상태 리터럴이 다시 들어왔다: {bad}"


def test_bottleneck_message_branches_on_actual_count():
    """스냅샷이 채워졌으면 병목 경고가 아니라 해소 표시가 나와야 한다."""
    s = _src()
    assert "if n_snap < 13:" in s
    assert "st.success(" in s.split("st.subheader(\"병목\")")[1][:800]


def test_progress_matches_repo_state():
    """현재 레포 상태와 화면 계산식이 같은 답을 내는가."""
    n = len(glob.glob(os.path.join(HERE, "data", "snapshots",
                                   "snapshot_*.csv")))
    pct = min(100, int(round(n / 13 * 100))) if n else 0
    assert pct == 100 if n >= 13 else pct < 100
    # 화면이 쓰는 식과 동일한지 소스에서 확인
    assert "min(100, int(round(n_snap / 13 * 100)))" in _src()


def test_resolved_items_are_listed():
    """해소된 항목을 미결로 남겨 두면 낡은 인상을 준다."""
    s = _src()
    assert "해소된 항목" in s
    for item in ("METADATA_VERIFIED", "생존편향", "버킷 드리프트"):
        assert item in s, f"해소 목록에 {item} 가 없다"


def test_open_items_reflect_current_gaps():
    """오늘 확인된 실제 미결이 표에 있는가."""
    s = _src()
    for item in ("유동비율 원천", "규칙 3", "벤치마크 조항", "40/60"):
        assert item in s, f"미결 항목에 {item} 가 없다"


def test_app_still_compiles():
    import py_compile
    py_compile.compile(APP, doraise=True)
