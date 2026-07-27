# -*- coding: utf-8 -*-
"""판정 입력을 PIT 판정 원장으로 병합한다.

기본 모드는 확정 원장용 fail-closed다. 근거일, 근거출처, 판정자,
FINAL 상태가 없으면 중단한다. 검토 중인 초안은 --allow-provisional로만
생성할 수 있으며, 결과 파일에도 DRAFT 상태와 TODO 출처가 그대로 남는다.
"""
from __future__ import annotations

import argparse
import os

import pandas as pd


JUDGMENT = [
    "hbm_massproduction", "hbm_exposure", "mem_ratio",
    "process_confirmed", "committee_ok",
]
BOOL_COLS = [
    "hbm_massproduction", "process_confirmed", "committee_ok", "admin_issue",
]
EV_MAP = {
    "코드": "ticker",
    "종목코드": "ticker",
    "사업연도": "fiscal_year",
    "유형": "sector",
    "HBM양산": "hbm_massproduction",
    "HBM노출도": "hbm_exposure",
    "메모리향비중": "mem_ratio",
    "HBM공정확인": "process_confirmed",
    "위원회확인": "committee_ok",
    "감사의견": "audit_opinion",
    "관리종목": "admin_issue",
    "근거공개일": "disclosed_at",
    "근거출처": "source",
    "판정자": "reviewer",
    "판정상태": "judgment_status",
}
TRUE_VALUES = {"true", "1", "y", "yes", "참", "t"}
FALSE_VALUES = {"false", "0", "n", "no", "거짓", "f"}
FINAL_REQUIRED = [
    "disclosed_at", "source", "reviewer", "judgment_status", "audit_opinion",
]


def parse_bool(value):
    """빈칸은 UNKNOWN으로 보존하고 알려진 값만 Boolean으로 변환한다."""
    if pd.isna(value) or str(value).strip() == "":
        return pd.NA
    if isinstance(value, bool):
        return value
    key = str(value).strip().lower()
    if key in TRUE_VALUES:
        return True
    if key in FALSE_VALUES:
        return False
    raise ValueError(f"Boolean 파싱 실패: {value!r}")


def normalize_ticker(series: pd.Series, label: str) -> pd.Series:
    raw = series.astype("string").str.strip()
    out = raw.str.zfill(6)
    bad = raw.isna() | raw.eq("") | ~out.str.fullmatch(r"\d{6}", na=False)
    if bad.any():
        raise ValueError(f"{label} 종목코드는 6자리 숫자여야 합니다: "
                         f"{out[bad].tolist()}")
    return out.astype(str)


def prepare_scaffold(raw: pd.DataFrame) -> pd.DataFrame:
    required = {
        "ticker", "name", "disclosed_at", "fiscal_year", "sector",
        "free_float", "source", "admin_issue",
    } | set(JUDGMENT)
    missing = sorted(required - set(raw.columns))
    if missing:
        raise ValueError(f"scaffold 필수 컬럼 누락: {missing}")

    out = raw.copy()
    out["ticker"] = normalize_ticker(out["ticker"], "scaffold")
    out["fiscal_year"] = pd.to_numeric(out["fiscal_year"], errors="coerce")
    if out["fiscal_year"].isna().any():
        raise ValueError("scaffold fiscal_year 파싱 실패")
    out["fiscal_year"] = out["fiscal_year"].astype(int)
    if out.duplicated(["ticker", "fiscal_year"]).any():
        raise ValueError("scaffold (ticker, fiscal_year) 중복")

    for col in ("reviewer", "judgment_status", "audit_opinion"):
        if col not in out:
            out[col] = ""
    # Boolean 컬럼을 evidence 와 **같은 boolean dtype**로 통일한다. 이걸 안 하면
    # 뒤의 where() 병합에서 evidence(boolean) 위에 scaffold(문자열 'True' 등)를
    # 덮을 때 최신 pandas 가 'Need to pass bool-like values'로 죽는다. 공란은
    # parse_bool 이 pd.NA 로 보존하므로 미심사 행은 그대로 judged 에서 제외된다.
    for col in BOOL_COLS:
        if col in out:
            out[col] = out[col].map(parse_bool).astype("boolean")
    return out[[c for c in out.columns if not c.startswith("_")]]


