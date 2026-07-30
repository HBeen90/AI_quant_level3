# -*- coding: utf-8 -*-
"""백테스트 실행 매니페스트 생성기 - 입력·산출 해시와 게이트 기록을 동결한다.

잠정(PROVISIONAL): 산출물 해시만 동결. 수치는 인용 금지 상태를 유지한다.
확정(FINAL): 게이트 파일(data/final_run_gates.json)의 5개 항목이 전부
승인자·일자로 채워졌을 때만 생성된다(fail-closed). FINAL 매니페스트가
존재해야 verify_claims.py 가 백테스트 수치의 인용 금지를 해제한다.

    python analysis/make_backtest_manifest.py --provisional --index-asof 2026-07-23
    python analysis/make_backtest_manifest.py --final --index-asof <확정일> \
        --gates data/final_run_gates.json
    python analysis/make_backtest_manifest.py --final ... --check-gates   # 사전점검만
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import date, datetime, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: FINAL 전환에 필요한 게이트 - 전부 value/by/on 비공란이어야 한다
GATE_KEYS = (
    "d1_index_asof",        # 백테스트 종료일 위원회 확정
    "d2_benchmark",         # 벤치마크 코드 확정(CONFIRMED) 또는 명시적 제외 결정
    "d3_admin_events",      # KIND 관리종목 이력 조사 승인 (수시편출 이벤트 확정)
    "judgment_322310",      # 오로스테크놀로지 정본 판정 추인
    "judgment_atsemicon",   # 에이티세미콘 생존편향 미니 판정 추인
)


def _sha(path: str) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest().upper()


def _git_head() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=HERE,
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except Exception:
        return None


def load_gates(path: str) -> dict:
    """게이트 파일 로드 + fail-closed 검증. 문제가 있으면 ValueError."""
    if not os.path.exists(path):
        raise ValueError(f"게이트 파일 없음: {path} "
                         "(data/final_run_gates_TEMPLATE.json 을 복사해 기입)")
    gates = json.loads(open(path, encoding="utf-8-sig").read())
    missing = [k for k in GATE_KEYS if k not in gates]
    if missing:
        raise ValueError(f"게이트 항목 누락: {missing}")
    empty = [k for k in GATE_KEYS
             if not all(str(gates[k].get(f, "")).strip()
                        for f in ("value", "by", "on"))]
    if empty:
        raise ValueError(f"게이트 미기입(value/by/on 공란): {empty}")
    try:
        date.fromisoformat(str(gates["d1_index_asof"]["value"]).strip())
        for key in GATE_KEYS:
            date.fromisoformat(str(gates[key]["on"]).strip())
    except ValueError as exc:
        raise ValueError(f"게이트 날짜는 YYYY-MM-DD 형식이어야 함: {exc}") from exc
    return {k: gates[k] for k in GATE_KEYS}


def build(out_dir: str, snapshots_dir: str, ledger: str, prices_cache: str,
          index_asof: str, final: bool, gates_path: str | None) -> dict:
    snaps = sorted(glob.glob(os.path.join(snapshots_dir, "snapshot_*.csv")))
    if not snaps:
        raise ValueError(f"스냅샷 없음: {snapshots_dir}")
    outputs = sorted(glob.glob(os.path.join(out_dir, "*.csv")))
    if not any(p.endswith("index_level.csv") for p in outputs):
        raise ValueError(f"산출물 없음: {out_dir}/index_level.csv")

    # 스냅샷 code_commit 단일성 (계보 강제)
    commits = set()
    for p in snaps:
        head = open(p, encoding="utf-8-sig").readline().strip().split(",")
        if "code_commit" in head:
            idx = head.index("code_commit")
            row = open(p, encoding="utf-8-sig").readlines()[1].rstrip("\n").split(",")
            if row[idx].strip():
                commits.add(row[idx].strip())
    if len(commits) > 1:
        raise ValueError(f"스냅샷 code_commit 혼재: {sorted(commits)}")

    current_head = _git_head()
    gates = None
    gate_file = None
    if final:
        gate_file = gates_path or os.path.join(HERE, "data",
                                               "final_run_gates.json")
        gates = load_gates(gate_file)
        if str(gates["d1_index_asof"]["value"]).strip() != index_asof:
            raise ValueError(
                f"--index-asof({index_asof}) 가 게이트 d1 값"
                f"({gates['d1_index_asof']['value']}) 과 다름")
        if len(commits) != 1:
            raise ValueError("FINAL 스냅샷에 단일 비공란 code_commit이 필요함")
        if not current_head:
            raise ValueError("현재 Git HEAD를 확인할 수 없음")
        snapshot_commit = next(iter(commits))
        if snapshot_commit != current_head:
            raise ValueError(
                f"스냅샷 code_commit({snapshot_commit})이 현재 HEAD"
                f"({current_head})와 불일치 - 스냅샷을 재생성할 것")

    def _ver(mod):
        try:
            return __import__(mod).__version__
        except Exception:
            return None

    inputs = {"data/verdict_ledger.csv": _sha(ledger)}
    if os.path.exists(prices_cache):
        inputs[os.path.relpath(prices_cache, HERE).replace("\\", "/")] = \
            _sha(prices_cache)
    benchmark = os.path.join(HERE, "data", "benchmark.yaml")
    if os.path.exists(benchmark):
        inputs["data/benchmark.yaml"] = _sha(benchmark)
    if gate_file:
        inputs[os.path.relpath(gate_file, HERE).replace("\\", "/")] = \
            _sha(gate_file)

    return {
        "schema_version": 1,
        "run_type": "FINAL_BACKTEST" if final else "PROVISIONAL_BACKTEST",
        "index_asof": index_asof,
        "generated_at_utc":
            datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "code_commit_snapshots": sorted(commits)[0] if commits else None,
        "code_commit_now": current_head,
        "inputs": inputs,
        "snapshots": {os.path.basename(p): _sha(p) for p in snaps},
        "outputs": {os.path.basename(p): _sha(p) for p in outputs},
        "gates": gates,
        "environment": {"python": platform.python_version(),
                        "pandas": _ver("pandas"), "pykrx": _ver("pykrx")},
        "citation_note": ("게이트 승인 완료 - verify_claims.py 가 수치 인용을 "
                          "해제한다" if final else
                          "잠정 실행 - 모든 수치 인용 금지 (FINAL 게이트 전)"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=os.path.join(HERE, "out", "backtest"))
    ap.add_argument("--snapshots", default=os.path.join(HERE, "data", "snapshots"))
    ap.add_argument("--ledger",
                    default=os.path.join(HERE, "data", "verdict_ledger.csv"))
    ap.add_argument("--prices-cache",
                    default=os.path.join(HERE, "out", "px.csv"))
    ap.add_argument("--index-asof", required=True)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--provisional", action="store_true")
    mode.add_argument("--final", action="store_true")
    ap.add_argument("--gates", default=None)
    ap.add_argument("--check-gates", action="store_true",
                    help="게이트 검증만 수행하고 종료 (실행 전 사전점검)")
    a = ap.parse_args()

    if a.check_gates:
        if not a.final:
            print("[SKIP] --check-gates 는 --final 에서만 의미 있음")
            return 0
        try:
            g = load_gates(a.gates or os.path.join(HERE, "data",
                                                   "final_run_gates.json"))
        except ValueError as e:
            print(f"[FAIL] 게이트 미충족: {e}")
            return 1
        # d1 과 --index-asof 의 불일치는 build() 에서도 잡히지만, 그때는 이미
        # 스냅샷 재생성과 본 실행이 끝난 뒤다. 사전점검이 존재하는 이유가
        # '돌리기 전에 막는 것'이므로 같은 대조를 여기서도 한다.
        asof = str(g["d1_index_asof"]["value"]).strip()
        if asof != a.index_asof:
            print(f"[FAIL] --index-asof({a.index_asof}) 가 게이트 "
                  f"d1_index_asof({asof}) 와 다릅니다")
            return 1
        print(f"[OK] 게이트 5건 전부 기입 확인 · INDEX_ASOF {asof} 일치")
        return 0

    try:
        m = build(a.out_dir, a.snapshots, a.ledger, a.prices_cache,
                  a.index_asof, a.final, a.gates)
    except ValueError as e:
        print(f"[FAIL] {e}")
        return 1
    name = ("backtest_run_manifest_FINAL.json" if a.final
            else "backtest_run_manifest_PROVISIONAL.json")
    dst = os.path.join(a.out_dir, name)
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(m, ensure_ascii=False, indent=1) + "\n")
    print(f"[OK] {m['run_type']} 매니페스트 생성: {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
