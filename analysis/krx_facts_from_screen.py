# -*- coding: utf-8 -*-
"""
krx_facts_from_screen.py ― KRX 화면 다운로드로 시장 팩트를 만든다
====================================================================
`fetch_market_facts.py`(금융위 API)의 **대체 경로**다. 금융위 주식시세정보
API 를 찾지 못한 상태이고, KRX 화면 다운로드는 KDM 접근 제한 안내문이
**명시적으로 허용**한 경로다.

무엇을 입력받는가
  KRX 정보데이터시스템 [기본 통계] -> [주식] -> [종목시세] -> [전종목 시세]
  를 **선정일마다** 조회해 내려받은 CSV/XLSX 13개.

  파일에 조회일자가 들어 있지 않은 경우가 있으므로, **파일명 또는 인자로
  날짜를 받는다.** 날짜를 추론하지 않는다 - 잘못 짝지으면 그 회차 전체가
  다른 시점의 데이터로 산출되고, 그건 조용히 틀린다.

무엇을 만드는가 (`market_facts()` 반환 계약과 동일)
    ticker(index) · listed(bool) · close · market_cap · mcap_rank
    · adv60 · listed_days

  - `market_cap` · `close` · `mcap_rank` : 전종목 시세 파일에서. **순위는
    파일에 있는 값을 쓰지 않고 전종목 시총으로 다시 계산**한다(파일의 순위
    열은 시장 구분별일 수 있다).
  - `adv60` : 별도 일별 거래대금 패널에서 이벤트 직전 60거래일 평균
    (`out/value_kis.csv`). 화면 파일의 당일 거래대금은 ADV 가 아니다.
  - `listed_days` : `data/listing_dates.csv` (KRX 공식 상장일)

왜 순위를 다시 계산하는가
  `mcap_rank` 는 상장 3개월 미만 종목의 **면제 판정**(시총 50위 이내)에
  쓰인다. 시장 구분별 순위를 전체 순위로 오인하면 코스닥 종목이 50위 안에
  들어가 면제를 받는다. 전종목을 받았으므로 직접 계산하는 것이 안전하다.

경계
  엔진을 수정하지 않는다. `market_facts()` 계약의 CSV 만 만들며, 조문·판정을
  바꾸지 않는다.

사용
    # 1) 파일명에 날짜가 있으면(예: data_2252_20200529.csv) 자동 인식
    python analysis\\krx_facts_from_screen.py --screens data\\krx_screens ^
        --values out\\value_kis.csv --listing-dates data\\listing_dates.csv ^
        --out data\\market_facts --compare data\\snapshots

    # 2) 파일-날짜를 직접 짝지어 줄 때
    python analysis\\krx_facts_from_screen.py --pair 20200529=data\\a.csv ^
        --pair 20201130=data\\b.csv --values out\\value_kis.csv ...
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import os
import re
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "analysis"))

from analysis.build_pit_snapshots import review_pairs  # noqa: E402

#: 열 이름을 가정하지 않는다. 후보를 두고, 하나도 없으면 실제 열을 띄우며 중단.
COL_CODE = ("단축코드", "종목코드", "표준코드")
COL_CLOSE = ("종가", "현재가", "종가(원)")
COL_MCAP = ("시가총액", "시가총액(원)", "시총")
COL_NAME = ("한글 종목명", "한글종목명", "종목명", "한글 종목약명")
#: 보통주만 남긴다. 우선주·ETF 등이 섞이면 시총 순위가 틀어진다.
COL_KIND = ("주식종류", "증권구분")

ADV_WINDOW = 60


def selection_adv60(values: pd.DataFrame, asof: pd.Timestamp,
                    listing: pd.Series) -> pd.Series:
    """Selection-date ADV including that day's finalized trading value."""
    window = values.loc[values.index <= pd.Timestamp(asof)].tail(ADV_WINDOW)
    if len(window) < ADV_WINDOW:
        raise SystemExit(
            f"[FAIL] {asof.date()}: ADV cache has {len(window)} market days; "
            f"{ADV_WINDOW} required")
    out = {}
    for ticker in values.columns:
        if ticker not in listing.index:
            continue
        live = window.loc[window.index >= pd.Timestamp(listing.loc[ticker]), ticker]
        if live.empty:
            out[ticker] = np.nan
            continue
        live = pd.to_numeric(live, errors="coerce").fillna(0.0)
        if (live < 0).any():
            raise SystemExit(f"[FAIL] {ticker}: negative trading value in ADV cache")
        out[ticker] = float(live.mean())
    return pd.Series(out, dtype=float)


