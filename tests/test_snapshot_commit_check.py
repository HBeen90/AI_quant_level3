# -*- coding: utf-8 -*-
"""스냅샷 코드 변경 판정 회귀 테스트 (임시 저장소, 네트워크 불요).

무엇을 고정하는가
  `make_backtest_manifest` 의 스냅샷 검사는 원래 **커밋 완전 동일**을
  요구했다. 그런데 `verify_claims._code_changed_between` 은 같은 문제를
  이미 '코드 무변경'으로 고쳤다 ― 동일한 교훈이 한쪽에만 적용돼 있었다.

  그 결과 스냅샷과 무관한 분석 스크립트 하나만 추가해도 스냅샷 재생성이
  강제됐고, 그 재생성은 KRX 접근을 요구하므로 확정 실행이 막혔다.

  느슨하게 푼 것이 아니다. 막아야 할 것은 '커밋이 다른 것'이 아니라
  **스냅샷 생성 경로의 코드가 바뀌는 것**이므로 그쪽을 직접 검사한다.

특히 중요한 것 ― 의존 선언 가드
  좁힌 경로 집합은 "스냅샷 생성기가 `analysis.index_calendar` 만
  import 한다"는 가정 위에 서 있다. 새 의존이 생기면 가정이 깨지므로
  넓은 판정으로 되돌아가야 한다. 이 가드가 실제로 초기 구현의 오류를
  잡았다(`# noqa` 주석이 달린 import 한 줄이 육안 검토에서 누락됨).
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

SRC = os.path.join(HERE, "analysis", "make_backtest_manifest.py")
DEPS = ("make_backtest_manifest.py", "build_pit_snapshots.py",
        "index_calendar.py")


def _load(here: str):
    spec = importlib.util.spec_from_file_location(
        "mbm_t", os.path.join(here, "analysis", "make_backtest_manifest.py"))
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    m.HERE = here
    return m


def _git(here: str, *args) -> str:
    r = subprocess.run(["git", *args], cwd=here, capture_output=True, text=True)
    return r.stdout.strip()


def _repo():
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "analysis"), exist_ok=True)
    for f in DEPS:
        shutil.copy(os.path.join(HERE, "analysis", f),
                    os.path.join(d, "analysis", f))
    with open(os.path.join(d, "requirements.txt"), "w") as f:
        f.write("pandas==2.3.3\n")
    _git(d, "init", "-q", ".")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "base")
    return d, _git(d, "rev-parse", "HEAD")


def _commit(d: str, path: str, text: str, msg: str) -> None:
    with open(os.path.join(d, path), "a", encoding="utf-8") as f:
        f.write(text)
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", msg)


def test_same_commit_passes():
    d, base = _repo()
    m = _load(d)
    assert not m._snapshot_code_changed(base, _git(d, "rev-parse", "HEAD"))


def test_unrelated_analysis_script_does_not_invalidate():
    """오늘 확정 실행이 막혔던 경우 ― 스냅샷과 무관한 추가는 통과해야 한다."""
    d, base = _repo()
    _commit(d, "analysis/other_tool.py", "# 무관한 분석 스크립트\n", "other")
    m = _load(d)
    assert not m._snapshot_code_changed(base, _git(d, "rev-parse", "HEAD"))


def test_calendar_change_invalidates():
    """일정 조문은 심사시점을 정하므로 스냅샷에 영향을 준다."""
    d, base = _repo()
    _commit(d, "analysis/index_calendar.py", "\n# 달력 변경\n", "cal")
    m = _load(d)
    assert m._snapshot_code_changed(base, _git(d, "rev-parse", "HEAD"))


def test_generator_change_invalidates():
    d, base = _repo()
    _commit(d, "analysis/build_pit_snapshots.py", "\n# 생성기 변경\n", "gen")
    m = _load(d)
    assert m._snapshot_code_changed(base, _git(d, "rev-parse", "HEAD"))


def test_requirements_change_invalidates():
    """라이브러리 버전이 바뀌면 수치가 달라질 수 있다."""
    d, base = _repo()
    _commit(d, "requirements.txt", "numpy==2.0.0\n", "req")
    m = _load(d)
    assert m._snapshot_code_changed(base, _git(d, "rev-parse", "HEAD"))


def test_unknown_commit_is_fail_closed():
    d, base = _repo()
    m = _load(d)
    assert m._snapshot_code_changed("deadbeef", _git(d, "rev-parse", "HEAD"))
    assert m._snapshot_code_changed("", base)


# ------------------------------------------------ 의존 선언 가드
def test_declared_dependencies_match_reality():
    """현재 스냅샷 생성기의 프로젝트 의존이 선언 목록과 일치하는가."""
    m = _load(HERE)
    assert m._snapshot_deps_are_declared(), (
        "build_pit_snapshots.py 의 프로젝트 import 가 "
        f"{m._SNAPSHOT_ALLOWED_IMPORTS} 를 벗어났다 - "
        "_SNAPSHOT_CODE_PATHS 를 갱신할 것")


def test_new_project_import_widens_the_check():
    """새 의존이 생기면 좁은 판정 가정이 깨지므로 넓은 쪽으로 되돌아간다."""
    d, base = _repo()
    _commit(d, "analysis/build_pit_snapshots.py",
            "\nfrom src.selection import classify_row  # 새 의존\n", "dep")
    m = _load(d)
    assert not m._snapshot_deps_are_declared()
    # 넓은 판정에서는 무관한 스크립트 추가도 차단된다
    _commit(d, "analysis/other_tool.py", "# 무관\n", "other")
    assert m._snapshot_code_changed(base, _git(d, "rev-parse", "HEAD"))
