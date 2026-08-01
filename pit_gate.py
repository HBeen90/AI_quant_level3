"""strict PIT 게이트 ― 스냅샷·판정원장의 시점 정합성 검사.

읽기 전용. 각 선정기준일에 사용된 값의 as-of 날짜가 그 선정기준일을
넘지 않는지, 그리고 근거 키가 붙어 있는지를 검사한다.

사용
----
    python pit_gate.py
    python pit_gate.py --strict          # 경고도 실패로
    python pit_gate.py --snapshots data/snapshots --ledger data/verdict_ledger.csv

종료코드: 0 통과 · 1 경고(+--strict) · 2 위반
"""

from __future__ import annotations

import argparse
import csv
import glob
import io
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
RCP_RE = re.compile(r"^\d{14}$")

# 스냅샷에서 as-of 를 갖는 열 -> 그 as-of 를 담은 열
ASOF_PAIRS = {
    "float_mcap": "ff_market_cap_asof",
    "free_float": "free_float_asof",
}

# 잠정치를 나타내는 값
PROVISIONAL_TOKENS = {"provisional", "잠정", "prov", "temp", "가정", "assumed"}


def read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    for encoding in ("utf-8-sig", "cp949", "latin-1"):
        try:
            text = path.read_text(encoding=encoding)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        return [], []
    reader = csv.DictReader(io.StringIO(text))
    return list(reader.fieldnames or []), list(reader)


def parse_date(raw: str):
    match = DATE_RE.match((raw or "").strip())
    if not match:
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def check_snapshots(pattern: str) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warns: list[str] = []
    files = sorted(glob.glob(pattern))
    if not files:
        errors.append(f"스냅샷을 찾지 못했다: {pattern}")
        return errors, warns

    for file_path in files:
        path = Path(file_path)
        header, rows = read_rows(path)
        if not rows:
            warns.append(f"{path.name}: 데이터 행이 없다")
            continue

        for lineno, row in enumerate(rows, start=2):
            loc = f"{path.name}:{lineno}"
            sel_raw = (row.get("selection_date") or "").strip()
            sel = parse_date(sel_raw)
            if sel is None:
                errors.append(f"{loc} selection_date 형식 오류: {sel_raw!r}")
                continue

            # as-of 쌍 검사
            for value_col, asof_col in ASOF_PAIRS.items():
                if value_col not in header:
                    continue
                value = (row.get(value_col) or "").strip()
                if not value:
                    continue
                if asof_col not in header:
                    warns.append(
                        f"{loc} '{value_col}' 에 대응하는 '{asof_col}' 열이 없다 "
                        "― as-of 없는 값은 PIT 검증 불가"
                    )
                    continue
                asof_raw = (row.get(asof_col) or "").strip()
                asof = parse_date(asof_raw)
                if asof is None:
                    errors.append(
                        f"{loc} {asof_col} 이 비었거나 형식 오류: {asof_raw!r}"
                    )
                elif asof > sel:
                    errors.append(
                        f"{loc} PIT 위반: {asof_col}({asof_raw}) > "
                        f"selection_date({sel_raw})"
                    )

            # 잠정치 검사
            for col, value in row.items():
                token = (value or "").strip().lower()
                if token in PROVISIONAL_TOKENS:
                    warns.append(f"{loc} '{col}' 이 잠정치다: {value!r}")

    return errors, warns


