# -*- coding: utf-8 -*-
"""
run_all.py - 전 테스트 한 줄 실행 (pytest 없이도 동작)
=======================================================
PR 노트의 "PowerShell 5.1은 && 미지원 - 한 줄씩 실행" 불편을 없앤다.

    python tests/run_all.py
    python tests/run_all.py --only index_calc      # 이름 부분일치 필터

각 테스트 파일을 별도 프로세스로 돌려 서로 오염되지 않게 하고, 마지막에
파일별 통과/실패 표와 종료코드를 낸다(CI에 그대로 물릴 수 있다).
pytest 가 설치돼 있으면 `pytest` 한 줄로도 같은 결과가 나온다(pytest.ini 동봉).
"""
from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

REQUIRED_FILES = [
    "src/index_calc.py",
    "src/rebalance.py",
    "src/selection.py",
    "src/weighting.py",
    "backtest/backtest.py",
    "build_ledger_from_evidence.py",
    "analysis/verify_judgment_snapshot.py",
    "data/verdict_ledger_scaffold.csv",
    "data/constituents/constituents_handoff_20260723.csv",
    "evidence/judgment_input_draft.csv",
    "evidence/judgment_snapshot_20260723.csv",
    "evidence/judgment_snapshot_20260723.meta.json",
    "evidence/source_docs/hbm_judgment_values_33_20260723.pdf",
    "evidence/source_docs/hbm_judgment_result_20260723.pdf",
    "tests/test_app_smoke.py",
    "tests/test_claims.py",
    "tests/test_develop_integration.py",
    "tests/test_index_calc_equivalence.py",
    "tests/test_index_calc_series.py",
    "tests/test_judgment_snapshot.py",
    "tests/test_ledger_bridge.py",
    "tests/test_pit_snapshots.py",
    "tests/test_run_backtest_smoke.py",
    "tests/test_schedule_v2.py",
    "tests/test_tr_equivalence.py",
    "tests/test_v2.py",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None, help="파일명 부분일치 필터")
    ap.add_argument("-v", "--verbose", action="store_true", help="전체 출력 표시")
    a = ap.parse_args()

    missing = [p for p in REQUIRED_FILES if not os.path.isfile(os.path.join(ROOT, p))]
    if missing:
        print("필수 패키지 파일 누락:")
        for p in missing:
            print(f"  - {p}")
        return 1

    files = sorted(glob.glob(os.path.join(HERE, "test_*.py")))
    if a.only:
        files = [f for f in files if a.only in os.path.basename(f)]
    if not files:
        print("실행할 테스트가 없습니다")
        return 1

    temp_root = os.path.join(ROOT, ".test_tmp")
    os.makedirs(temp_root, exist_ok=True)
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": ROOT,
        "TEMP": temp_root,
        "TMP": temp_root,
        "TMPDIR": temp_root,
    }
    rows, failed = [], 0
    for f in files:
        name = os.path.basename(f)
        t0 = time.time()
        p = subprocess.run([sys.executable, f], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           cwd=ROOT, env=env)
        dt = time.time() - t0
        ok = p.returncode == 0
        last = ([ln for ln in p.stdout.strip().splitlines() if ln.strip()][-1]
                if p.stdout.strip() else "")
        rows.append((name, ok, dt, last))
        if a.verbose or not ok:
            print(f"\n{'='*70}\n{name}\n{'='*70}")
            print(p.stdout)
            if p.stderr.strip():
                print("--- stderr ---")
                print(p.stderr)
        if not ok:
            failed += 1

    width = max(len(r[0]) for r in rows)
    print(f"\n{'='*70}")
    for name, ok, dt, last in rows:
        print(f"{name:<{width}}  {'PASS' if ok else 'FAIL'}  {dt:5.1f}s  {last}")
    print(f"{'='*70}")
    print(f"{len(rows) - failed}/{len(rows)} 파일 통과"
          + ("" if not failed else f" - {failed}개 실패"))
    try:
        os.rmdir(temp_root)
    except OSError:
        pass
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
