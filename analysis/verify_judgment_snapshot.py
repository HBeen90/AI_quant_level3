# -*- coding: utf-8 -*-
"""2026-07-23 확정 판정 스냅샷을 선정·비중 엔진으로 재현한다.

검증 범위는 한 시점의 횡단면이다. 이 파일은 2026-07-23 판정값을 과거
심사일에 소급하지 않으며, 역사적 PIT 원장이나 시계열 성과를 생성하지 않는다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import selection, weighting  # noqa: E402
from analysis.build_pit_snapshots import load_ledger  # noqa: E402


DEFAULT_SNAPSHOT = ROOT / "evidence" / "judgment_snapshot_20260723.csv"
DEFAULT_META = ROOT / "evidence" / "judgment_snapshot_20260723.meta.json"
DEFAULT_HANDOFF = ROOT / "data" / "constituents" / "constituents_handoff_20260723.csv"
DEFAULT_LEDGER = ROOT / "data" / "verdict_ledger.csv"

EXPECTED_ROWS = 33
EXPECTED_AS_OF = pd.Timestamp("2026-07-23")
WEIGHT_ROUNDING_TOL = 5e-5  # 비중 소수 넷째 자리 공표의 반올림 허용치
GROUP_MAP = {"anchor": "앵커", "core": "핵심", "satellite": "위성"}
VALID_GROUPS = {"앵커", "핵심", "위성", "미편입"}
TRUE_VALUES = {"true", "1", "y", "yes", "참", "t"}
FALSE_VALUES = {"false", "0", "n", "no", "거짓", "f"}


def _ticker(series: pd.Series, label: str) -> pd.Series:
    raw = series.astype("string").str.strip()
    out = raw.str.zfill(6)
    bad = raw.isna() | raw.eq("") | ~out.str.fullmatch(r"\d{6}", na=False)
    if bad.any():
        raise ValueError(f"{label} 종목코드는 6자리 숫자여야 합니다: {out[bad].tolist()}")
    return out.astype(str)


def _bool(value, *, unknown_as_false: bool = False):
    if pd.isna(value) or str(value).strip() == "":
        if unknown_as_false:
            return False
        return pd.NA
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    key = str(value).strip().lower()
    if key in TRUE_VALUES:
        return True
    if key in FALSE_VALUES:
        return False
    raise ValueError(f"Boolean 파싱 실패: {value!r}")


def _hash(path: Path, mode: str | None = None) -> str:
    if mode == "utf8-lf":
        text = path.read_text(encoding="utf-8-sig")
        payload = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    else:
        payload = path.read_bytes()
    return hashlib.sha256(payload).hexdigest().upper()


def verify_source_hashes(meta_path: Path = DEFAULT_META) -> dict:
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    for rel, spec in meta["files"].items():
        path = ROOT / rel
        if not path.is_file():
            raise FileNotFoundError(f"확정 근거 파일 누락: {rel}")
        actual = _hash(path, spec.get("hash_mode"))
        if actual != spec["sha256"]:
            raise ValueError(f"확정 근거 해시 불일치: {rel}")
    return meta


def load_snapshot(path: Path = DEFAULT_SNAPSHOT) -> pd.DataFrame:
    d = pd.read_csv(path, dtype={"ticker": str})
    required = {
        "as_of", "ticker", "name", "sector", "hbm_massproduction",
        "hbm_exposure", "mem_ratio", "process_confirmed", "committee_ok",
        "audit_opinion", "admin_issue", "pre_screen_pass", "expected_group",
        "expected_weight", "judgment_status", "reviewer", "value_source",
        "decision_source",
    }
    missing = sorted(required - set(d.columns))
    if missing:
        raise ValueError(f"확정 스냅샷 필수 컬럼 누락: {missing}")

    d["ticker"] = _ticker(d["ticker"], "snapshot")
    if d["ticker"].duplicated().any():
        raise ValueError("확정 스냅샷 ticker 중복")
    d["as_of"] = pd.to_datetime(d["as_of"], errors="coerce")
    if d["as_of"].isna().any() or set(d["as_of"]) != {EXPECTED_AS_OF}:
        raise ValueError("확정 스냅샷 기준일은 2026-07-23 단일값이어야 합니다")
    if len(d) != EXPECTED_ROWS:
        raise ValueError(f"확정 후보 수 {len(d)} != {EXPECTED_ROWS}")

    for col in ("hbm_exposure", "mem_ratio", "expected_weight"):
        d[col] = pd.to_numeric(d[col], errors="coerce")
    for col in ("hbm_exposure", "mem_ratio"):
        if d[col].isna().any() or ~d[col].between(0.0, 1.0).all():
            raise ValueError(f"{col}은 전 종목 0~1 확정값이어야 합니다")
    if d["expected_weight"].dropna().pipe(
            lambda x: ~x.between(0.0, 1.0)).any():
        raise ValueError("expected_weight는 0~1 범위여야 합니다")

    for col in ("hbm_massproduction", "admin_issue", "pre_screen_pass"):
        d[col] = d[col].map(_bool).astype("boolean")
        if d[col].isna().any():
            raise ValueError(f"{col}은 확정 Boolean이어야 합니다")
    for col in ("process_confirmed", "committee_ok"):
        d[col] = d[col].map(_bool).astype("boolean")

    if not set(d["expected_group"]).issubset(VALID_GROUPS):
        raise ValueError("알 수 없는 expected_group")
    if set(d["judgment_status"]) != {"FINAL"}:
        raise ValueError("확정 스냅샷의 judgment_status는 모두 FINAL이어야 합니다")
    if d["reviewer"].astype(str).str.strip().eq("").any():
        raise ValueError("확정 스냅샷 reviewer 공란")
    if d["admin_issue"].any() or not d["pre_screen_pass"].all():
        raise ValueError("보고서의 '33종목 사전 스크린 전원 통과'와 불일치")
    if d["audit_opinion"].ne("적정").any():
        raise ValueError("보고서의 감사의견 사전 스크린 결과와 불일치")
    return d


def verify_ledger_alignment(snapshot: pd.DataFrame,
                            ledger_path: Path = DEFAULT_LEDGER) -> float:
    """횡단면 수치가 FY2025 FINAL 원장의 정본과 같은지 확인한다."""
    ledger = load_ledger(str(ledger_path))
    annual = ledger.loc[ledger["fiscal_year"].eq(2025)].set_index("ticker")
    snap = snapshot.set_index("ticker")
    if set(annual.index) != set(snap.index):
        raise ValueError("FY2025 FINAL 원장과 횡단면 스냅샷의 종목 집합이 다릅니다")
    max_error = 0.0
    for col in ("hbm_exposure", "mem_ratio"):
        left = pd.to_numeric(snap[col], errors="coerce")
        right = pd.to_numeric(annual[col].reindex(snap.index), errors="coerce")
        mismatch = ~np.isclose(left, right, atol=1e-12, rtol=0, equal_nan=True)
        if mismatch.any():
            rows = [
                f"{ticker}:{left.loc[ticker]}!={right.loc[ticker]}"
                for ticker in snap.index[mismatch]
            ]
            raise ValueError(f"횡단면-{col} FINAL 원장 불일치: {rows}")
        max_error = max(max_error, float((left - right).abs().max()))
    return max_error


def classify_snapshot(snapshot: pd.DataFrame) -> pd.DataFrame:
    engine_input = pd.DataFrame({
        "종목명": snapshot["name"],
        "코드": snapshot["ticker"],
        "유형": snapshot["sector"],
        "HBM양산": snapshot["hbm_massproduction"].astype(bool),
        "HBM노출도": snapshot["hbm_exposure"],
        "메모리향비중": snapshot["mem_ratio"],
        # 비적용/미확인은 규칙 C 평가에서 보수적으로 False다.
        "HBM공정확인": snapshot["process_confirmed"].fillna(False).astype(bool),
        "위원회확인": snapshot["committee_ok"].fillna(False).astype(bool),
    })
    classified = selection.classify(engine_input)
    out = snapshot.copy()
    out["actual_group"] = classified["군"].to_numpy()
    mismatch = out[out["actual_group"] != out["expected_group"]]
    if len(mismatch):
        cols = ["ticker", "name", "expected_group", "actual_group"]
        raise AssertionError("판정 불일치:\n" + mismatch[cols].to_string(index=False))
    return out


def verify_weights(classified: pd.DataFrame,
                   path: Path = DEFAULT_HANDOFF) -> tuple[pd.DataFrame, float]:
    handoff = pd.read_csv(path, dtype={"코드": str})
    required = {"코드", "종목명", "bucket", "weight", "ff_market_cap"}
    missing = sorted(required - set(handoff.columns))
    if missing:
        raise ValueError(f"인계 CSV 필수 컬럼 누락: {missing}")
    handoff["ticker"] = _ticker(handoff["코드"], "handoff")
    handoff["군"] = handoff["bucket"].map(GROUP_MAP)
    if handoff["군"].isna().any():
        raise ValueError("인계 CSV bucket 값 오류")
    for col in ("weight", "ff_market_cap"):
        handoff[col] = pd.to_numeric(handoff[col], errors="coerce")
    if handoff[["weight", "ff_market_cap"]].isna().any().any():
        raise ValueError("인계 CSV 비중/유동시총 파싱 실패")
    if (handoff["ff_market_cap"] <= 0).any():
        raise ValueError("인계 CSV 유동시총은 양수여야 합니다")

    picked = classified[classified["actual_group"] != "미편입"].copy()
    expected = picked.set_index("ticker")
    if set(handoff["ticker"]) != set(expected.index):
        raise AssertionError("판정 7종목과 인계 CSV 종목 집합이 다릅니다")
    group_check = handoff.set_index("ticker")["군"].reindex(expected.index)
    if not group_check.equals(expected["actual_group"]):
        raise AssertionError("판정 군과 인계 CSV bucket이 다릅니다")

    reported = handoff.set_index("ticker")["weight"].reindex(expected.index)
    if not np.allclose(reported, expected["expected_weight"], atol=1e-12, rtol=0):
        raise AssertionError("PDF 공표 비중과 인계 CSV 비중이 다릅니다")
    if abs(float(reported.sum()) - 1.0) > 1e-12:
        raise AssertionError("공표 비중 합계가 100%가 아닙니다")

    recomputed = weighting.allocate(
        handoff["군"].to_numpy(),
        handoff["ff_market_cap"].to_numpy(dtype=float),
    )
    handoff["recomputed_weight"] = recomputed
    handoff["error_pp"] = (handoff["recomputed_weight"] - handoff["weight"]) * 100
    max_error_pp = float(handoff["error_pp"].abs().max())
    if np.abs(recomputed - handoff["weight"]).max() > WEIGHT_ROUNDING_TOL:
        raise AssertionError(
            f"비중 재현 오차 {max_error_pp:.6f}%p가 공표 반올림 허용치를 초과")
    check = handoff.rename(columns={"weight": "편입비중", "ff_market_cap": "유동시총"})
    issues = weighting.verify(check)
    if issues:
        raise AssertionError(f"비중 불변식 위반: {issues}")
    return handoff, max_error_pp


def run_verification(snapshot_path: Path = DEFAULT_SNAPSHOT,
                     meta_path: Path = DEFAULT_META,
                     handoff_path: Path = DEFAULT_HANDOFF) -> dict:
    meta = verify_source_hashes(meta_path)
    snapshot = load_snapshot(snapshot_path)
    ledger_error = verify_ledger_alignment(snapshot)
    classified = classify_snapshot(snapshot)
    weights, max_error_pp = verify_weights(classified, handoff_path)
    selected = classified[classified["actual_group"] != "미편입"]
    counts = selected["actual_group"].value_counts().to_dict()
    weight_rows = weights[
        ["ticker", "종목명", "군", "weight", "recomputed_weight", "error_pp"]
    ].copy()

    # 경계값 세 종목은 버그가 가장 잘 숨어드는 회귀 기준이다.
    by_ticker = classified.set_index("ticker")
    assert by_ticker.loc["089030", "hbm_exposure"] == selection.CORE_TH
    assert by_ticker.loc["089030", "actual_group"] == "핵심"
    assert by_ticker.loc["112290", "actual_group"] == "위성"
    assert by_ticker.loc["357780", "mem_ratio"] == selection.SAT_MEM_TH
    assert by_ticker.loc["357780", "actual_group"] == "미편입"

    return {
        "as_of": str(EXPECTED_AS_OF.date()),
        "candidate_count": len(classified),
        "selected_count": len(selected),
        "group_counts": {
            "앵커": counts.get("앵커", 0),
            "핵심": counts.get("핵심", 0),
            "위성": counts.get("위성", 0),
        },
        "selected_tickers": selected["ticker"].tolist(),
        "weight_rows": weight_rows.to_dict("records"),
        "reported_weight_sum": float(weights["weight"].sum()),
        "max_weight_error_pp": max_error_pp,
        "ledger_max_numeric_error": ledger_error,
        "source_status": meta["status"],
    }


def markdown_report(result: dict) -> str:
    counts = result["group_counts"]
    weight_lines = [
        "| 코드 | 종목명 | 군 | 공표비중 | 재계산비중 | 오차(%p) |",
        "|---|---|---|---:|---:|---:|",
    ]
    for row in result["weight_rows"]:
        weight_lines.append(
            f"| {row['ticker']} | {row['종목명']} | {row['군']} | "
            f"{row['weight']:.4%} | {row['recomputed_weight']:.4%} | "
            f"{row['error_pp']:+.6f} |"
        )
    weight_table = "\n".join(weight_lines)
    return f"""# 2026-07-23 확정 판정 교차검증

