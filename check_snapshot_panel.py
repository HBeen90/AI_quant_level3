"""data/snapshots/*.csv 교차파일 패널 검사.

스냅샷이 시점별 개별 파일로 나뉘어 있는 구조를 전제한다 (diagnose_pit.py 가
처리하지 못한 케이스). 읽기 전용이며 아무것도 수정하지 않는다.

확인하는 것
-----------
1. 스냅샷 목록 · 선정기준일 · 종목수 · eligible 수
2. 종목별 exposure / mem_ratio 가 시점마다 실제로 변하는지
3. 군(group) 전이 횟수 — 편출입 압력의 직접 측정치
4. 버퍼 임계값 교차 횟수 (핵심 30/27, 위성 70/67)
   -> 실측 경로에 버퍼 민감도를 논할 만한 교차가 있는지 판단

사용
----
    python check_snapshot_panel.py
    python check_snapshot_panel.py --dir data/snapshots
    python check_snapshot_panel.py --core-new 30 --core-hold 27 \
                                   --sat-new 70 --sat-hold 67
"""

from __future__ import annotations

import argparse
import csv
import io
import re
from collections import defaultdict
from pathlib import Path

EXPOSURE_COL_CANDIDATES = ("exposure", "hbm_exposure", "노출도")
MEM_COL_CANDIDATES = ("mem_ratio", "memory_ratio", "메모리향비중")
TICKER_COL_CANDIDATES = ("ticker", "코드", "종목코드", "code")
NAME_COL_CANDIDATES = ("name", "종목명")
DATE_COL_CANDIDATES = ("selection_date", "as_of", "기준일")
GROUP_COL_CANDIDATES = ("group", "bucket", "군")
ELIGIBLE_COL_CANDIDATES = ("eligible", "적격")


