# -*- coding: utf-8 -*-
"""FINAL 매니페스트 게이트를 고정한다 - 게이트 미기입이면 FINAL 이 못 생긴다."""
from __future__ import annotations

import json
import os
import sys
import tempfile

try:
    import pytest
    _raises = pytest.raises
except ImportError:                      # tests/run_all.py 경로 - pytest 불필요
    import re as _re
    from contextlib import contextmanager

    @contextmanager
    def _raises(exc, match=None):
        try:
            yield
        except exc as e:
            if match and not _re.search(match, str(e)):
                raise AssertionError(f"예외 메시지 불일치: {e}")
        else:
            raise AssertionError(f"{exc.__name__} 미발생")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from analysis.make_backtest_manifest import (  # noqa: E402
    GATE_KEYS,
    _git_head,
    _sha,
    build,
    load_gates,
    validate_price_cache_manifest,
)


def _tree(tmp: str, commit: str | None = None) -> dict:
    """합성 산출물 트리 - 네트워크·실데이터 불필요."""
    commit = commit or _git_head()
    snaps = os.path.join(tmp, "snapshots")
    out = os.path.join(tmp, "out")
    os.makedirs(snaps)
    os.makedirs(out)
    with open(os.path.join(snaps, "snapshot_20200615.csv"), "w",
              encoding="utf-8-sig", newline="\n") as f:
        f.write("ticker,group,code_commit\n000001,anchor," + commit + "\n")
    with open(os.path.join(out, "index_level.csv"), "w",
              encoding="utf-8-sig", newline="\n") as f:
        f.write("date,level,turnover,reason\n2020-06-15,1000.0,0.0,regular\n")
    ledger = os.path.join(tmp, "ledger.csv")
    open(ledger, "w", encoding="utf-8-sig").write("ticker\n000001\n")
    px = os.path.join(tmp, "px.csv")
    open(px, "w", encoding="utf-8", newline="\n").write(
        "date,000001\n2026-07-23,1000\n")
    price_manifest = os.path.join(tmp, "price_manifest.json")
    with open(price_manifest, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"path": px, "end": "2026-07-23",
                   "price_type": "adjusted_close", "sha256": _sha(px)}, f)
    benchmark = os.path.join(tmp, "benchmark.csv")
    open(benchmark, "w", encoding="utf-8", newline="\n").write(
        "date,KRX 반도체\n2026-07-23,1000\n")
    return {"out": out, "snaps": snaps, "ledger": ledger,
            "px": px, "price_manifest": price_manifest,
            "benchmark": benchmark}


def _gates(tmp: str, complete: bool = True) -> str:
    g = {k: {"value": ("2026-07-23" if k == "d1_index_asof" else "승인 내용"),
             "by": "위원회" if complete else "",
             "on": "2026-07-31" if complete else ""}
         for k in GATE_KEYS}
    p = os.path.join(tmp, "gates.json")
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        json.dump(g, f, ensure_ascii=False)
    return p


def test_provisional_builds_without_gates():
    with tempfile.TemporaryDirectory() as tmp:
        t = _tree(tmp)
        m = build(t["out"], t["snaps"], t["ledger"], t["px"],
                  "2026-07-23", final=False, gates_path=None)
    assert m["run_type"] == "PROVISIONAL_BACKTEST"
    assert m["gates"] is None
    assert "인용 금지" in m["citation_note"]
    print("[OK] 잠정 매니페스트는 게이트 없이 생성 - 인용 금지 명기")


def test_final_refuses_incomplete_gates():
    with tempfile.TemporaryDirectory() as tmp:
        t = _tree(tmp)
        gates = _gates(tmp, complete=False)
        with _raises(ValueError, match="게이트 미기입"):
            build(t["out"], t["snaps"], t["ledger"], t["px"],
                  "2026-07-23", final=True, gates_path=gates)
        with _raises(ValueError, match="게이트 파일 없음"):
            load_gates(os.path.join(tmp, "없음.json"))
        gates = _gates(tmp, complete=True)
        broken = json.loads(open(gates, encoding="utf-8").read())
        broken["d3_admin_events"]["on"] = "2026/07/30"
        with open(gates, "w", encoding="utf-8", newline="\n") as f:
            json.dump(broken, f, ensure_ascii=False)
        with _raises(ValueError, match="YYYY-MM-DD"):
            load_gates(gates)
    print("[OK] 게이트 공란·부재 시 FINAL 생성 차단 (fail-closed)")


