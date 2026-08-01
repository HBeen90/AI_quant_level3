# -*- coding: utf-8 -*-
"""
fetch_prices_kis.py ― KIS 로 가격 패널을 수집해 **기존 캐시 형식**으로 쓴다
============================================================================
엔진(run_backtest.py)을 한 줄도 바꾸지 않는다. 이 스크립트가 만든 CSV 를
`--prices-cache` 로 주면 `fetch_prices()` 의 캐시 분기가 그대로 읽는다.
전송 계층만 pykrx -> KIS 로 바뀌고, 캐시 검증(오름차순·중복·기간 커버리지
fail-closed)은 엔진 쪽 코드가 계속 수행한다.

캐시 형식 (run_backtest.fetch_prices 와 동일)
  - CSV, 첫 열 날짜(오름차순·중복 없음), 이후 열 6자리 티커
  - 값은 수정주가 종가(float), 0원은 결측
  - 상장 전·미수신 종목은 NaN 열로 유지 (pykrx 경로와 같은 규약)

교차검증 (--compare)
  같은 기간·종목의 옛 pykrx 캐시와 겹치는 관측을 대조한다. 수정주가
  보정 방식은 기관마다 달라 완전 일치가 보장되지 않으므로, 이 대조는
  **재현성 판정의 핵심 게이트**다. 상대오차가 --tol 을 넘는 셀이 하나라도
  있으면 FAIL 로 중단하고 상세를 저장한다 - 그 상태로 FINAL 수치(레벨
  20,881.52)가 재현된다고 말하면 안 되기 때문이다.

사용
    # 1) 확정 스냅샷의 종목으로 수집 (권장)
    python analysis\\fetch_prices_kis.py --snapshots data\\snapshots ^
        --start 20200601 --end 20260723 --out out\\px_kis.csv ^
        --compare out\\px.csv

    # 2) 티커 직접 지정 (연결 점검용)
    python analysis\\fetch_prices_kis.py --tickers 005930,000660 ^
        --start 20260101 --end 20260723 --out out\\px_kis_smoke.csv

환경 변수: KIS_APP_KEY / KIS_APP_SECRET
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from analysis.kis_client import KISClient, KISError  # noqa: E402


def tickers_from_snapshots(path: str) -> list:
    files = sorted(glob.glob(os.path.join(path, "snapshot_*.csv")))
    if not files:
        sys.exit(f"[FAIL] 스냅샷이 없다: {path} - --tickers 로 직접 지정하거나 "
                 "스냅샷을 먼저 준비할 것")
    ts = set()
    for f in files:
        df = pd.read_csv(f, dtype={"ticker": str})
        if "ticker" not in df.columns:
            sys.exit(f"[FAIL] {os.path.basename(f)} 에 ticker 열이 없다")
        ts |= {str(t).zfill(6) for t in df["ticker"]}
    return sorted(ts)


def collect_panel(cli: KISClient, tickers: list, start: str, end: str,
                  what: str = "close") -> pd.DataFrame:
    cols = {}
    for i, t in enumerate(tickers, 1):
        df = cli.daily_prices(t, start, end)
        if df.empty:
            print(f"  [주의] {t}: 관측 없음 (상장 전·폐지 가능) - NaN 유지")
            cols[t] = pd.Series(dtype=float)
        else:
            cols[t] = df[what].astype(float)
        if i % 5 == 0 or i == len(tickers):
            print(f"  ... {i}/{len(tickers)} 종목")
    px = pd.DataFrame(cols).sort_index()
    px.index.name = "날짜"
    return px


def cross_check(new: pd.DataFrame, old_path: str, tol: float,
                report: str) -> None:
    old = pd.read_csv(old_path, index_col=0, parse_dates=True)
    old.columns = [str(c).zfill(6) for c in old.columns]
    common_t = sorted(set(new.columns) & set(old.columns))
    common_d = new.index.intersection(old.index)
    if not common_t or len(common_d) == 0:
        sys.exit("[FAIL] 교차검증 불가 - 옛 캐시와 겹치는 종목/날짜가 없다")
    a = new.loc[common_d, common_t]
    b = old.loc[common_d, common_t]
    both = a.notna() & b.notna()
    rel = ((a - b).abs() / b.abs()).where(both)
    worst = rel.max().sort_values(ascending=False)
    n_cells = int(both.sum().sum())
    n_bad = int((rel > tol).sum().sum())
    print(f"[대조] 겹침 {len(common_d)}일 x {len(common_t)}종목 "
          f"(유효 셀 {n_cells:,}) · 허용 상대오차 {tol:g}")
    print("[대조] 종목별 최대 상대오차 상위:")
    print(worst.head(8).to_string())
    detail = rel.stack().rename("rel_err").reset_index()
    detail.columns = ["날짜", "ticker", "rel_err"]
    detail = detail[detail["rel_err"] > 0].sort_values(
        "rel_err", ascending=False)
    os.makedirs(os.path.dirname(report) or ".", exist_ok=True)
    detail.head(1000).to_csv(report, index=False, encoding="utf-8-sig")
    print(f"[대조] 상세(불일치 상위 1000행) -> {report}")
    if n_bad:
        sys.exit(f"[FAIL] 허용오차 초과 셀 {n_bad}건 - 수정주가 보정 방식 차이일 "
                 "수 있다. 상세 파일을 확인하고, 해소 전에는 이 패널로 FINAL "
                 "재현을 주장하지 말 것")
    print("[OK] 옛 pykrx 캐시와 허용오차 내 전수 일치")


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--snapshots", help="스냅샷 폴더에서 종목 집합을 만든다")
    g.add_argument("--tickers", help="쉼표 구분 티커 (연결 점검용)")
    ap.add_argument("--start", required=True, help="YYYYMMDD")
    ap.add_argument("--end", required=True, help="YYYYMMDD")
    ap.add_argument("--out", required=True, help="출력 CSV (캐시 형식)")
    ap.add_argument("--compare", help="옛 pykrx 캐시와 교차검증")
    ap.add_argument("--tol", type=float, default=1e-6,
                    help="교차검증 허용 상대오차 (기본 1e-6 = 사실상 동일)")
    ap.add_argument("--value-out", help="거래대금 패널도 저장 (ADV60 용)")
    ap.add_argument("--rate", type=float, default=3.0, help="초당 호출 상한")
    a = ap.parse_args()

    try:
        cli = KISClient.from_env(rate_per_sec=a.rate)
        cli.token()
    except KISError as e:
        sys.exit(f"[FAIL] {e}")
    print("[OK] KIS 토큰 확보")

    tickers = (tickers_from_snapshots(a.snapshots) if a.snapshots
               else sorted({t.strip().zfill(6)
                            for t in a.tickers.split(",") if t.strip()}))
    print(f"[수집] {len(tickers)}종목 · {a.start}~{a.end} · 수정주가 종가")

    try:
        px = collect_panel(cli, tickers, a.start, a.end, "close")
    except KISError as e:
        sys.exit(f"[FAIL] 수집 중단 - {e} (부분 산출물은 저장하지 않는다)")

    if px.dropna(how="all").empty:
        sys.exit("[FAIL] 전 종목 관측 0 - 기간·티커를 확인할 것")
    if not px.index.is_monotonic_increasing or px.index.has_duplicates:
        sys.exit("[FAIL] 인덱스가 오름차순·중복 없음 조건을 깼다 (버그)")

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    px.to_csv(a.out, encoding="utf-8-sig")
    print(f"[저장] {a.out} ({px.shape[0]}일 x {px.shape[1]}종목)")

    if a.value_out:
        try:
            val = collect_panel(cli, tickers, a.start, a.end, "value")
        except KISError as e:
            sys.exit(f"[FAIL] 거래대금 수집 중단 - {e}")
        val.to_csv(a.value_out, encoding="utf-8-sig")
        print(f"[저장] {a.value_out} (거래대금 - ADV60 산출용)")

    if a.compare:
        if not os.path.exists(a.compare):
            sys.exit(f"[FAIL] 대조 대상이 없다: {a.compare}")
        cross_check(px, a.compare, a.tol,
                    os.path.join(os.path.dirname(a.out) or ".",
                                 "px_kis_crosscheck.csv"))
    else:
        print("[주의] 교차검증 없이 저장했다. FINAL 재현 주장 전에 "
              "--compare 로 옛 캐시와 대조할 것")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