def read_text(path: Path) -> str:
    for encoding in ("utf-8-sig", "cp949", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return ""


def pick(header: list[str], candidates: tuple[str, ...]) -> str | None:
    lowered = {c.strip().lower(): c for c in header}
    for cand in candidates:
        if cand.lower() in lowered:
            return lowered[cand.lower()]
    return None


def to_float(raw: str) -> float | None:
    if raw is None:
        return None
    text = raw.strip().replace("%", "").replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def truthy(raw: str) -> bool:
    return (raw or "").strip().lower() in {"1", "y", "yes", "true", "t", "적격", "o"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", default="data/snapshots")
    parser.add_argument("--core-new", type=float, default=30.0)
    parser.add_argument("--core-hold", type=float, default=27.0)
    parser.add_argument("--sat-new", type=float, default=70.0)
    parser.add_argument("--sat-hold", type=float, default=67.0)
    args = parser.parse_args()

    root = Path(args.dir)
    if not root.is_dir():
        print(f"error: 디렉터리가 없다: {root}")
        return 2

    files = sorted(p for p in root.glob("*.csv"))
    if not files:
        print(f"error: {root} 에 CSV 가 없다")
        return 2

    # (date, ticker) -> row
    panel: dict[str, dict[str, dict[str, str]]] = {}
    names: dict[str, str] = {}
    cols: dict[str, str | None] = {}

    print("=" * 70)
    print("1. 스냅샷 목록")
    print("=" * 70)

    for path in files:
        text = read_text(path)
        if not text.strip():
            print(f"  ! {path.name}: 빈 파일")
            continue
        reader = csv.DictReader(io.StringIO(text))
        header = list(reader.fieldnames or [])
        if not header:
            print(f"  ! {path.name}: 헤더 없음")
            continue

        c_date = pick(header, DATE_COL_CANDIDATES)
        c_tick = pick(header, TICKER_COL_CANDIDATES)
        c_name = pick(header, NAME_COL_CANDIDATES)
        c_exp = pick(header, EXPOSURE_COL_CANDIDATES)
        c_mem = pick(header, MEM_COL_CANDIDATES)
        c_grp = pick(header, GROUP_COL_CANDIDATES)
        c_elig = pick(header, ELIGIBLE_COL_CANDIDATES)
        cols = {
            "date": c_date, "ticker": c_tick, "name": c_name,
            "exposure": c_exp, "mem": c_mem, "group": c_grp,
            "eligible": c_elig,
        }
        if not c_tick:
            print(f"  ! {path.name}: 종목 식별 열을 찾지 못함 ({header})")
            continue

        rows = list(reader)
        dates = {(r.get(c_date) or "").strip() for r in rows} if c_date else set()
        dates.discard("")
        sel = sorted(dates)[0] if len(dates) == 1 else (
            f"MIXED({len(dates)})" if dates else path.stem
        )

        n_elig = sum(1 for r in rows if c_elig and truthy(r.get(c_elig, "")))
        elig_note = f" · eligible {n_elig}" if c_elig else ""
        print(f"  {path.name:34s} {sel:12s} {len(rows):3d}종목{elig_note}")

        bucket = panel.setdefault(sel, {})
        for row in rows:
            ticker = (row.get(c_tick) or "").strip()
            if not ticker:
                continue
            bucket[ticker] = row
            if c_name and not names.get(ticker):
                names[ticker] = (row.get(c_name) or "").strip()

    dates_sorted = sorted(panel)
    if len(dates_sorted) < 2:
        print("\n시점이 2개 미만이다 — 패널 검사를 할 수 없다.")
        return 1

    print(f"\n  총 {len(dates_sorted)}개 시점: {dates_sorted[0]} ~ {dates_sorted[-1]}")

    # ------------------------------------------------ 2. 값 변동
    print()
    print("=" * 70)
    print("2. 노출도가 시점마다 변하는가")
    print("=" * 70)

    def variation(field: str, col_key: str) -> tuple[int, int, list[str]]:
        per: dict[str, set[float]] = defaultdict(set)
        for date in dates_sorted:
            for ticker, row in panel[date].items():
                col = cols.get(col_key)
                if not col:
                    continue
                value = to_float(row.get(col, ""))
                if value is not None:
                    per[ticker].add(round(value, 6))
        total = sum(1 for t in per if per[t])
        varying = sum(1 for t in per if len(per[t]) > 1)
        constant = sorted(t for t in per if len(per[t]) == 1)
        return varying, total, constant

    for label, key in (("exposure", "exposure"), ("mem_ratio", "mem")):
        if not cols.get(key):
            print(f"  '{label}' 열이 없다 — 건너뜀")
            continue
        varying, total, constant = variation(label, key)
        if total == 0:
            print(f"  '{label}': 값이 모두 비어 있다")
            continue
        share = varying / total
        verdict = "실측 시계열" if share >= 0.5 else (
            "일부만 갱신" if varying else "전 시점 동일 — 형식만 시점화"
        )
        print(f"  '{label}': {total}종목 중 {varying}종목 변동 "
              f"({share:.0%})  ->  {verdict}")
        if constant and len(constant) <= 12:
            shown = ", ".join(f"{t}({names.get(t, '')})" for t in constant[:12])
            print(f"     고정 종목: {shown}")

    # 예시 경로
    col_exp = cols.get("exposure")
    if col_exp:
        print("\n  종목별 exposure 추이 (변동폭 상위 5)")
        series: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for date in dates_sorted:
            for ticker, row in panel[date].items():
                value = to_float(row.get(col_exp, ""))
                if value is not None:
                    series[ticker].append((date, value))
        ranked = sorted(
            (t for t in series if len(series[t]) >= 2),
            key=lambda t: max(v for _, v in series[t]) - min(v for _, v in series[t]),
            reverse=True,
        )
        for ticker in ranked[:5]:
            path_str = " -> ".join(f"{v:.1f}" for _, v in series[ticker])
            print(f"     {ticker} {names.get(ticker, ''):10s} {path_str}")

    # ------------------------------------------------ 3. 군 전이
    col_grp = cols.get("group")
    if col_grp:
        print()
        print("=" * 70)
        print("3. 군(group) 전이")
        print("=" * 70)
        transitions = 0
        per_ticker: dict[str, int] = defaultdict(int)
        detail: list[str] = []
        for index in range(1, len(dates_sorted)):
            prev_date, curr_date = dates_sorted[index - 1], dates_sorted[index]
            prev, curr = panel[prev_date], panel[curr_date]
            changed = 0
            for ticker in set(prev) | set(curr):
                g_prev = (prev.get(ticker, {}).get(col_grp) or "").strip() or "-"
                g_curr = (curr.get(ticker, {}).get(col_grp) or "").strip() or "-"
                if g_prev != g_curr:
                    changed += 1
                    transitions += 1
                    per_ticker[ticker] += 1
            detail.append(f"     {prev_date} -> {curr_date}: {changed}건")
        print(f"  총 전이 {transitions}건 ({len(dates_sorted) - 1}개 구간)")
        for line in detail:
            print(line)
        if per_ticker:
            top = sorted(per_ticker.items(), key=lambda kv: -kv[1])[:5]
            print("  전이 빈발 종목: " + ", ".join(
                f"{t}({names.get(t, '')}) {n}회" for t, n in top
            ))

    # ------------------------------------------------ 4. 임계값 교차
    print()
    print("=" * 70)
    print("4. 버퍼 임계값 교차 — 민감도 분석 가능성 판단")
    print("=" * 70)

    def crossings(col_key: str, new_th: float, hold_th: float, label: str) -> None:
        col = cols.get(col_key)
        if not col:
            print(f"  '{label}': 열이 없다 — 건너뜀")
            return
        series: dict[str, list[float]] = defaultdict(list)
        for date in dates_sorted:
            for ticker, row in panel[date].items():
                value = to_float(row.get(col, ""))
                if value is not None:
                    series[ticker].append(value)

        cross_new = cross_band = 0
        in_band_obs = 0
        band_tickers: set[str] = set()
        for ticker, values in series.items():
            for value in values:
                if hold_th <= value < new_th:
                    in_band_obs += 1
                    band_tickers.add(ticker)
            for i in range(1, len(values)):
                a, b = values[i - 1], values[i]
                if (a >= new_th) != (b >= new_th):
                    cross_new += 1
                if (a >= hold_th) != (b >= hold_th):
                    cross_band += 1

        print(f"  '{label}'  신규 {new_th:g} / 유지 {hold_th:g}")
        print(f"     신규기준 교차 : {cross_new}회")
        print(f"     유지기준 교차 : {cross_band}회")
        print(f"     완충구간 체류 : {in_band_obs}개 관측 "
              f"({len(band_tickers)}종목)")
        if cross_new == 0 and in_band_obs == 0:
            print("     -> 임계값 근처에 움직임이 없다. 실측만으로는 "
                  "버퍼 효과를 관측할 수 없다 (합성 경로가 필요한 이유)")
        elif in_band_obs and cross_new:
            print("     -> 완충구간 진입과 교차가 모두 관측된다. "
                  "실측 기반 버퍼 민감도 산출이 가능하다")
        else:
            print("     -> 관측이 희소하다. 실측 수치는 참고로만 쓰고 "
                  "합성 강건성 점검을 병기할 것")

    crossings("exposure", args.core_new, args.core_hold, "핵심군 (exposure)")
    print()
    crossings("mem", args.sat_new, args.sat_hold, "위성군 (mem_ratio)")

    print()
    print("=" * 70)
    print("주의: 임계값 기본값은 30/27 · 70/67 이다. 방법론과 다르면 "
          "--core-new 등으로 지정할 것.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
