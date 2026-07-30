# -*- coding: utf-8 -*-
"""
collect_dividends.py - TR 지수용 배당 패널 수집 (제6조)
========================================================
무엇을 만드는가
---------------
`run_backtest.py --mode gross_tr|both --dividends <CSV>` 가 요구하는 배당 계약
파일을 만든다.

    ex_date, ticker, dps        (배당락일 · 종목 · 주당배당금)

TR 경로(`simulate_index(mode="gross_tr")`)와 제수 보정
(`index_calc.adjust_divisor_for_dividend`)은 이미 있고 검증도 끝났다
(tests/test_tr_equivalence.py). 없는 것은 **입력 데이터 하나**뿐이었다.

반드시 알고 써야 하는 두 가지 근사
----------------------------------
pykrx 는 **배당락일을 제공하지 않는다.** 주는 것은 연간 주당배당금(DPS)뿐이다.
그래서 이 수집기는 두 곳에서 근사한다. 둘 다 결과를 바꾸므로 숨기지 않는다.

  (1) 배당락일 = 해당 연도 **12월 마지막 거래일**
      한국의 12월 결산법인은 배당기준일이 12/31이고, T+2 결제 때문에 12월
      마지막 거래일에 사면 배당을 못 받는다 - 그래서 그날이 배당락일이다.
      한계: 2023년 배당절차 개선 이후 기준일을 따로 정하는 기업이 있다.
            그런 기업은 실제 배당락일이 다르다.

  (2) 연간 DPS 를 **12월에 한 번에** 반영
      분기배당 기업(예: 삼성전자)은 실제로는 3·6·9·12월에 나눠 떨어진다.
      연 단위 총액은 맞지만 **경로가 다르다.** 지수 레벨의 중간 궤적과
      배당락일 전후의 제수 조정 시점이 실제와 어긋난다.

그래서 이 산출물로 만든 TR 계열은 **보조 비교**로만 쓰고, 발표 수치로 인용하기
전에 실제 배당락일 원천(예: DART 현금배당결정 공시)으로 검증해야 한다.
--acknowledge-approximation 을 명시적으로 붙여야 파일이 생성되는 이유가 그것이다.

날짜는 어떻게 정하는가
----------------------
달력을 추측하지 않는다. `out/px.csv` 의 실제 거래일 인덱스에서 뽑는다.

    배당락일   = 사업연도 Y 의 12월 **마지막 거래일**
    DPS 조회일 = Y+1 년 6월 **마지막 거래일**
                 (FY Y 배당은 Y+1 년 3월 주총에서 확정되므로 6월이면 반영됨)

사용
----
    python analysis\\collect_dividends.py --acknowledge-approximation
    python analysis\\run_backtest.py --snapshots data\\snapshots ^
        --prices-cache out\\px.csv --mode both --dividends data\\dividends.csv ^
        --policy mid --out out\\backtest_tr
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

#: 배당락일이 표본 안에 들어오는 사업연도. FY2019 의 배당락일(2019-12)은
#: 백테스트 시작(2020-06-15) 이전이라 의미가 없고, FY2026 은 배당락일이
#: 표본 종료(2026-07-23) 이후라 아직 오지 않았다.
DEFAULT_FISCAL_YEARS = list(range(2020, 2026))


def load_price_index(px_path: str) -> pd.DatetimeIndex:
    px = pd.read_csv(px_path, index_col=0, parse_dates=True)
    if not px.index.is_monotonic_increasing:
        raise SystemExit("[중단] 가격 패널 인덱스가 날짜 오름차순이 아닙니다")
    return px.index


def last_trading_day(idx: pd.DatetimeIndex, year: int, month: int):
    """해당 연·월의 마지막 거래일. 없으면 None(달력 추측 금지)."""
    m = idx[(idx.year == year) & (idx.month == month)]
    return m[-1] if len(m) else None


def date_plan(idx: pd.DatetimeIndex, fiscal_years: list[int]) -> pd.DataFrame:
    """사업연도별 (배당락일, DPS 조회일). 거래일 인덱스에서만 뽑는다."""
    rows = []
    for y in fiscal_years:
        ex = last_trading_day(idx, y, 12)
        q = last_trading_day(idx, y + 1, 6)
        rows.append({"fiscal_year": y, "ex_date": ex, "dps_query_date": q,
                     "사용가능": ex is not None and q is not None})
    return pd.DataFrame(rows)


def collect(tickers: list[str], plan: pd.DataFrame) -> pd.DataFrame:
    """pykrx 로 사업연도별 DPS 를 받아 배당 계약 행으로 만든다."""
    try:
        from pykrx import stock
    except ImportError:
        raise SystemExit("[중단] pykrx 미설치 - pip install pykrx")

    rows = []
    for r in plan.itertuples(index=False):
        if not r.사용가능:
            print(f"  FY{r.fiscal_year}  건너뜀 (배당락일 또는 조회일이 "
                  f"가격 패널 범위 밖)")
            continue
        qymd = r.dps_query_date.strftime("%Y%m%d")
        try:
            fund = stock.get_market_fundamental(qymd, market="ALL")
        except Exception as exc:
            raise SystemExit(f"[중단] FY{r.fiscal_year} DPS 조회 실패({qymd}): {exc}")
        if fund is None or fund.empty or "DPS" not in fund.columns:
            raise SystemExit(f"[중단] FY{r.fiscal_year} DPS 응답이 비었습니다"
                             f"({qymd}) - 휴장일이거나 로그인 만료")
        got = 0
        for t in tickers:
            if t not in fund.index:
                continue
            dps = float(pd.to_numeric(fund.loc[t, "DPS"], errors="coerce") or 0)
            if dps <= 0:
                continue                      # 무배당은 행을 만들지 않는다
            rows.append({"ex_date": r.ex_date.strftime("%Y-%m-%d"),
                         "ticker": t, "dps": dps,
                         "fiscal_year": r.fiscal_year,
                         "dps_query_date": qymd})
            got += 1
        print(f"  FY{r.fiscal_year}  배당락일 {r.ex_date.date()} · "
              f"DPS 조회 {r.dps_query_date.date()} · 배당 종목 {got}개")
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="TR 지수용 배당 패널 수집")
    ap.add_argument("--prices-cache", default=os.path.join(HERE, "out", "px.csv"))
    ap.add_argument("--out", default=os.path.join(HERE, "data", "dividends.csv"))
    ap.add_argument("--years", default=None,
                    help="쉼표구분 사업연도 (기본 2020~2025)")
    ap.add_argument("--acknowledge-approximation", action="store_true",
                    help="배당락일·분기배당 근사를 인지했음을 명시(필수)")
    a = ap.parse_args()

    if not a.acknowledge_approximation:
        print("=" * 74)
        print("[중단] 이 수집기는 두 가지를 근사합니다. 인지 확인이 필요합니다.")
        print("=" * 74)
        print("  (1) 배당락일 = 12월 마지막 거래일로 가정")
        print("      - 2023년 배당절차 개정 이후 기준일이 다른 기업은 어긋납니다")
        print("  (2) 연간 DPS를 12월에 일괄 반영")
        print("      - 분기배당 기업(삼성전자 등)은 실제 경로와 다릅니다")
        print("        연 단위 총액은 맞지만 중간 궤적과 제수 조정 시점이 다릅니다")
        print()
        print("  이 데이터로 만든 TR 계열은 **보조 비교** 용도입니다.")
        print("  발표 수치로 인용하려면 실제 배당락일 원천(DART 현금배당결정")
        print("  공시 등)으로 검증해야 합니다.")
        print()
        print("  동의하면 --acknowledge-approximation 을 붙여 다시 실행하십시오.")
        return 1

    px_idx = load_price_index(a.prices_cache)
    px = pd.read_csv(a.prices_cache, index_col=0, nrows=1)
    tickers = [str(c).strip().zfill(6) for c in px.columns]
    years = ([int(x) for x in a.years.split(",")] if a.years
             else DEFAULT_FISCAL_YEARS)

    plan = date_plan(px_idx, years)
    print(f"가격 패널: {px_idx[0].date()} ~ {px_idx[-1].date()} · {len(tickers)}종목")
    print("\n[사업연도별 날짜 계획]")
    print(plan.to_string(index=False))
    print("\n[DPS 수집]")

    div = collect(tickers, plan)
    if div.empty:
        print("\n[중단] 수집된 배당이 0건입니다 - 조회 실패이거나 종목 코드 불일치")
        return 1

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    div[["ex_date", "ticker", "dps"]].to_csv(
        a.out, index=False, encoding="utf-8-sig")

    manifest = {
        "생성일": pd.Timestamp(px_idx[-1]).strftime("%Y-%m-%d") + " 기준 패널",
        "행수": int(len(div)),
        "사업연도": sorted(div["fiscal_year"].unique().tolist()),
        "배당락일": sorted(div["ex_date"].unique().tolist()),
        "종목수": int(div["ticker"].nunique()),
        "근사_1_배당락일": "12월 마지막 거래일로 가정 - 2023년 배당절차 개정 "
                          "이후 기준일이 다른 기업은 어긋남",
        "근사_2_분기배당": "연간 DPS를 12월 일괄 반영 - 분기배당 기업은 "
                          "총액은 맞으나 경로가 다름",
        "인용_제한": "보조 비교 전용. 실제 배당락일 원천으로 검증 전에는 "
                    "발표 수치로 인용 금지",
        "출처": "pykrx get_market_fundamental(DPS)",
    }
    mpath = os.path.splitext(a.out)[0] + "_manifest.json"
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"\n[완료] {len(div)}건 · {div['ticker'].nunique()}종목 · "
          f"배당락일 {div['ex_date'].nunique()}일")
    print(f"저장: {a.out}")
    print(f"      {mpath}")
    print("\n[연도별 배당 종목 수]")
    print(div.groupby("fiscal_year")["ticker"].size().to_string())
    print("\n다음 - PR/TR 병기 실행 (별도 출력 폴더를 쓸 것. 확정 산출물을 덮지 말 것)")
    print("  python analysis\\run_backtest.py --snapshots data\\snapshots ^")
    print("      --prices-cache out\\px.csv --mode both ^")
    print(f"      --dividends {os.path.relpath(a.out, HERE)} ^")
    print("      --policy mid --no-benchmark --out out\\backtest_tr")
    print("\n주의: TR 계열은 위 두 근사에 의존합니다. 보조 비교로만 쓰십시오.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