def check_ledger(path: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warns: list[str] = []
    if not path.exists():
        warns.append(f"원장을 찾지 못했다: {path}")
        return errors, warns

    header, rows = read_rows(path)
    if not rows:
        warns.append(f"{path.name}: 데이터 행이 없다")
        return errors, warns

    has_rcp = any(c.lower() in {"source_rcp_no", "rcept_no", "rcp_no"} for c in header)
    if not has_rcp:
        warns.append(
            f"{path.name}: 접수번호 열이 없다 ― 근거 키 없이는 값을 "
            "원문으로 되짚을 수 없다 (strict PIT 미충족)"
        )

    # 회계연도별 disclosed_at 분포 ― 법정기한 가정 탐지
    by_year: dict[str, set[str]] = defaultdict(set)
    for lineno, row in enumerate(rows, start=2):
        loc = f"{path.name}:{lineno}"
        year = (row.get("fiscal_year") or "").strip()
        disclosed_raw = (row.get("disclosed_at") or "").strip()
        if not disclosed_raw:
            errors.append(f"{loc} disclosed_at 이 비어 있다")
            continue
        if parse_date(disclosed_raw) is None:
            errors.append(f"{loc} disclosed_at 형식 오류: {disclosed_raw!r}")
            continue
        if year:
            by_year[year].add(disclosed_raw)

        for col in ("source_rcp_no", "rcept_no", "rcp_no"):
            if col in header:
                value = (row.get(col) or "").strip()
                if not value:
                    errors.append(f"{loc} {col} 이 비어 있다")
                elif "," in value or ";" in value:
                    errors.append(f"{loc} {col} 에 복수 접수번호가 있다: {value!r}")
                elif not RCP_RE.match(value):
                    errors.append(f"{loc} {col} 는 14자리 숫자여야 한다: {value!r}")

    # 핵심 검사: 한 회계연도의 disclosed_at 이 전부 동일하면 실제 접수일이 아니다
    for year in sorted(by_year):
        values = by_year[year]
        if len(values) == 1:
            only = next(iter(values))
            month_day = only[5:]
            hint = " (사업보고서 법정기한)" if month_day == "03-31" else ""
            errors.append(
                f"{path.name}: 회계연도 {year} 의 disclosed_at 이 전 종목 "
                f"동일하다 ({only}){hint} ― 실제 DART 접수일이 아니라 "
                "가정값으로 판단된다"
            )
        elif len(values) < 5:
            warns.append(
                f"{path.name}: 회계연도 {year} 의 disclosed_at 이 "
                f"{len(values)}종류뿐이다 {sorted(values)} ― 실제 접수일 분포가 "
                "맞는지 확인할 것"
            )

    return errors, warns


def check_control(path: Path) -> tuple[list[str], list[str]]:
    """관리종목 수집기의 양성 통제 검사."""
    errors: list[str] = []
    warns: list[str] = []
    if not path.exists():
        warns.append(f"통제 로그가 없다: {path}")
        return errors, warns

    _, rows = read_rows(path)
    matched = [r for r in rows if (r.get("matched") or "").strip().lower()
               in {"1", "y", "yes", "true", "t"}]

    if len(rows) < 5:
        errors.append(
            f"{path.name}: 통제 사례가 {len(rows)}건뿐이다 ― 수집기 오작동과 "
            "'이력 없음'을 구분할 수 없다. 관리종목 지정이 확실한 종목 "
            "5건 이상을 양성 통제로 추가할 것"
        )
    if rows and not matched:
        errors.append(
            f"{path.name}: 통제 사례 {len(rows)}건 중 매칭 0건 ― 수집기가 "
            "이벤트를 찾지 못하고 있다"
        )
    return errors, warns


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--snapshots", default="data/snapshots/*.csv")
    parser.add_argument("--ledger", default="data/verdict_ledger.csv")
    parser.add_argument(
        "--control",
        default="evidence/kind_admin_history_20260730/control_log.csv",
    )
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    all_errors: list[str] = []
    all_warns: list[str] = []

    for title, (errors, warns) in (
        ("스냅샷 as-of 정합성", check_snapshots(args.snapshots)),
        ("원장 접수일·근거키", check_ledger(Path(args.ledger))),
        ("수집기 양성 통제", check_control(Path(args.control))),
    ):
        print("=" * 68)
        print(title)
        print("=" * 68)
        if not errors and not warns:
            print("  통과")
        for message in errors:
            print(f"  [위반] {message}")
        for message in warns:
            print(f"  [경고] {message}")
        print()
        all_errors += errors
        all_warns += warns

    print(f"위반 {len(all_errors)}건 · 경고 {len(all_warns)}건")
    if all_errors:
        print("\n=> strict PIT 미충족. 슬라이드에 'strict PIT' 표현을 쓸 수 없다.")
        return 2
    if all_warns and args.strict:
        return 1
    print("\n=> strict PIT 게이트 통과.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
