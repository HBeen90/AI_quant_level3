# -*- coding: utf-8 -*-
"""
발표 문장 등록부 회귀 테스트 - 클레임이 조용히 썩는 것을 막는다.

`verify_claims.py` 를 만들어 두기만 하면, 엔진이 바뀌었을 때 클레임이
틀려져도 아무도 모른다. 발표 직전에야 발견하게 된다. 그래서 테스트
스위트에 넣어 `run_all.py` 가 매번 확인하게 한다.

여기서 검증하는 것
  1. 등록된 발표 문장이 전부 지금 재현되는가
  2. 인용 금지 수치 스캐너가 실제 위반을 잡는가 (오탐·미탐 둘 다)
  3. 프로젝트 문서 자체가 스캔을 통과하는가
"""
import os
import hashlib
import json
import subprocess
import sys
import tempfile

import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.setdefault("INDEX_ASOF", "2026-07-26")   # 결정론 고정

import analysis.verify_claims as claims_module  # noqa: E402
from analysis.make_backtest_manifest import GATE_KEYS  # noqa: E402
from analysis.verify_claims import (AUDITS, CLAIMS, FORBIDDEN,  # noqa: E402
                                    _run, c_test_suite, scan_forbidden)


def test_all_claims_reproduce():
    """등록된 발표 문장이 전부 재현되는가(테스트 스위트 클레임은 재귀 방지 제외)."""
    entries = [c for c in CLAIMS if c[1] is not c_test_suite]
    rows, failed = _run(entries)
    bad = [r["문장"] for r in rows if r["결과"] != "PASS"]
    assert not failed, f"재현 실패 문장 - 발표에 쓰면 안 됨: {bad}"
    print(f"[OK] 발표 문장 {len(rows)}개 전부 재현 "
          f"(테스트 스위트 클레임은 run_all 자체가 검증)")


def test_audits_are_separated_from_claims():
    """감사 항목이 발표 문장 표에 섞이지 않았는가.

    감사 문장은 '결함이 있다'를 주장하므로 PASS = 결함 재현이다. 이걸
    발표 문장과 같은 표에 두면 (a) 발표에 결함이 인용되고 (b) 결함을 고치는
    순간 FAIL 로 바뀌어 회귀처럼 보인다.
    """
    claim_fns = {c[1] for c in CLAIMS}
    audit_fns = {a[1] for a in AUDITS}
    assert not (claim_fns & audit_fns), "감사 항목이 발표 문장에 중복 등록됨"
    assert audit_fns, "감사 항목이 비어 있음"
    rows, _ = _run(list(AUDITS))
    assert all(r["결과"] == "PASS" for r in rows), \
        "감사 대상 결함이 재현되지 않음 - 고쳐졌다면 AUDITS 에서 내릴 것"
    print(f"[OK] 감사 {len(rows)}건이 발표 문장과 분리됨")


def test_scanner_catches_real_violation():
    """스캐너 미탐 방지 - 금지 수치가 인용되면 반드시 잡는다."""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "발표.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write("우리 지수는 CAGR 21.6%, 샤프 0.89 를 기록했다.\n")
        hits = scan_forbidden([p])
        assert len(hits) >= 1, "명백한 위반을 못 잡음"
        print(f"[OK] 스캐너가 위반 인용을 검출 ({hits.iloc[0]['수치']})")


def test_scanner_avoids_false_positives():
    """스캐너 오탐 방지 - 경보가 무의미해지는 것을 막는다."""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "정상.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write("한미반도체 비중은 29.32%다.\n")          # '9.3' 부분일치 금지
            f.write("core 상한은 18%, 위성 합계는 18%다.\n")
            f.write("회전율 48.5% 는 미실측이므로 쓰지 않는다.\n")  # 부정 문맥
            f.write("<!-- scan: off -->\n지수 3,161.9pt\n<!-- scan: on -->\n")
        hits = scan_forbidden([p])
        assert len(hits) == 0, f"오탐 발생: {hits.to_dict('records')}"
        print("[OK] 오탐 없음 (숫자 경계 · 부정 문맥 · scan:off 구간)")


def test_scanner_catches_formatting_variants():
    """미탐 방지 - 자리수 변형·부호 표기·흔한 단어로 위장한 유출을 잡는다.

    과거 스캐너의 세 구멍을 못박는다.
      1) 24.6 -> '24.60'(자리수만 추가)  2) 유니코드 마이너스 25.4
      3) '출처' 같은 흔한 단어가 든 줄 전체 스킵으로 진짜 유출이 면제되던 문제
    """
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "발표.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write("회전율 24.60% 이고 CAGR 20.30% 이다.\n")          # 자리수 변형
            f.write("MDD 는 \u221225.4% 를 기록했다.\n")              # 유니코드 마이너스
            f.write("실측 회전율 24.6% (출처: 사내 백테스트)\n")        # 흔한 단어 위장
        hits = scan_forbidden([p])
        lines = set(hits["줄"].tolist())
        assert {1, 2, 3} <= lines, \
            f"자리수/부호/위장 유출 미탐 - 잡힌 줄: {sorted(lines)}"
        print("[OK] 자리수 변형·수학용 마이너스·흔한 단어 위장 유출 검출")