def prepare_evidence(raw: pd.DataFrame, fiscal_year: int | None) -> pd.DataFrame:
    out = raw.rename(columns=EV_MAP).copy()
    if "ticker" not in out:
        raise ValueError("evidence에 코드/ticker 컬럼이 없습니다")
    out["ticker"] = normalize_ticker(out["ticker"], "evidence")

    if "fiscal_year" not in out:
        if fiscal_year is None:
            raise ValueError("evidence에 사업연도가 없습니다. --fiscal-year를 지정하세요")
        out["fiscal_year"] = fiscal_year
    out["fiscal_year"] = pd.to_numeric(out["fiscal_year"], errors="coerce")
    if out["fiscal_year"].isna().any():
        raise ValueError("evidence fiscal_year 파싱 실패")
    out["fiscal_year"] = out["fiscal_year"].astype(int)
    if out.duplicated(["ticker", "fiscal_year"]).any():
        raise ValueError("evidence (ticker, fiscal_year) 중복")

    for col in BOOL_COLS:
        if col in out:
            out[col] = out[col].map(parse_bool).astype("boolean")
    for col in ("hbm_exposure", "mem_ratio"):
        if col not in out:
            continue
        original = out[col].astype("string").str.strip()
        out[col] = pd.to_numeric(out[col], errors="coerce")
        bad = original.notna() & original.ne("") & out[col].isna()
        if bad.any():
            raise ValueError(f"{col} 숫자 파싱 실패: {original[bad].tolist()}")
        if (out[col].notna() & ~out[col].between(0.0, 1.0)).any():
            raise ValueError(f"{col}은 0~1 범위여야 합니다")
    return out


