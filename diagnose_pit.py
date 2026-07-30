"""실측 노출도 시계열이 있는지 진단한다.

버퍼 민감도 실험이 합성 경로 기반인지 실측 기반인지를 판정하기 위한
읽기 전용 진단 도구다. 아무것도 수정하지 않는다.

사용
----
    python diagnose_pit.py                # 레포 루트에서
    python diagnose_pit.py --root .       # 경로 지정
    python diagnose_pit.py --max-rows 0   # 전체 행 읽기 (기본 200000)

의존성 없음 (표준 라이브러리만). Windows PowerShell 에서 그대로 실행된다.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

# ------------------------------------------------------------------ 패턴

DATE_VALUE_RE = re.compile(r"^\s*(\d{4}[-/.]\d{2}[-/.]\d{2}|\d{8}|\d{4}[-/.]\d{2})\s*$")

# 심사시점(as-of) 축으로 쓰일 만한 열 이름
ASOF_NAME_RE = re.compile(
    r"selection|asof|as_of|base_date|basis|rebal|review|기준일|심사|평가일|산정일",
    re.IGNORECASE,
)
# 노출도로 쓰일 만한 열 이름
EXPOSURE_NAME_RE = re.compile(
    r"exposure|expos|노출|hbm_rev|hbm_ratio|rev_ratio|revenue_ratio|"
    r"관여|involve|score|memory_share|메모리|비중_노출|적합",
    re.IGNORECASE,
)
# 종목 식별자
TICKER_NAME_RE = re.compile(
    r"^(ticker|code|stock_code|symbol|isin|종목코드|종목|shortcode)$", re.IGNORECASE
)

RNG_RE = re.compile(
    r"default_rng|np\.random|numpy\.random|RandomState|random\.seed|"
    r"random\.Random\(|seed\s*=|--seed|Generator\(|PCG64|MT19937"
)
ASOF_PARAM_RE = re.compile(
    r"bgn_de|end_de|rcept_dt|rcept_no|as_?of|selection_date|기준일|"
    r"--date|--asof|--as-of|--selection|point_in_time|\bpit\b",
    re.IGNORECASE,
)

SKIP_DIRS = {
    ".git", ".venv", "venv", "env", "__pycache__", "node_modules",
    ".mypy_cache", ".pytest_cache", ".idea", ".vscode", "build", "dist",
}

NUMERIC_RE = re.compile(r"^\s*-?\d+(\.\d+)?\s*%?\s*$")


def read_text(path: Path) -> str:
    """인코딩을 관용적으로 읽는다 (utf-8-sig -> cp949 -> latin-1)."""
    for encoding in ("utf-8-sig", "cp949", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return ""


def iter_files(root: Path, suffixes: set[str]):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in suffixes:
            yield path


# ------------------------------------------------------------ CSV 분석

class CsvProfile:
    def __init__(self, path: Path, root: Path):
        self.path = path
        self.rel = path.relative_to(root).as_posix()
        self.header: list[str] = []
        self.n_rows = 0
        self.uniques: dict[str, set[str]] = {}
        self.date_cols: list[str] = []
        self.asof_cols: list[str] = []
        self.exposure_cols: list[str] = []
        self.ticker_col: str | None = None
        self.error: str | None = None
        self.truncated = False

    def load(self, max_rows: int) -> None:
        text = read_text(self.path)
        if not text.strip():
            self.error = "빈 파일 또는 디코딩 실패"
            return
        try:
            reader = csv.DictReader(io.StringIO(text))
            self.header = list(reader.fieldnames or [])
            if not self.header:
                self.error = "헤더 없음"
                return
            samples: dict[str, list[str]] = {c: [] for c in self.header}
            uniques: dict[str, set[str]] = {c: set() for c in self.header}
            for row in reader:
                self.n_rows += 1
                for col in self.header:
                    value = (row.get(col) or "").strip()
                    if len(samples[col]) < 400:
                        samples[col].append(value)
                    if len(uniques[col]) <= 5000:
                        uniques[col].add(value)
                if max_rows and self.n_rows >= max_rows:
                    self.truncated = True
                    break
        except csv.Error as exc:
            self.error = f"CSV 파싱 실패: {exc}"
            return

        self.uniques = uniques
        self._classify(samples)

    def _classify(self, samples: dict[str, list[str]]) -> None:
        for col in self.header:
            values = [v for v in samples[col] if v]
            if values:
                hits = sum(1 for v in values if DATE_VALUE_RE.match(v))
                if hits >= max(1, int(0.8 * len(values))):
                    self.date_cols.append(col)
            if ASOF_NAME_RE.search(col):
                self.asof_cols.append(col)
            if EXPOSURE_NAME_RE.search(col):
                self.exposure_cols.append(col)
            if self.ticker_col is None and TICKER_NAME_RE.match(col.strip()):
                self.ticker_col = col

    def primary_asof(self) -> str | None:
        """심사시점 축으로 가장 유력한 열.

        이름으로 지정된 열을 우선하고, 없으면 '날짜형이면서 고유값 수가
        행 수보다 훨씬 적은' 열을 고른다 (패널 키의 특징).
        """
        named = [c for c in self.asof_cols if c in self.date_cols]
        if named:
            return min(named, key=lambda c: len(self.uniques.get(c, ())))
        if self.asof_cols:
            return self.asof_cols[0]
        candidates = [
            c for c in self.date_cols
            if self.n_rows and 1 < len(self.uniques.get(c, ())) <= max(2, self.n_rows // 2)
        ]
        if candidates:
            return min(candidates, key=lambda c: len(self.uniques.get(c, ())))
        return None


def analyze_panel(profile: CsvProfile, root: Path, max_rows: int) -> list[str]:
    """(종목, 시점) 패널에서 노출도가 시점마다 변하는지 검사."""
    notes: list[str] = []
    asof = profile.primary_asof()
    if not asof or not profile.exposure_cols or not profile.ticker_col:
        return notes

    n_dates = len(profile.uniques.get(asof, ()))
    if n_dates < 2:
        return notes

    text = read_text(profile.path)
    reader = csv.DictReader(io.StringIO(text))
    # ticker -> exposure_col -> set(values)
    per_ticker: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    seen = 0
    for row in reader:
        seen += 1
        ticker = (row.get(profile.ticker_col) or "").strip()
        if not ticker:
            continue
        for col in profile.exposure_cols:
            value = (row.get(col) or "").strip()
            if value:
                per_ticker[ticker][col].add(value)
        if max_rows and seen >= max_rows:
            break

    for col in profile.exposure_cols:
        varying = sum(1 for t in per_ticker if len(per_ticker[t].get(col, ())) > 1)
        total = sum(1 for t in per_ticker if per_ticker[t].get(col))
        if not total:
            continue
        share = varying / total
        if varying == 0:
            notes.append(
                f"  [!] '{col}' 은 {n_dates}개 시점에 걸쳐 종목별 값이 "
                f"전혀 변하지 않는다 ({total}종목 전부 단일값) "
                f"― 형식만 시점화된 것으로 의심"
            )
        elif share < 0.5:
            notes.append(
                f"  [~] '{col}' 은 {total}종목 중 {varying}종목만 시점별로 변한다 "
                f"({share:.0%}) ― 일부만 갱신된 상태일 수 있다"
            )
        else:
            notes.append(
                f"  [OK] '{col}' 은 {total}종목 중 {varying}종목이 시점별로 변한다 "
                f"({share:.0%}) ― 실제 시계열로 보인다"
            )
    return notes


# ------------------------------------------------------------ 코드 분석

def scan_python(root: Path) -> tuple[list[tuple[str, int, str]], dict[str, list[str]]]:
    rng_hits: list[tuple[str, int, str]] = []
    argparse_by_file: dict[str, list[str]] = {}

    for path in iter_files(root, {".py"}):
        rel = path.relative_to(root).as_posix()
        text = read_text(path)
        if not text:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if RNG_RE.search(line):
                rng_hits.append((rel, lineno, line.strip()[:120]))
        args = re.findall(r"add_argument\(\s*[\"']([^\"']+)[\"']", text)
        if args:
            argparse_by_file[rel] = args
    return rng_hits, argparse_by_file


# ------------------------------------------------------------ 출력

def hr(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="레포 루트 (기본: 현재 디렉터리)")
    parser.add_argument(
        "--max-rows", type=int, default=200000,
        help="파일당 최대 읽기 행 수 (0 = 전체)",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"error: 디렉터리가 아니다: {root}", file=sys.stderr)
        return 2

    print(f"레포 루트: {root}")

    # ---------------------------------------------------- 1. CSV 목록
    hr("1. CSV / 데이터 파일 헤더")
    profiles: list[CsvProfile] = []
    for path in iter_files(root, {".csv", ".tsv"}):
        profile = CsvProfile(path, root)
        profile.load(args.max_rows)
        profiles.append(profile)

    if not profiles:
        print("  CSV 파일을 찾지 못했다.")
    for profile in profiles:
        suffix = " (일부만 읽음)" if profile.truncated else ""
        print(f"\n-- {profile.rel}  [{profile.n_rows}행{suffix}]")
        if profile.error:
            print(f"   ! {profile.error}")
            continue
        print(f"   열: {', '.join(profile.header)}")

    # ---------------------------------------------------- 2. 심사시점 축
    hr("2. 심사시점(as-of) 축 ― 결정적 검사")
    panel_files: list[CsvProfile] = []
    for profile in profiles:
        if profile.error or not profile.header:
            continue
        asof = profile.primary_asof()
        if not asof:
            continue
        values = sorted(v for v in profile.uniques.get(asof, ()) if v)
        print(f"\n-- {profile.rel}   축 열: '{asof}'")
        print(f"   고유 시점 수: {len(values)}")
        shown = values if len(values) <= 20 else values[:10] + ["..."] + values[-5:]
        print(f"   값: {', '.join(shown)}")
        if profile.exposure_cols:
            print(f"   노출도 후보 열: {', '.join(profile.exposure_cols)}")
        if profile.ticker_col:
            print(f"   종목 식별 열: {profile.ticker_col}")
        if len(values) >= 2:
            panel_files.append(profile)
            for note in analyze_panel(profile, root, args.max_rows):
                print(note)
        else:
            print("   -> 시점이 1개다. 이 파일은 횡단면 단면이다.")

    if not panel_files:
        print("\n  시점이 2개 이상인 파일이 없다.")

    # ---------------------------------------------------- 3. 난수
    hr("3. 난수 생성기 사용 위치")
    rng_hits, argparse_by_file = scan_python(root)
    if not rng_hits:
        print("  난수 사용 흔적이 없다.")
    else:
        by_file = Counter(f for f, _, _ in rng_hits)
        for rel, count in by_file.most_common():
            print(f"\n-- {rel}  ({count}건)")
            for f, lineno, line in rng_hits:
                if f == rel:
                    print(f"   {lineno:5d}: {line}")

    # ---------------------------------------------------- 4. CLI 파라미터
    hr("4. 스크립트 CLI 파라미터 (시점 지정 가능 여부)")
    if not argparse_by_file:
        print("  argparse 를 쓰는 파일이 없다.")
    for rel, params in sorted(argparse_by_file.items()):
        asof_params = [p for p in params if ASOF_PARAM_RE.search(p)]
        mark = "시점 파라미터 있음" if asof_params else "시점 파라미터 없음"
        print(f"\n-- {rel}  [{mark}]")
        print(f"   전체: {', '.join(params)}")
        if asof_params:
            print(f"   해당: {', '.join(asof_params)}")

    # ---------------------------------------------------- 5. 판정
    hr("5. 판정")
    max_dates = 0
    best: CsvProfile | None = None
    for profile in profiles:
        if profile.error:
            continue
        asof = profile.primary_asof()
        if not asof:
            continue
        count = len([v for v in profile.uniques.get(asof, ()) if v])
        if count > max_dates:
            max_dates, best = count, profile

    exposure_varies = False
    for profile in panel_files:
        for note in analyze_panel(profile, root, args.max_rows):
            if "[OK]" in note:
                exposure_varies = True

    rng_files = {f for f, _, _ in rng_hits}
    sensitivity_rng = sorted(
        f for f in rng_files
        if re.search(r"sensitiv|buffer|robust|synth|simul|mc_|monte", f, re.IGNORECASE)
    )

    print(f"  최대 고유 심사시점 수 : {max_dates}"
          f"{'  (' + best.rel + ')' if best else ''}")
    print(f"  노출도 시점별 변동     : {'있음' if exposure_varies else '없음/미확인'}")
    print(f"  난수 사용 파일 수      : {len(rng_files)}")
    if sensitivity_rng:
        print(f"  민감도 관련 난수 파일  : {', '.join(sensitivity_rng)}")

    print()
    if max_dates < 2:
        print("  => [합성] 심사시점이 1개 이하다. 노출도 실측 시계열이 없다.")
        print("     발표에서 seed 11개 설명을 그대로 유지하는 것이 맞다.")
    elif not exposure_varies:
        print("  => [주의: 형식만 시점화] 시점은 여러 개지만 노출도가 시점별로")
        print("     변하지 않는다. 시계열 형태만 갖춘 것이므로 실측이라고")
        print("     발표하면 값 대조 시 드러난다. 반드시 원인을 확인할 것.")
    else:
        print("  => [실측 있음] 시점이 2개 이상이고 노출도가 시점별로 변한다.")
        print("     seed 기반 강건성 논거를 실경로 + 부트스트랩으로 교체해야 한다.")
        print("     슬라이드 11 의 88->82->46->17 수치도 재산출 대상이다.")

    print()
    print("  주의: 이 판정은 열 이름 휴리스틱에 기반한다. 위 1~4 절의 실제")
    print("  열 이름과 값을 함께 확인할 것.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
