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
import re
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


#: 스냅샷 재현성에 실제로 영향을 주는 코드 경로.
#:
#: 왜 전체 코드가 아니라 이 셋인가 - `build_pit_snapshots.py` 는 프로젝트
#: 모듈 중 `analysis.index_calendar` 하나만 import 한다(나머지는 stdlib·
#: numpy·pandas·pykrx). 따라서 다른 파일이 바뀌어도 같은 입력에서 같은
#: 스냅샷이 나온다. 전체 코드 동일성을 요구하면 스냅샷과 무관한 분석
#: 스크립트 하나만 추가해도 재생성을 강제하게 되고, 그 재생성은 KRX
#: 접근을 요구한다.
#:
#: 이는 `verify_claims._code_changed_between` 이 '커밋 동일'에서 '코드
#: 무변경'으로 옮겨간 것과 같은 교훈이며, 그 판정이 이쪽에는 적용되지
#: 않은 채 남아 있었다.
_SNAPSHOT_CODE_PATHS = ("analysis/build_pit_snapshots.py",
                        "analysis/index_calendar.py",
                        "requirements.txt")

#: 위 경로 집합이 유효하려면 스냅샷 생성기의 프로젝트 의존이 이 목록에
#: 한정돼야 한다. 새 의존이 생기면 가정이 깨지므로 넓은 판정으로 되돌린다.
_SNAPSHOT_ALLOWED_IMPORTS = {"analysis.index_calendar"}
_PROJECT_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.MULTILINE)


def _snapshot_deps_are_declared() -> bool:
    """스냅샷 생성기의 프로젝트 의존이 선언 목록 안에 있는가.

    가정이 조용히 낡는 것을 막는다. 실제로 이 검사가 초기 구현의 오류를
    잡았다 - `# noqa` 주석이 달린 import 한 줄이 육안 검토에서 누락됐고,
    그 결과 좁힌 경로 집합이 틀렸다.
    """
    try:
        src = open(os.path.join(HERE, "analysis", "build_pit_snapshots.py"),
                   encoding="utf-8").read()
    except Exception:
        return False
    for m in _PROJECT_IMPORT_RE.finditer(src):
        mod = m.group(1) or m.group(2) or ""
        root = mod.split(".")[0]
        if root in {"src", "backtest", "analysis"} \
                and mod not in _SNAPSHOT_ALLOWED_IMPORTS:
            return False
    return True


def _snapshot_code_changed(a: str, b: str) -> bool:
    """스냅샷 생성 경로가 두 커밋 사이에 바뀌었는가. 확인 불가면 True(fail-closed)."""
    if not a or not b:
        return True
    if a == b:
        return False
    paths = (_SNAPSHOT_CODE_PATHS if _snapshot_deps_are_declared()
             else ("src", "backtest", "analysis", "requirements.txt"))
    try:
        r = subprocess.run(["git", "diff", "--name-only", a, b, "--", *paths],
                           cwd=HERE, capture_output=True, text=True, timeout=20)
    except Exception:
        return True
    if r.returncode != 0:
        return True
    return bool(r.stdout.strip())


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


def validate_price_cache_manifest(path: str, prices_cache: str,
                                  index_asof: str) -> dict:
    """FINAL 가격 캐시가 수정주가 계약과 매니페스트 해시에 맞는지 검증한다."""
    if not os.path.exists(path):
        raise ValueError(f"가격 캐시 매니페스트 없음: {path}")
    meta = json.loads(open(path, encoding="utf-8-sig").read())
    configured = str(meta.get("path", "")).strip()
    configured_path = (configured if os.path.isabs(configured) else
                       os.path.join(HERE, configured))
    if os.path.normcase(os.path.abspath(configured_path)) != \
            os.path.normcase(os.path.abspath(prices_cache)):
        raise ValueError("가격 캐시 경로가 매니페스트와 다름")
    if str(meta.get("end", "")).strip() != index_asof:
        raise ValueError("가격 캐시 종료일이 INDEX_ASOF와 다름")
    if str(meta.get("price_type", "")).strip() != "adjusted_close":
        raise ValueError("FINAL 지수 수익률에는 adjusted_close 가격 캐시가 필요함")
    if not os.path.exists(prices_cache):
        raise ValueError(f"가격 캐시 없음: {prices_cache}")
    if str(meta.get("sha256", "")).lower() != _sha(prices_cache).lower():
        raise ValueError("가격 캐시 SHA-256 불일치")
    return meta


def build(out_dir: str, snapshots_dir: str, ledger: str, prices_cache: str,
          index_asof: str, final: bool, gates_path: str | None,
          price_manifest_path: str | None = None,
          benchmark_cache: str | None = None) -> dict:
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
    price_manifest = price_manifest_path or os.path.join(
        HERE, "data", "price_cache_manifest.json")
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
        if _snapshot_code_changed(snapshot_commit, current_head):
            raise ValueError(
                f"스냅샷 code_commit({snapshot_commit}) 이후 스냅샷 생성 코드가"
                f" 바뀌었다(현재 HEAD {current_head}) - 스냅샷을 재생성할 것."
                f" 대상 경로: {', '.join(_SNAPSHOT_CODE_PATHS)}")
        validate_price_cache_manifest(price_manifest, prices_cache, index_asof)

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
    if os.path.exists(price_manifest):
        inputs[os.path.relpath(price_manifest, HERE).replace("\\", "/")] = \
            _sha(price_manifest)
    if benchmark_cache:
        if not os.path.exists(benchmark_cache):
            raise ValueError(f"벤치마크 캐시 없음: {benchmark_cache}")
        inputs[os.path.relpath(benchmark_cache, HERE).replace("\\", "/")] = \
            _sha(benchmark_cache)
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
    ap.add_argument("--price-manifest",
                    default=os.path.join(HERE, "data",
                                         "price_cache_manifest.json"))
    ap.add_argument("--benchmark-cache", default=None)
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
        try:
            meta = validate_price_cache_manifest(
                a.price_manifest, a.prices_cache, a.index_asof)
        except ValueError as e:
            print(f"[FAIL] FINAL 가격 캐시 미충족: {e}")
            return 1
        print(f"[OK] 게이트 5건 · INDEX_ASOF {asof} · 가격 캐시 "
              f"{meta['price_type']} 일치")
        return 0

    try:
        m = build(a.out_dir, a.snapshots, a.ledger, a.prices_cache,
                  a.index_asof, a.final, a.gates, a.price_manifest,
                  a.benchmark_cache)
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