## 결론

- 후보 {result['candidate_count']}종목을 규칙 0/A/C로 재판정한 결과
  {result['selected_count']}종목이 편입되었다.
- 군별 구성은 앵커 {counts['앵커']} · 핵심 {counts['핵심']} · 위성
  {counts['위성']}로 두 PDF의 결과와 전 종목 일치했다.
- 인계 유동시총으로 비중을 다시 계산한 최대 오차는
  **{result['max_weight_error_pp']:.6f}%p**로 공표 반올림 범위 안이다.
- 공표 비중 합계는 **{result['reported_weight_sum']:.2%}**다.

## 7종목 비중 대조

{weight_table}

## 경계 사례

- 테크윙: HBM 노출도 30%로 핵심군 신규 기준 경계에서 편입
- 와이씨켐: 메모리향 75% + 공정 확인 + 위원회 확인으로 위성군 편입
- 솔브레인: 메모리향 70% 경계는 통과하지만 공정 귀속 근거 미충족으로 미편입
- ISC: 감사의견은 초안의 `의견거절`이 아니라 확정 자료의 `적정`

## 검증 범위

이번 PASS는 **2026-07-23 한 시점의 33종목 판정과 7종목 비중 산정**에
한정된다. 종목별 원천 DART URL·개별 공개일 계보, 2019-2025 역사적 PIT
판정, 지수 시계열·CAGR·변동성·MDD·회전율·상관계수는 검증하지 않았다.
따라서 이 스냅샷을 과거 심사일에 소급 복사하지 않는다.
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    ap.add_argument("--meta", type=Path, default=DEFAULT_META)
    ap.add_argument("--handoff", type=Path, default=DEFAULT_HANDOFF)
    ap.add_argument("--report", type=Path, default=None)
    args = ap.parse_args()
    try:
        result = run_verification(args.snapshot, args.meta, args.handoff)
    except (AssertionError, FileNotFoundError, KeyError, ValueError) as exc:
        print(f"[FAIL] {exc}")
        return 1

    print("[PASS] 2026-07-23 확정 판정 스냅샷")
    print(f"  후보 {result['candidate_count']} -> 편입 {result['selected_count']} "
          f"(앵커 {result['group_counts']['앵커']} / "
          f"핵심 {result['group_counts']['핵심']} / "
          f"위성 {result['group_counts']['위성']})")
    print(f"  비중 합계 {result['reported_weight_sum']:.2%}, "
          f"최대 재현 오차 {result['max_weight_error_pp']:.6f}%p")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(markdown_report(result), encoding="utf-8")
        print(f"  보고서: {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
