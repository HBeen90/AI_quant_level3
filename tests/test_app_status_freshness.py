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

# ------------------------------------------------ 산출물 경로 탐색
def test_reader_searches_multiple_output_dirs():
    """PR/TR 산출물은 `out/backtest_tr/` 에 분리 저장된다.

    OUTDIR 한 곳만 보면 **파일이 있는데도 '없습니다'** 라고 표시한다
    (2026-08-01 실제 발생 - ⑥ 화면이 배당 데이터가 없다고 안내했으나
    산출물과 등록 클레임은 모두 정상이었다).
    """
    s = _src()
    assert "_candidate_dirs" in s, "폴더 탐색 헬퍼가 없다"
    assert "backtest_tr" in s, "TR 폴더가 탐색 목록에 없다"
    # _read 가 단일 경로를 직접 조립하지 않아야 한다
    body = s[s.index("def _read("):s.index("def _read(") + 900]
    assert "_find(name)" in body
    finder = s[s.index("def _find("):s.index("def _backtest_final(")]
    assert "for d in _candidate_dirs()" in finder


def test_candidate_dirs_are_deduplicated():
    import os as _os
    ns = {"os": _os, "HERE": HERE, "OUTDIR": "out/backtest"}
    src = _src()
    exec(src[src.index("def _candidate_dirs"):src.index("def _read(")], ns)
    dirs = ns["_candidate_dirs"]()
    abs_dirs = [_os.path.abspath(d) for d in dirs]
    assert len(abs_dirs) == len(set(abs_dirs)), "중복 폴더가 있다"
    assert any("backtest_tr" in d for d in dirs)


def test_missing_outdir_does_not_expand_search():
    """없는 폴더를 지정하면 탐색이 넓어지면 안 된다.

    넓어지면 사용자가 잘못된 폴더를 줬을 때도 데이터를 찾아내고,
    '데이터 없음' 안내(합성 수치 오용 방지 장치)가 작동하지 않는다.
    실제로 이 테스트가 초기 구현의 과잉 탐색을 잡았다.
    """
    import os as _os
    ns = {"os": _os, "HERE": HERE, "OUTDIR": "__no_such_out__"}
    src = _src()
    exec(src[src.index("def _candidate_dirs"):src.index("def _read(")], ns)
    dirs = ns["_candidate_dirs"]()
    assert dirs == ["__no_such_out__"], f"탐색이 확장됐다: {dirs}"


def test_pr_tr_artifact_is_found_when_present():
    """레포에 산출물이 있으면 탐색이 실제로 찾아야 한다."""
    import os as _os
    ns = {"os": _os, "HERE": HERE, "OUTDIR": _os.path.join(HERE, "out", "backtest")}
    src = _src()
    exec(src[src.index("def _candidate_dirs"):src.index("def _read(")], ns)
    real = _os.path.join(HERE, "out", "backtest_tr", "index_level_pr_tr.csv")
    if not _os.path.exists(real):
        return                      # 산출물이 없는 환경에서는 건너뛴다
    found = [d for d in ns["_candidate_dirs"]()
             if _os.path.exists(_os.path.join(d, "index_level_pr_tr.csv"))]
    assert found, "산출물이 존재하는데 탐색이 찾지 못했다"

# ------------------------------------------------ 인덱스 이름 가정
def test_no_hardcoded_index_column_name():
    """`reset_index()` 후 컬럼명을 "index" 로 가정하지 않는가.

    산출 CSV 의 인덱스 이름은 저장 시점에 따라 다르다. PR/TR 파일은
    `날짜`, 정책 비교표는 이름이 없어 `index` 가 된다. 이름을 박아 두면
    한쪽에서 KeyError 로 화면이 죽거나(⑥, 2026-08-01 실제 발생) rename 이
    조용히 실패한다(⑤, 잠재).
    """
    s = _src()
    assert 'melt("index"' not in s, "melt 가 인덱스 이름을 가정한다"
    assert 'rename(columns={"index"' not in s, "rename 이 인덱스 이름을 가정한다"
    assert "flat.columns[0]" in s or "d.columns[0]" in s


def test_melt_works_for_any_index_name():
    """어떤 인덱스 이름이든 계열 2종으로 펼쳐져야 한다."""
    import pandas as pd
    base = pd.DataFrame({"PR": [1000.0, 1010.0], "TR": [1000.0, 1012.0]},
                        index=pd.to_datetime(["2020-06-15", "2020-06-16"]))
    for name in ("날짜", "index", None):
        d = base.copy()
        d.index.name = name
        flat = d.reset_index()
        long = flat.melt(flat.columns[0], var_name="계열", value_name="지수")
        assert len(long) == 4
        assert sorted(long["계열"].unique()) == ["PR", "TR"]