def test_scanner_catches_unsigned_magnitude():
    """음수 금지수치(MDD -25.4·벤치 -34.1)를 부호 없이 써도 잡는다.

    낙폭은 'MDD 25.4%'처럼 절댓값으로 쓰는 일이 흔해, 부호만 떼면 통과하던
    구멍을 막는다. 반대로 비금지 인접값(25.5)·양수 금지수치는 영향 없음."""
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "발표.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write("최대낙폭은 25.4% 였다.\n")           # -25.4 의 양수 표기
            f.write("벤치마크 낙폭 34.1%.\n")             # -34.1 의 양수 표기
            f.write("변동성은 25.5% 로 안정적.\n")        # 비금지 - 잡히면 안 됨
        hits = scan_forbidden([p])
        caught = set(hits["줄"].tolist())
        assert {1, 2} <= caught, f"부호없는 MDD 미탐: {sorted(caught)}"
        assert 3 not in caught, "비금지값(25.5) 오탐"
        print("[OK] 부호 없는 낙폭 표기(25.4/34.1) 검출·인접값 오탐 없음")


def test_project_docs_pass_scan():
    """프로젝트 문서가 스스로 규칙을 지키는가 - 규칙을 쓴 문서가 위반하면 끝이다."""
    # 목록으로 두는 이유(glob 전량 스캔으로 바꾸지 말 것): 작업 로그 성격의
    # 문서(리밸런싱_마무리_보고.md 등)는 확정 실행의 실측치를 그대로 싣는다.
    # 코드를 고치면 FINAL 매니페스트가 무효가 되어 그 수치들이 다시 잠기고,
    # 전량 스캔이면 **매니페스트를 되살릴 확정 실행 자체가 이 검사에 막힌다**
    # (이 저장소에서 두 번 겪은 순서 함정과 같은 구조). 발표에 나가는 문서만
    # 명시적으로 넣는다.
    docs = [os.path.join(HERE, "docs", f) for f in
            ("FACTSHEET.md", "00_INDEX.md", "DEVELOP_ROADMAP.md",
             "WHAT_IS_PIT_SNAPSHOT.md", "버킷규정_개정안.md")]
    docs = [d for d in docs if os.path.exists(d)]
    assert docs, "점검할 문서를 못 찾음"
    hits = scan_forbidden(docs)
    assert len(hits) == 0, \
        f"프로젝트 문서에 금지 수치 유출:\n{hits.to_string(index=False)}"
    print(f"[OK] 문서 {len(docs)}개 스캔 통과")