#: 종목코드 정규화 ― 문자를 **지우지 않고** 6자리 숫자만 채택한다.
#: 신형우선주 코드에는 문자가 들어간다(00680K 미래에셋대우2우B 등 18건).
#: 문자를 지우면 000680(LS네트웍스)과 충돌해 같은 종목으로 뭉개진다
#: (2026-08-02 실측: 3쌍 충돌). 우선주는 유니버스에 없으므로 제외가 맞다.
def _norm_code(s):
    c = s.astype(str).str.strip()
    return c.where(c.str.fullmatch(r"\d{6}"))


def _pick(df: pd.DataFrame, cands: tuple, what: str) -> str:
    c = next((x for x in cands if x in df.columns), None)
    if c is None:
        sys.exit(f"[FAIL] {what} 열을 찾지 못했다. 받은 열: "
                 f"{list(df.columns)[:20]} - 열 이름을 가정하지 않는다.")
    return c


def read_screen(path: str) -> pd.DataFrame:
    """KRX 전종목 시세 파일 1개 -> ticker · name · close · market_cap."""
    if not os.path.exists(path):
        sys.exit(f"[FAIL] 화면 파일이 없다: {path}")
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xls"):
        df = pd.read_excel(path, dtype=str)
    else:
        for enc in ("cp949", "utf-8-sig", "utf-8"):
            try:
                df = pd.read_csv(path, dtype=str, encoding=enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            sys.exit(f"[FAIL] {path} 인코딩을 판별하지 못했다")

    cc = _pick(df, COL_CODE, "종목코드")
    cl = _pick(df, COL_CLOSE, "종가")
    cm = _pick(df, COL_MCAP, "시가총액")
    nm = next((x for x in COL_NAME if x in df.columns), None)

    def num(s):
        return pd.to_numeric(
            s.astype(str).str.replace(",", "", regex=False)
             .str.replace(r"[^\d.\-]", "", regex=True), errors="coerce")

    out = pd.DataFrame({
        "ticker": _norm_code(df[cc]),
        "name": df[nm].astype(str).str.strip() if nm else "",
        "close": num(df[cl]),
        "market_cap": num(df[cm]),
    })
    # 보통주만. 우선주가 섞이면 같은 회사가 두 번 세어져 순위가 틀어진다.
    kk = next((x for x in COL_KIND if x in df.columns), None)
    if kk is not None:
        keep = df[kk].astype(str).str.contains("보통", na=False)
        if keep.any():
            out = out[keep.values]
    n_drop = int(out["ticker"].isna().sum())
    out = out[out["ticker"].notna() & out["market_cap"].notna()
              & (out["market_cap"] > 0)]
    if n_drop:
        print(f"  [정규화] {os.path.basename(path)}: 6자리 숫자가 아닌 코드 "
              f"{n_drop}건 제외(신형우선주 등)")
    if out["ticker"].duplicated().any():
        dup = sorted(out.loc[out["ticker"].duplicated(), "ticker"].unique())
        sys.exit(f"[FAIL] {os.path.basename(path)} 종목코드 중복 {dup[:5]} - "
                 "보통주 필터가 듣지 않았다. 파일을 확인할 것.")
    # 전종목 기준 시총 순위를 **직접** 계산한다(파일의 순위 열을 쓰지 않는다)
    out["mcap_rank"] = out["market_cap"].rank(ascending=False, method="min")
    return out.set_index("ticker").sort_index()


def _date_from_name(path: str) -> str | None:
    m = re.findall(r"(20\d{6})", os.path.basename(path))
    return m[-1] if m else None


def collect_screens(screens_dir: str | None, pairs: list) -> dict:
    """{YYYYMMDD: 경로}. 날짜를 추론하지 않는다 - 파일명 또는 --pair 로만."""
    got: dict = {}
    for p in pairs or []:
        if "=" not in p:
            sys.exit(f"[FAIL] --pair 형식은 YYYYMMDD=경로 다: {p}")
        d, f = p.split("=", 1)
        got[d.strip()] = f.strip()
    if screens_dir:
        for f in sorted(glob.glob(os.path.join(screens_dir, "*"))):
            if os.path.splitext(f)[1].lower() not in (".csv", ".xlsx", ".xls"):
                continue
            d = _date_from_name(f)
            if d is None:
                print(f"[주의] 파일명에서 날짜를 못 읽어 건너뜀: "
                      f"{os.path.basename(f)} - --pair 로 지정할 것")
                continue
            got.setdefault(d, f)
    if not got:
        sys.exit("[FAIL] 화면 파일이 없다. --screens 또는 --pair 를 줄 것")
    return got


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--screens", default=None,
                    help="전종목 시세 파일 폴더(파일명에 YYYYMMDD 포함)")
    ap.add_argument("--pair", action="append", default=[],
                    help="YYYYMMDD=경로 (반복 지정)")
    ap.add_argument("--values", required=True, help="일별 거래대금 패널(ADV60)")
    ap.add_argument("--listing-dates", required=True)
    ap.add_argument("--ledger", default=os.path.join(HERE, "data",
                                                     "verdict_ledger.csv"))
    ap.add_argument("--out", default=os.path.join(HERE, "data", "market_facts"))
    ap.add_argument("--start", type=int, default=2020)
    ap.add_argument("--end", type=int, default=2026)
    ap.add_argument("--compare", default=None, help="기존 스냅샷과 교차검증")
    a = ap.parse_args()

    led = pd.read_csv(a.ledger, dtype={"ticker": str})
    tickers = sorted({str(t).strip().zfill(6) for t in led["ticker"]})

    ld = pd.read_csv(a.listing_dates, dtype={"ticker": str})
    ld["ticker"] = ld["ticker"].str.strip().str.zfill(6)
    listing = pd.Series(pd.to_datetime(ld["listing_date"]).values,
                        index=ld["ticker"])
    miss = [t for t in tickers if t not in listing.index]
    if miss:
        sys.exit(f"[FAIL] 상장일 파일에 없는 종목: {miss}")

    val = pd.read_csv(a.values, index_col=0, parse_dates=True)
    val.columns = [str(c).zfill(6) for c in val.columns]
    val = val.apply(pd.to_numeric, errors="coerce").sort_index()
    if val.index.has_duplicates or val.columns.duplicated().any():
        sys.exit("[FAIL] 거래대금 패널에 중복 날짜·종목이 있다")

    td = pd.bdate_range(f"{a.start}-01-01", f"{a.end}-12-31")
    sels = {pd.Timestamp(s).strftime("%Y%m%d"): pd.Timestamp(s)
            for s, _ in review_pairs(td, a.start, a.end)}

    screens = collect_screens(a.screens, a.pair)
    unknown = sorted(set(screens) - set(sels))
    if unknown:
        sys.exit(f"[FAIL] 선정일이 아닌 날짜의 파일: {unknown}\n"
                 f"       선정일은 {sorted(sels)} 다. 날짜를 확인할 것.")
    lack = sorted(set(sels) - set(screens))
    if lack:
        print(f"[주의] 화면 파일이 없는 선정일 {len(lack)}개: {lack}")

    os.makedirs(a.out, exist_ok=True)
    made = []
    for tag in sorted(screens):
        asof = sels[tag]
        scr = read_screen(screens[tag])
        adv = selection_adv60(val, asof, listing)
        rows = []
        for t in tickers:
            if t not in scr.index:
                rows.append({"ticker": t, "listed": False})
                continue
            av = float(adv.get(t, np.nan)) if len(adv) else np.nan
            rows.append({
                "ticker": t, "listed": True,
                "close": float(scr.loc[t, "close"]),
                "market_cap": float(scr.loc[t, "market_cap"]),
                "mcap_rank": float(scr.loc[t, "mcap_rank"]),
                "adv60": av,
                "listed_days": float((asof - listing.loc[t]).days),
            })
        f = pd.DataFrame(rows).set_index("ticker")
        for c in ("close", "market_cap", "mcap_rank", "adv60", "listed_days"):
            if c not in f.columns:
                f[c] = np.nan
        f = f[["listed", "close", "market_cap", "mcap_rank", "adv60",
               "listed_days"]]
        source_hash = hashlib.sha256(
            open(screens[tag], "rb").read()).hexdigest()
        f["asof"] = asof.date()
        f["market_cap_source"] = "KRX all-stock screen download"
        f["source_file"] = os.path.basename(screens[tag])
        f["source_sha256"] = source_hash

        # ADV60 이 전멸하면 그 회차는 산출할 수 없다. 조용히 저장하면
        # screen() 이 전 종목을 'ADV60<10억'으로 탈락시켜 **그 회차 구성이
        # 비고**, 원인은 데이터 부재인데 규칙 탈락으로 보인다.
        n_listed = int(f["listed"].astype(bool).sum())
        n_adv_ok = int(f.loc[f["listed"].astype(bool), "adv60"].notna().sum())
        if n_listed and n_adv_ok == 0:
            sys.exit(
                f"[FAIL] {tag}: 상장 {n_listed}종목인데 ADV60 이 전부 결측이다.\n"
                f"       선정일 {asof.date()} 의 직전 60거래일이 거래대금 패널"
                f"(시작 {val.index.min().date()}) 밖이다.\n"
                "       ADV60 은 방법론 기초 필터(10억)의 입력이므로, 없는 채로 "
                "산출하면 그 회차 전 종목이 규칙 탈락으로 기록된다.\n"
                "       해결: 거래대금 패널을 선정일 이전 60거래일까지 확장할 것"
                " (KIS 수집 시작일을 앞당긴다).")
        if n_listed and n_adv_ok < n_listed:
            short = sorted(f.index[f["listed"].astype(bool)
                                   & f["adv60"].isna()])
            print(f"  [주의] {tag}: ADV60 결측 {len(short)}종목 {short[:6]}"
                  f"{'...' if len(short) > 6 else ''} - 해당 종목은 "
                  "'자료불충분'으로 탈락한다(fail-closed).")

        dst = os.path.join(a.out, f"facts_{tag}.csv")
        f.to_csv(dst, encoding="utf-8-sig")
        n_l = int(f["listed"].astype(bool).sum())
        n_adv = int(f["adv60"].notna().sum())
        print(f"[{tag}] 전종목 {len(scr)}건 · 상장 {n_l}/{len(f)} · "
              f"ADV60 {n_adv}종목 -> {os.path.basename(dst)}")
        made.append(tag)

    if not made:
        sys.exit("[FAIL] 산출된 팩트가 없다")

    print(f"\n[저장] {a.out}/facts_*.csv ({len(made)}회차)")
    print("[순위] mcap_rank 는 파일의 순위 열이 아니라 전종목 시총으로 다시 "
          "계산했다 - 시장 구분별 순위를 전체 순위로 오인하면 상장 3개월 면제"
          "(시총 50위) 판정이 틀어진다.")
    print("[ADV] 화면 파일의 당일 거래대금이 아니라 별도 패널의 직전 60거래일 "
          "평균이다.")
    print("[출처] KRX 화면 다운로드 - KDM 접근 제한 안내문이 허용한 경로다. "
          "받은 원본 파일을 함께 보존해 계보를 남길 것.")

    if a.compare:
        from analysis.fetch_market_facts import cross_check
        return cross_check(a.out, a.compare, a.ledger)
    print("[주의] 교차검증 없이 저장했다. 스냅샷 재생성 전에 --compare 로 "
          "기존 float_mcap 과 대조할 것")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
