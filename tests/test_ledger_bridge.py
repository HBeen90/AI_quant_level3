# -*- coding: utf-8 -*-
"""판정 입력 -> PIT 원장 브릿지의 신뢰 경계를 검증한다."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from analysis.build_pit_snapshots import load_ledger  # noqa: E402
from build_ledger_from_evidence import (  # noqa: E402
    EV_MAP,
    build_ledger,
    file_sha256,
    normalize_ticker,
    parse_bool,
)
from hbm_evidence import (admin_flag_for, admin_issue_cell,  # noqa: E402
                          annual_report)


def _scaffold(years=(2025,)) -> pd.DataFrame:
    return pd.DataFrame([{
        "ticker": "000001",
        "name": "테스트",
        "disclosed_at": f"{year + 1}-03-31",
        "fiscal_year": year,
        "sector": "기존유형",
        "hbm_massproduction": pd.NA,
        "hbm_exposure": pd.NA,
        "mem_ratio": pd.NA,
        "process_confirmed": pd.NA,
        "committee_ok": pd.NA,
        "free_float": 0.60,
        "source": f"DART FY{year} TODO",
        "admin_issue": False,
        "_핸도버2026bucket": "core",
    } for year in years])


def _final_evidence(year=2025) -> pd.DataFrame:
    return pd.DataFrame([{
        "코드": "000001",
        "사업연도": year,
        "유형": "반도체 장비",
        "HBM양산": False,
        "HBM노출도": 0.42,
        "메모리향비중": 0.80,
        "HBM공정확인": True,
        "위원회확인": True,
        "감사의견": "적정",
        "관리종목": False,
        "근거공개일": f"{year + 1}-03-20",
        "근거출처": f"https://dart.fss.or.kr/fy/{year}",
        "판정자": "파트2 담당자",
        "판정상태": "FINAL",
    }])


def test_blank_boolean_stays_unknown():
    assert pd.isna(parse_bool(""))
    assert pd.isna(parse_bool(pd.NA))
    try:
        parse_bool("maybe")
        raise AssertionError("알 수 없는 Boolean 값이 허용됨")
    except ValueError:
        pass
    try:
        normalize_ticker(pd.Series([""]), "test")
        raise AssertionError("빈 종목코드가 000000으로 변환됨")
    except ValueError:
        pass
    print("[OK] 빈 Boolean은 False가 아니라 UNKNOWN")


def test_provisional_draft_is_labeled_and_helpers_are_removed():
    scaffold = pd.read_csv(
        os.path.join(ROOT, "data", "verdict_ledger_scaffold.csv"),
        dtype={"ticker": str},
    )
    evidence = pd.read_csv(
        os.path.join(ROOT, "evidence", "judgment_input_draft.csv"),
        dtype=str,
    )
    ledger, stats = build_ledger(
        scaffold, evidence, fiscal_year=2025, allow_provisional=True)
    assert stats == {
        "rows": 33,
        "excluded_unreviewed": 190,
        "numeric_unknown": 26,
        "status": "PROVISIONAL",
    }
    assert set(ledger["fiscal_year"]) == {2025}
    assert set(ledger["judgment_status"]) == {"DRAFT"}
    assert not any(c.startswith("_") for c in ledger.columns)
    print("[OK] 실제 초안 33행은 DRAFT로만 생성되고 헬퍼 열은 제거")


def test_strict_mode_rejects_missing_lineage():
    evidence = _final_evidence().drop(
        columns=["근거공개일", "근거출처", "판정자", "판정상태"])
    try:
        build_ledger(_scaffold(), evidence)
        raise AssertionError("계보 없는 원장이 FINAL로 생성됨")
    except ValueError as exc:
        assert "계보 컬럼 누락" in str(exc)
    print("[OK] 확정 모드는 공시일·출처·판정자·상태 누락 차단")


def test_strict_mode_builds_auditable_final_row():
    ledger, stats = build_ledger(_scaffold(), _final_evidence())
    row = ledger.iloc[0]
    assert stats["status"] == "FINAL" and stats["rows"] == 1
    assert row["judgment_status"] == "FINAL"
    assert row["reviewer"] == "파트2 담당자"
    assert row["source"].startswith("https://")
    assert row["disclosed_at"] == "2026-03-20"
    assert row["sector"] == "반도체 장비"
    print("[OK] 완전한 계보를 가진 FINAL 원장 생성")


def test_strict_mode_rejects_unknown_admin_issue():
    """관리종목 이력 미확인을 '해당 없음(False)'으로 확정하지 않는다."""
    evidence = _final_evidence()
    source_col = next(k for k, v in EV_MAP.items() if v == "admin_issue")
    evidence[source_col] = evidence[source_col].astype(object)
    evidence.loc[0, source_col] = ""
    try:
        build_ledger(_scaffold(), evidence)
        raise AssertionError("admin_issue UNKNOWN이 FINAL 원장에 진입함")
    except ValueError as exc:
        assert "admin_issue UNKNOWN" in str(exc)
    print("[OK] 관리종목 이력 UNKNOWN은 FINAL 원장 진입 차단")


def test_sector_update_is_keyed_by_fiscal_year():
    evidence = pd.concat([
        _final_evidence(2024).assign(유형="구유형"),
        _final_evidence(2025).assign(유형="신유형"),
    ], ignore_index=True)
    ledger, _ = build_ledger(_scaffold((2024, 2025)), evidence)
    sectors = ledger.set_index("fiscal_year")["sector"].to_dict()
    assert sectors == {2024: "구유형", 2025: "신유형"}
    print("[OK] 최신 유형이 과거 사업연도에 누수되지 않음")


def test_pit_loader_rejects_draft_without_explicit_flag():
    ledger, _ = build_ledger(
        _scaffold(), _final_evidence().assign(판정상태="DRAFT"),
        allow_provisional=True,
    )
    path = os.path.join(ROOT, "tests", "_tmp_ledger_bridge.csv")
    try:
        ledger.to_csv(path, index=False, encoding="utf-8-sig")
        try:
            load_ledger(path)
            raise AssertionError("DRAFT 원장이 확정 PIT 경로에 진입함")
        except SystemExit as exc:
            assert "FINAL" in str(exc)
        loaded = load_ledger(path, allow_provisional=True)
        assert len(loaded) == 1
    finally:
        if os.path.exists(path):
            os.remove(path)
    print("[OK] DRAFT 원장은 명시적 탐색 모드에서만 PIT 진입")


def test_string_boolean_scaffold_does_not_crash_merge():
    """scaffold bool 컬럼에 문자열('True' 등)이 와도 병합이 죽지 않는다.

    과거엔 evidence(boolean) 위에 scaffold(문자열)를 where() 로 덮을 때 최신
    pandas 가 'Need to pass bool-like values'로 크래시했다. parse_bool 통일로
    버전 무관하게 처리되는지 본다(evidence 가 덮으므로 결과 판정은 evidence 값).
    """
    scaffold = _scaffold()
    for col in ("hbm_massproduction", "process_confirmed", "committee_ok"):
        scaffold[col] = "True"                       # 문자열 bool 주입(오용 모사)
    ledger, stats = build_ledger(scaffold, _final_evidence())
    assert stats["status"] == "FINAL" and stats["rows"] == 1
    # evidence 의 판정값이 우선(HBM양산=False)
    assert bool(ledger.iloc[0]["hbm_massproduction"]) is False
    print("[OK] 문자열 bool scaffold 병합 크래시 방지(버전 무관)")


def test_strict_mode_rejects_unreviewed_scaffold_row():
    """확정 모드는 근거 없는 scaffold-only 행을 FINAL 원장에 넣지 않는다.

    bool 3종을 미리 채우고(2026 확정값 복사 등) 근거출처·판정자·공개일을 비운
    scaffold 행은, evidence 가 덮지 않으므로 판정상태가 FINAL 이 아니다. 크래시
    수정(문자열 bool 통일)으로 이 행이 병합을 통과하게 됐어도, 행별 FINAL 가드가
    확정 원장 진입을 차단해야 한다.
    """
    scaffold = _scaffold((2024, 2025))
    for col in ("hbm_massproduction", "process_confirmed", "committee_ok"):
        scaffold[col] = True                         # 근거 없이 판정 bool 채움
    scaffold["source"] = "copied-from-2026-handover"  # TODO 아님(TODO 검사 회피)
    scaffold["disclosed_at"] = ""                     # 실제 공개일 없음
    evidence = _final_evidence(2025)                  # FY2025 만 근거 있음
    try:
        build_ledger(scaffold, evidence)             # FY2024 는 근거 없는 행
        raise AssertionError("근거 없는 scaffold 행이 FINAL 원장에 진입함")
    except ValueError as e:
        assert "FINAL 이 아닌" in str(e) or "공란" in str(e)
    print("[OK] 확정 모드가 근거 미확인 scaffold 행 차단(우회 경로 폐쇄)")


def test_admin_lookup_failure_stays_unknown_not_false():
    """관리종목 조회 실패를 '해당없음(False)'으로 코어스하지 않는다.

    과거엔 admin_status_map 실패가 빈 사전 -> 전 종목 '해당없음' -> 템플릿 False
    로 흘러, 실제 관리종목이 '아님'으로 조용히 기록됐다. 실패는 UNKNOWN(빈칸)로
    남아 사람이 확인해야 한다.
    """
    # 조회 실패(None): UNKNOWN → 셀은 빈칸(False 아님)
    assert admin_flag_for("000660", None, None) == "조회실패-수동확인"
    assert admin_issue_cell(admin_flag_for("000660", None, None)) == ""
    # 조회 성공·지정 종목: True
    got = admin_flag_for("111111", {"111111": "관리종목"}, None)
    assert got == "관리종목" and admin_issue_cell(got) is True
    # 조회 성공·해당없음: False (진짜 확인된 '아님')
    ok = admin_flag_for("222222", {"111111": "관리종목"}, None)
    assert ok == "해당없음" and admin_issue_cell(ok) is False
    # PIT(과거연도) 모드: 현재 상태 자동 혼입 금지 → 빈칸
    assert admin_issue_cell(admin_flag_for("000660", {"000660": "관리종목"}, 2023)) == ""
    # 카드 생성 조기 실패 행: '관리종목' 키 없음 -> summary NaN. NaN 을 True 로
    # 코어스하지 않고 UNKNOWN(빈칸)으로 남긴다(수집 실패를 관리종목으로 오기록 금지).
    assert admin_issue_cell(float("nan")) == "" and admin_issue_cell("") == ""
    print("[OK] 관리종목 조회 실패·미수집(NaN) UNKNOWN 보존(False·True 코어스 차단)")


def test_evidence_fetch_is_fiscal_year_bounded():
    class FakeDart:
        def list(self, code, start, end, kind, final):
            assert (code, start, end, kind, final) == (
                "000001", "2026-01-01", "2026-12-31", "A", False)
            return pd.DataFrame([
                {"report_nm": "[정정]사업보고서 (2025.12)",
                 "rcept_no": "2", "rcept_dt": "20260401"},
                {"report_nm": "사업보고서 (2025.12)",
                 "rcept_no": "1", "rcept_dt": "20260320"},
            ])

    report = annual_report(FakeDart(), "000001", 2025)
    assert report == ("사업보고서 (2025.12)", "1", "20260320")
    print("[OK] 근거수집은 지정 사업연도 공시창만 조회")


def test_cli_output_is_cross_platform_reproducible():
    """CSV 줄바꿈과 실행환경 매니페스트를 플랫폼과 무관하게 고정한다."""
    with tempfile.TemporaryDirectory() as tmp:
        scaffold = os.path.join(tmp, "scaffold.csv")
        evidence = os.path.join(tmp, "evidence.csv")
        output = os.path.join(tmp, "ledger.csv")
        manifest = os.path.join(tmp, "manifest.json")
        _scaffold().to_csv(scaffold, index=False, encoding="utf-8-sig")
        _final_evidence().to_csv(evidence, index=False, encoding="utf-8-sig")
        p = subprocess.run([
            sys.executable, os.path.join(ROOT, "build_ledger_from_evidence.py"),
            "--scaffold", scaffold,
            "--evidence", evidence,
            "--out", output,
            "--manifest", manifest,
            "--code-commit", "abcdef1",
        ], capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert p.returncode == 0, p.stdout + p.stderr
        assert b"\r\n" not in open(output, "rb").read()
        meta = json.load(open(manifest, encoding="utf-8"))
        assert meta["output"]["line_ending"] == "LF"
        assert meta["output"]["sha256"] == file_sha256(output)
        assert meta["builder"]["code_commit"] == "abcdef1"
        assert meta["builder"]["python"] and meta["builder"]["pandas"]
    print("[OK] 원장 LF 고정 · 입력/출력 해시 · 실행환경 매니페스트 기록")


if __name__ == "__main__":
    test_blank_boolean_stays_unknown()
    test_provisional_draft_is_labeled_and_helpers_are_removed()
    test_strict_mode_rejects_missing_lineage()
    test_strict_mode_builds_auditable_final_row()
    test_strict_mode_rejects_unknown_admin_issue()
    test_sector_update_is_keyed_by_fiscal_year()
    test_pit_loader_rejects_draft_without_explicit_flag()
    test_string_boolean_scaffold_does_not_crash_merge()
    test_strict_mode_rejects_unreviewed_scaffold_row()
    test_admin_lookup_failure_stays_unknown_not_false()
    test_evidence_fetch_is_fiscal_year_bounded()
    test_cli_output_is_cross_platform_reproducible()
    print("\n12/12 판정 원장 브릿지 테스트 통과")