def test_factsheet_is_generated_not_handwritten():
    """FACTSHEET 가 실제로 생성 가능한가 - '자동 생성'이라 써 놓고 손으로
    만들어 두면, 그 문서의 유일한 존재 이유(재현된 값)가 사라진다."""
    with tempfile.TemporaryDirectory() as tmp:
        generated = os.path.join(tmp, "FACTSHEET.md")
        p = subprocess.run(
            [sys.executable, os.path.join(HERE, "analysis", "verify_claims.py"),
             "--factsheet-out", generated, "--fast"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", cwd=HERE,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        assert p.returncode == 0, f"팩트시트 생성 실패:\n{p.stderr[-800:]}"
        out = open(generated, encoding="utf-8").read()
        assert "# 발표 팩트시트" in out and "인용 금지" in out
        assert "감사 결과" in out, "감사 결과 절이 빠짐"
        for name, _ in FORBIDDEN:
            assert name.split(" (")[0] in out, f"금지 목록 누락: {name}"
    fs = os.path.join(HERE, "docs", "FACTSHEET.md")
    if os.path.exists(fs):
        cur = open(fs, encoding="utf-8").read()
        assert "verify_claims.py --factsheet" in cur, \
            "docs/FACTSHEET.md 가 생성본이 아님 - 재생성할 것"
        expected_titles = [c[0] for c in CLAIMS if c[1] is not c_test_suite] \
            + [a[0] for a in AUDITS]
        missing = [title for title in expected_titles if title not in cur]
        assert not missing, f"FACTSHEET 저장본이 등록부보다 낡음: {missing}"
        assert "[FAIL]" not in cur, "FACTSHEET에 실패한 발표 문장이 남아 있음"
    print("[OK] FACTSHEET 생성 경로 동작 · 저장본 등록부 정합")


def test_python_sources_are_cp949_compatible():
    """팀 PowerShell 기본 콘솔에서 표시 문자 때문에 직접 실행이 죽지 않는다."""
    bad = []
    for base, _, files in os.walk(HERE):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(base, name)
            try:
                open(path, encoding="utf-8").read().encode("cp949")
            except UnicodeEncodeError as exc:
                bad.append(f"{os.path.relpath(path, HERE)}:{exc.start}")
    assert not bad, f"CP949 비호환 Python 소스: {bad}"
    print("[OK] Python 소스 CP949 콘솔 호환")


def test_final_unlock_requires_current_hashes_and_commit():
    """FINAL 뒤 산출물·입력·코드가 바뀌면 성과 수치를 다시 잠근다."""
    def sha(path):
        return hashlib.sha256(open(path, "rb").read()).hexdigest().upper()

    old_dir = claims_module._BT_DIR
    old_git_head = claims_module._git_head
    with tempfile.TemporaryDirectory() as tmp:
        index_path = os.path.join(tmp, "index_level.csv")
        with open(index_path, "w", encoding="utf-8", newline="\n") as f:
            f.write("date,level,turnover\n")
            f.write("2020-01-01,1000,0\n2020-06-01,1050,0.1\n")
            f.write("2021-01-01,1100,0.2\n")
        snapshot = sorted(
            name for name in os.listdir(claims_module._SNAPSHOT_DIR)
            if name.startswith("snapshot_") and name.endswith(".csv")
        )[0]
        snapshot_path = os.path.join(claims_module._SNAPSHOT_DIR, snapshot)
        ledger = os.path.join(HERE, "data", "verdict_ledger.csv")
        gates = {
            k: {
                "value": "2026-07-23" if k == "d1_index_asof" else "승인",
                "by": "위원회",
                "on": "2026-07-30",
            }
            for k in GATE_KEYS
        }
        manifest = {
            "run_type": "FINAL_BACKTEST",
            "index_asof": "2026-07-23",
            "code_commit_snapshots": "same-commit",
            "code_commit_now": "same-commit",
            "inputs": {"data/verdict_ledger.csv": sha(ledger)},
            "snapshots": {snapshot: sha(snapshot_path)},
            "outputs": {"index_level.csv": sha(index_path)},
            "gates": gates,
        }
        with open(os.path.join(tmp, "backtest_run_manifest_FINAL.json"), "w",
                  encoding="utf-8", newline="\n") as f:
            json.dump(manifest, f, ensure_ascii=False)
        claims_module._BT_DIR = tmp
        claims_module._git_head = lambda: "same-commit"
        try:
            assert claims_module._backtest_status()[0] == "final"
            manifest["inputs"]["data/verdict_ledger.csv"] = "0" * 64
            with open(os.path.join(tmp, "backtest_run_manifest_FINAL.json"), "w",
                      encoding="utf-8", newline="\n") as f:
                json.dump(manifest, f, ensure_ascii=False)
            assert claims_module._backtest_status()[0] != "final"

            manifest["inputs"]["data/verdict_ledger.csv"] = sha(ledger)
            with open(os.path.join(tmp, "backtest_run_manifest_FINAL.json"), "w",
                      encoding="utf-8", newline="\n") as f:
                json.dump(manifest, f, ensure_ascii=False)
            claims_module._git_head = lambda: "new-commit"
            assert claims_module._backtest_status()[0] != "final"

            claims_module._git_head = lambda: "same-commit"
            with open(index_path, "a", encoding="utf-8") as f:
                f.write("2021-01-04,9999,0\n")
            assert claims_module._backtest_status()[0] != "final"
            assert any("CAGR" in row[0] for row in claims_module.forbidden_rows())
        finally:
            claims_module._BT_DIR = old_dir
            claims_module._git_head = old_git_head
    print("[OK] FINAL 이후 산출물·입력·코드 변경 시 클레임 잠금 재적용")


if __name__ == "__main__":
    test_all_claims_reproduce()
    test_audits_are_separated_from_claims()
    test_scanner_catches_real_violation()
    test_scanner_avoids_false_positives()
    test_scanner_catches_formatting_variants()
    test_scanner_catches_unsigned_magnitude()
    test_project_docs_pass_scan()
    test_factsheet_is_generated_not_handwritten()
    test_python_sources_are_cp949_compatible()
    test_final_unlock_requires_current_hashes_and_commit()
    print("\n10/10 클레임 등록부 회귀 테스트 통과")