def build_ledger(scaffold: pd.DataFrame, evidence: pd.DataFrame,
                 fiscal_year: int | None = None,
                 allow_provisional: bool = False) -> tuple[pd.DataFrame, dict]:
    sc = prepare_scaffold(scaffold)
    ev = prepare_evidence(evidence, fiscal_year)
    key = ["ticker", "fiscal_year"]

    known = sc[key].merge(ev[key], on=key, how="right", indicator=True)
    unknown = known.loc[known["_merge"].ne("both"), key]
    if len(unknown):
        raise ValueError("scaffold에 없는 evidence 키: "
                         f"{unknown.astype(str).agg('/'.join, axis=1).tolist()}")

    if not allow_provisional:
        missing_cols = sorted(set(FINAL_REQUIRED) - set(ev.columns))
        if missing_cols:
            raise ValueError(f"확정 모드 evidence 계보 컬럼 누락: {missing_cols}")
        if ev["judgment_status"].astype(str).str.upper().ne("FINAL").any():
            raise ValueError("확정 모드는 모든 evidence 행의 판정상태가 FINAL이어야 합니다")
        for col in ("source", "reviewer", "audit_opinion"):
            if ev[col].isna().any() or ev[col].astype(str).str.strip().eq("").any():
                raise ValueError(f"확정 모드 {col} 공란")
        if ev["source"].astype(str).str.contains("TODO", case=False, na=True).any():
            raise ValueError("확정 모드 근거출처에 TODO가 남아 있습니다")
        dates = pd.to_datetime(ev["disclosed_at"], errors="coerce")
        if dates.isna().any():
            raise ValueError("확정 모드 근거공개일 파싱 실패")
        ev["disclosed_at"] = dates.dt.strftime("%Y-%m-%d")
    else:
        if "judgment_status" not in ev:
            ev["judgment_status"] = "DRAFT"
        ev["judgment_status"] = ev["judgment_status"].fillna("DRAFT")

    merge_fields = [
        "sector", *JUDGMENT, "audit_opinion", "admin_issue",
        "disclosed_at", "source", "reviewer", "judgment_status",
    ]
    merge_fields = [c for c in merge_fields if c in ev.columns]
    merged = sc.merge(ev[key + merge_fields], on=key, how="left",
                      suffixes=("", "_ev"), validate="one_to_one")
    for col in merge_fields:
        ev_col = f"{col}_ev"
        if ev_col in merged:
            merged[col] = merged[ev_col].where(merged[ev_col].notna(), merged[col])
            merged.drop(columns=ev_col, inplace=True)

    # Boolean 3종이 확인된 행만 심사 원장에 넣는다. 수치 공란은 0으로 만들지
    # 않고 NaN으로 남겨 핵심/위성 임계 판정에서 보수적으로 미편입되게 한다.
    required_bool = ["hbm_massproduction", "process_confirmed", "committee_ok"]
    for col in required_bool:
        merged[col] = merged[col].map(parse_bool).astype("boolean")
    judged = merged[merged[required_bool].notna().all(axis=1)].copy()

    if judged.empty:
        raise ValueError("심사 가능한 행이 0개입니다. Boolean 3종을 확인하세요")
    judged["free_float"] = pd.to_numeric(judged["free_float"], errors="coerce")
    if judged["free_float"].isna().any():
        bad = judged.loc[judged["free_float"].isna(),
                         ["ticker", "fiscal_year"]].astype(str)
        raise ValueError("심사 행 free_float 결측: "
                         f"{bad.agg('/'.join, axis=1).tolist()}")
    if not allow_provisional:
        if judged["source"].astype(str).str.contains("TODO", case=False, na=True).any():
            raise ValueError("확정 원장 source에 TODO가 남아 있습니다")
        # 확정 모드 행별 가드 - 출력 행 전부가 evidence 로 심사된 FINAL 이어야 한다.
        # evidence 계보 검사(위)는 evidence 행만 본다. bool 만 채워진 scaffold-only
        # 행이 근거·공개일·판정자 없이 judged 에 섞여 FINAL 원장에 들어가는 경로를
        # 여기서 닫는다(2026 확정값을 과거 골격에 복사하는 오용 등).
        st = judged["judgment_status"].astype(str).str.upper()
        stray = judged.loc[st.ne("FINAL"), ["ticker", "fiscal_year"]]
        if len(stray):
            raise ValueError(
                "확정 원장에 FINAL 이 아닌 행(근거 미확인 scaffold 행 등): "
                f"{stray.astype(str).agg('/'.join, axis=1).tolist()}")
        for col in ("source", "reviewer", "disclosed_at"):
            blank = judged[col].isna() | judged[col].astype(str).str.strip().eq("")
            if blank.any():
                bad = judged.loc[blank, ["ticker", "fiscal_year"]]
                raise ValueError(f"확정 원장 {col} 공란 행: "
                                 f"{bad.astype(str).agg('/'.join, axis=1).tolist()}")

    judged.sort_values(key, inplace=True)
    stats = {
        "rows": len(judged),
        "excluded_unreviewed": len(merged) - len(judged),
        "numeric_unknown": int(
            (judged["hbm_exposure"].isna() & judged["mem_ratio"].isna()).sum()
        ),
        "status": "PROVISIONAL" if allow_provisional else "FINAL",
    }
    return judged, stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scaffold", required=True)
    ap.add_argument("--evidence", required=True)
    ap.add_argument("--out", default="data/verdict_ledger.csv")
    ap.add_argument("--fiscal-year", type=int, default=None)
    ap.add_argument("--allow-provisional", action="store_true",
                    help="DRAFT/TODO 계보를 허용한다. 확정 백테스트 입력으로 사용 금지")
    args = ap.parse_args()

    try:
        scaffold = pd.read_csv(args.scaffold, dtype={"ticker": str})
        evidence = pd.read_csv(args.evidence, dtype=str)
        ledger, stats = build_ledger(
            scaffold, evidence, args.fiscal_year, args.allow_provisional)
    except (ValueError, KeyError) as exc:
        print(f"[FAIL] {exc}")
        return 1

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    ledger.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"[OK] {args.out} 생성 - {stats['status']} {stats['rows']}행, "
          f"미심사 제외 {stats['excluded_unreviewed']}행, "
          f"수치 UNKNOWN {stats['numeric_unknown']}행")
    if args.allow_provisional:
        print("[주의] PROVISIONAL 원장은 성과 발표와 확정 지수 산출에 사용할 수 없습니다")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