def test_final_builds_with_complete_gates_and_checks_asof():
    with tempfile.TemporaryDirectory() as tmp:
        t = _tree(tmp)
        gates = _gates(tmp, complete=True)
        m = build(t["out"], t["snaps"], t["ledger"], t["px"],
                  "2026-07-23", final=True, gates_path=gates,
                  price_manifest_path=t["price_manifest"],
                  benchmark_cache=t["benchmark"])
        assert m["run_type"] == "FINAL_BACKTEST"
        assert set(m["gates"]) == set(GATE_KEYS)
        assert "data/benchmark.yaml" in m["inputs"]
        assert any(name.endswith("price_manifest.json") for name in m["inputs"])
        assert any(name.endswith("benchmark.csv") for name in m["inputs"])
        assert any(name.endswith("gates.json") for name in m["inputs"])
        with _raises(ValueError, match="벤치마크 캐시 없음"):
            build(t["out"], t["snaps"], t["ledger"], t["px"],
                  "2026-07-23", final=True, gates_path=gates,
                  price_manifest_path=t["price_manifest"],
                  benchmark_cache=os.path.join(tmp, "missing.csv"))
        with _raises(ValueError, match="다름"):
            build(t["out"], t["snaps"], t["ledger"], t["px"],
                  "2026-12-31", final=True, gates_path=gates)
    print("[OK] 게이트 완비 시 FINAL 생성 · d1 값과 --index-asof 대조")


def test_final_refuses_unadjusted_or_tampered_price_cache():
    with tempfile.TemporaryDirectory() as tmp:
        t = _tree(tmp)
        meta = json.loads(open(t["price_manifest"], encoding="utf-8").read())
        meta["price_type"] = "unadjusted_close"
        with open(t["price_manifest"], "w", encoding="utf-8", newline="\n") as f:
            json.dump(meta, f)
        with _raises(ValueError, match="adjusted_close"):
            validate_price_cache_manifest(t["price_manifest"], t["px"],
                                          "2026-07-23")

        meta["price_type"] = "adjusted_close"
        meta["sha256"] = "0" * 64
        with open(t["price_manifest"], "w", encoding="utf-8", newline="\n") as f:
            json.dump(meta, f)
        with _raises(ValueError, match="SHA-256"):
            validate_price_cache_manifest(t["price_manifest"], t["px"],
                                          "2026-07-23")
    print("[OK] 원주가·변조 가격 캐시의 FINAL 전환 차단")


def test_mixed_snapshot_commits_refused():
    with tempfile.TemporaryDirectory() as tmp:
        t = _tree(tmp)
        with open(os.path.join(t["snaps"], "snapshot_20201214.csv"), "w",
                  encoding="utf-8-sig", newline="\n") as f:
            f.write("ticker,group,code_commit\n000001,anchor,zzz9999\n")
        with _raises(ValueError, match="혼재"):
            build(t["out"], t["snaps"], t["ledger"], t["px"],
                  "2026-07-23", final=False, gates_path=None)
    print("[OK] 스냅샷 code_commit 혼재 차단")


def test_final_refuses_snapshot_from_other_commit():
    with tempfile.TemporaryDirectory() as tmp:
        t = _tree(tmp, commit="stale-commit")
        gates = _gates(tmp, complete=True)
        with _raises(ValueError, match="code_commit.*현재 HEAD"):
            build(t["out"], t["snaps"], t["ledger"], t["px"],
                  "2026-07-23", final=True, gates_path=gates)
    print("[OK] 현재 HEAD와 다른 재사용 스냅샷의 FINAL 전환 차단")


if __name__ == "__main__":
    test_provisional_builds_without_gates()
    test_final_refuses_incomplete_gates()
    test_final_builds_with_complete_gates_and_checks_asof()
    test_final_refuses_unadjusted_or_tampered_price_cache()
    test_mixed_snapshot_commits_refused()
    test_final_refuses_snapshot_from_other_commit()
    print("\n6/6 매니페스트 게이트 테스트 통과")
