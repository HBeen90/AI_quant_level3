# -*- coding: utf-8 -*-
"""
candidate_discovery.py ― 전시장 후보발굴 계층 (P1)
====================================================
`docs/candidate_discovery_contract.md` (v2) 의 구현이다. 고정 33종목 후보
유니버스의 **선택편향**을 해소한다 ― "33종목은 어떻게 골랐나, 밖에 자격자가
있으면 어떻게 아나"에 답할 수 있게 만든다.

계층 경계 (계약 §1)
  이 모듈은 **열거와 점수화까지만** 한다. 매출 노출도 추정·편입 판정은
  파트2 소관이며 여기서 하지 않는다. `hbm_evidence.py` 는 확장하지 않는다
  (그쪽은 지정 종목 카드 생성기다).

두 실행 모드 (계약 §5-1)
  운영 : --discover  네트워크 조회 → 심사시점 CSV 동결
  재현 : --load      동결 CSV만 소비. **네트워크 재조회 금지.**
         동결 파일이 없는 심사시점은 "후보발굴 미실시"로 보고한다.
         조용히 생략하면 후보군 고정 상태가 성과에 숨는다.

과거 구간에 대한 정직한 한계 (계약 §5-3)
  전 상장종목의 과거 시점 사업보고서를 전수 재조회하는 것은 현실적으로
  불가능하다. 따라서 v1 은 **현 시점 1회 스크린**으로 "지금 기준 33종목 밖에
  자격 후보가 있는가"를 확인하는 데 목적이 있다. 과거 심사시점은 여전히
  후보군 고정 상태이며, 그 사실과 편향 방향(상향 가능성)을 백테스트 보고서에
  고지해야 한다. 이 모듈은 그 고지를 대신하지 않는다.

사용
    # 운영 - 전시장 발굴 (pykrx + DART 필요)
    python analysis/candidate_discovery.py --discover --selection-date 2026-05-29

    # 재현 - 동결 CSV 검증·보고 (네트워크 불요)
    python analysis/candidate_discovery.py --load
    python analysis/candidate_discovery.py --load --coverage
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import os
import re
import sys
import time

import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

OUT_DIR = os.path.join(HERE, "data", "candidates")
LEDGER = os.path.join(HERE, "data", "verdict_ledger.csv")

SCHEMA = ["selection_date", "ticker", "name", "listing_date", "listed_asof",
          "source_rcp_no", "disclosed_at", "keyword_version",
          "hbm_hits", "process_hits", "discovery_reason", "review_status"]

STATUSES = {"NEW", "SCREENED_OUT", "PART2_PENDING", "LEDGER_ADDED", "REJECTED"}
FROZEN_FORBIDDEN = {"NEW"}                      # 계약 §3 동결 규칙

#: 키워드 사전 v1 ― **팀 승인 대기 기안** (계약 §8-1).
#: 사전 변경은 소급 적용하지 않고 버전 증가로만 한다(계약 §5-4).
KEYWORDS = {
    "kw_v1": {
        "hbm": ("HBM", "고대역폭", "High Bandwidth", "HBM3", "HBM4"),
        "process": ("TSV", "TC본딩", "TC 본딩", "thermal compression",
                    "하이브리드 본딩", "MR-MUF", "실리콘관통전극"),
    }
}

#: 기초 스크린 임계 ― **팀 승인 대기 기안** (계약 §8-1).
THRESHOLDS = {"min_mcap_krw": 350e8, "min_adv60_krw": 10e8,
              "min_listed_days": 90, "min_hbm_hits": 5}


def _sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest().upper()


# ----------------------------------------------------------------------
# 재현 경로 ― strict 로더와 검증 게이트 (계약 §7). 네트워크 불요.
# ----------------------------------------------------------------------
def load_frozen(path: str) -> pd.DataFrame:
    """게이트 1·4: 스키마·dtype·상태값·중복·NEW 잔존 검사. 위반 시 예외."""
    df = pd.read_csv(path, dtype={"ticker": str, "source_rcp_no": str})
    miss = [c for c in SCHEMA if c not in df.columns]
    if miss:
        raise ValueError(f"{os.path.basename(path)}: 스키마 누락 {miss}")
    if list(df.columns)[:len(SCHEMA)] != SCHEMA:
        raise ValueError(f"{os.path.basename(path)}: 컬럼 순서가 계약과 다름")
    df["ticker"] = df["ticker"].astype(str).str.strip().str.zfill(6)
    if (df["ticker"].str.len() != 6).any():
        raise ValueError("6자리 종목코드 위반")
    bad = set(df["review_status"].dropna()) - STATUSES
    if bad:
        raise ValueError(f"허용하지 않는 review_status: {sorted(bad)}")
    left = set(df["review_status"]) & FROZEN_FORBIDDEN
    if left:                                    # 계약 §3 동결 규칙
        raise ValueError(
            f"동결 파일에 실행 중 상태가 남아 있음: {sorted(left)} - "
            "NEW 는 동결 전에 전이가 끝나야 한다(fail-closed)")
    if df.duplicated(["selection_date", "ticker"]).any():
        d = df[df.duplicated(["selection_date", "ticker"], keep=False)]
        raise ValueError(f"(selection_date, ticker) 중복: {len(d)}행")
    return df


def validate_pit(df: pd.DataFrame) -> list:
    """게이트 2·3: 공개근거 시점 · 상장 정합. 위반 목록을 돌려준다."""
    errs = []
    sd = pd.to_datetime(df["selection_date"], errors="coerce")
    da = pd.to_datetime(df["disclosed_at"], errors="coerce")
    ld = pd.to_datetime(df["listing_date"], errors="coerce")
    late = df[da.notna() & (da > sd)]
    if len(late):
        errs.append(f"[게이트2] disclosed_at > selection_date: {len(late)}행 "
                    f"{sorted(late['ticker'].head(5))}")
    la = df["listed_asof"].astype(str).str.strip().str.lower()
    if not la.isin({"true", "false"}).all():
        errs.append("[게이트3] listed_asof 는 true/false 만 허용")
    else:
        expect = (ld.notna() & (ld <= sd))
        mismatch = df[la.eq("true") != expect]
        if len(mismatch):
            errs.append(f"[게이트3] listed_asof 가 listing_date 와 모순: "
                        f"{len(mismatch)}행 {sorted(mismatch['ticker'].head(5))}")
    return errs


def coverage_report(out_dir: str = OUT_DIR,
                    selection_dates: list | None = None) -> pd.DataFrame:
    """게이트 6: 심사시점별 후보 수·상태 분포. **미실시 시점을 명시**한다."""
    files = sorted(glob.glob(os.path.join(out_dir, "candidate_discovery_*.csv")))
    got = {}
    for f in files:
        m = re.search(r"candidate_discovery_(\d{4}-\d{2}-\d{2})\.csv$", f)
        if m:
            got[m.group(1)] = f
    dates = selection_dates or sorted(got)
    rows = []
    for d in dates:
        if d not in got:
            rows.append({"selection_date": d, "상태": "후보발굴 미실시",
                         "후보수": 0, "PART2_PENDING": 0, "SCREENED_OUT": 0,
                         "sha256": ""})
            continue
        df = load_frozen(got[d])
        vc = df["review_status"].value_counts()
        rows.append({"selection_date": d, "상태": "동결",
                     "후보수": len(df),
                     "PART2_PENDING": int(vc.get("PART2_PENDING", 0)),
                     "SCREENED_OUT": int(vc.get("SCREENED_OUT", 0)),
                     "sha256": _sha256(got[d])[:16]})
    return pd.DataFrame(rows)


def compare_to_ledger(df: pd.DataFrame) -> pd.DataFrame:
    """발굴 후보 중 판정원장 유니버스(33종목) 밖의 종목 ― P1 의 핵심 산출.

    이 표가 비어 있으면 "현 시점 기준 후보군 고정의 누락은 0건"이라 말할 수
    있다. 비어 있지 않으면 그 자체가 발견이며 파트2 판정 대상이다.
    """
    led = pd.read_csv(LEDGER, dtype={"ticker": str}, usecols=["ticker"])
    known = set(led["ticker"].str.strip().str.zfill(6))
    cand = df[df["review_status"].eq("PART2_PENDING")]
    new = cand[~cand["ticker"].isin(known)]
    return new[["selection_date", "ticker", "name", "hbm_hits",
                "process_hits", "discovery_reason"]].reset_index(drop=True)


# ----------------------------------------------------------------------
# 운영 경로 ― 전시장 발굴 (네트워크 필요)
# ----------------------------------------------------------------------
def discover(selection_date: str, kw_version: str = "kw_v1",
             sleep: float = 0.3, limit: int | None = None) -> pd.DataFrame:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    key = os.environ.get("DART_API_KEY")
    if not key:
        sys.exit("[FAIL] DART_API_KEY 가 없다(.env). 중단한다.")
    try:
        from pykrx import stock
    except ImportError:
        sys.exit("[FAIL] pykrx 미설치 - pip install pykrx")

    sd = pd.Timestamp(selection_date)
    ymd = sd.strftime("%Y%m%d")
    print(f"[발굴] 심사시점 {selection_date} · 사전 {kw_version}")

    tickers = sorted(set(stock.get_market_ticker_list(ymd, market="ALL")))
    print(f"  전 상장종목 {len(tickers)}종목 열거")

    try:
        cap = stock.get_market_cap(ymd, market="ALL")
    except Exception as e:
        sys.exit(f"[FAIL] 시가총액 조회 실패({type(e).__name__}) - "
                 "KRX 로그인이 필요할 수 있다(KRX_ID/KRX_PW). "
                 "시총 기준을 빼고 스크린하면 결과가 달라지므로 중단한다.")

    adv_from = (sd - pd.tseries.offsets.BDay(60)).strftime("%Y%m%d")
    rows = []
    todo = tickers[:limit] if limit else tickers
    for i, t in enumerate(todo, 1):
        name = stock.get_market_ticker_name(t)
        mcap = float(cap["시가총액"].get(t, 0.0))
        row = {"selection_date": selection_date, "ticker": t, "name": name,
               "listing_date": "", "listed_asof": "true",
               "source_rcp_no": "", "disclosed_at": "",
               "keyword_version": kw_version, "hbm_hits": 0, "process_hits": 0,
               "discovery_reason": "", "review_status": "NEW"}
        if mcap < THRESHOLDS["min_mcap_krw"]:
            row.update(discovery_reason=f"시총 {mcap/1e8:.0f}억 < 350억",
                       review_status="SCREENED_OUT")
            rows.append(row)
            continue
        try:
            o = stock.get_market_ohlcv(adv_from, ymd, t)
            adv = float(o["거래대금"].tail(60).mean()) if len(o) else 0.0
            first = pd.Timestamp(o.index[0]) if len(o) else sd
        except Exception:
            adv, first = 0.0, sd
        row["listing_date"] = first.strftime("%Y-%m-%d")
        if adv < THRESHOLDS["min_adv60_krw"]:
            row.update(discovery_reason=f"ADV60 {adv/1e8:.1f}억 < 10억",
                       review_status="SCREENED_OUT")
            rows.append(row)
            continue
        rows.append(row)                       # 키워드 점수화 대상
        if i % 200 == 0:
            print(f"  ... {i}/{len(todo)} 기초 스크린")

    df = pd.DataFrame(rows)
    survivors = df[df["review_status"].eq("NEW")]
    print(f"  기초 스크린 통과 {len(survivors)}종목 → DART 키워드 점수화")

    from hbm_evidence import _clean, _make_dart, annual_report
    dart = _make_dart(key)
    kw = KEYWORDS[kw_version]
    for j, (idx, r) in enumerate(survivors.iterrows(), 1):
        fy = sd.year - 1                       # 선정기준일까지 공개된 최신 사업연도
        got = annual_report(dart, r["ticker"], fy)
        if not got:
            df.at[idx, "discovery_reason"] = f"FY{fy} 사업보고서 없음"
            df.at[idx, "review_status"] = "SCREENED_OUT"
            continue
        nm, rcp, dt = got
        dt_iso = f"{dt[:4]}-{dt[4:6]}-{dt[6:8]}" if len(str(dt)) == 8 else str(dt)
        if pd.Timestamp(dt_iso) > sd:          # 계약 §5-2 공개근거 시점 필터
            df.at[idx, "discovery_reason"] = "선정기준일 이후 공시"
            df.at[idx, "review_status"] = "SCREENED_OUT"
            continue
        try:
            txt = _clean(dart.document(rcp))
        except Exception:
            df.at[idx, "discovery_reason"] = "본문 조회 실패"
            df.at[idx, "review_status"] = "SCREENED_OUT"
            continue
        h = sum(txt.count(k) for k in kw["hbm"])
        p = sum(txt.count(k) for k in kw["process"])
        df.at[idx, "source_rcp_no"] = rcp
        df.at[idx, "disclosed_at"] = dt_iso
        df.at[idx, "hbm_hits"] = h
        df.at[idx, "process_hits"] = p
        if h >= THRESHOLDS["min_hbm_hits"]:
            df.at[idx, "discovery_reason"] = f"hbm_hits>={THRESHOLDS['min_hbm_hits']}"
            df.at[idx, "review_status"] = "PART2_PENDING"
        else:
            df.at[idx, "discovery_reason"] = "임계 미달"
            df.at[idx, "review_status"] = "SCREENED_OUT"
        if j % 20 == 0:
            print(f"  ... {j}/{len(survivors)} 점수화")
        time.sleep(sleep)

    if df["review_status"].isin(FROZEN_FORBIDDEN).any():
        sys.exit("[FAIL] NEW 상태가 남았다 - 동결 불가(계약 §3)")
    return df[SCHEMA]


def freeze(df: pd.DataFrame, selection_date: str) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, f"candidate_discovery_{selection_date}.csv")
    if os.path.exists(p):
        sys.exit(f"[FAIL] 이미 동결된 파일이 있다: {p} - 재실행은 새 파일로만"
                 "(계약 §7-5)")
    df.to_csv(p, index=False, encoding="utf-8-sig", lineterminator="\n")
    print(f"[동결] {p}\n[sha256] {_sha256(p)}")
    return p


# ----------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true", help="운영: 전시장 발굴")
    ap.add_argument("--load", action="store_true", help="재현: 동결 CSV 검증")
    ap.add_argument("--coverage", action="store_true", help="심사시점 커버리지 표")
    ap.add_argument("--selection-date", default=None)
    ap.add_argument("--keyword-version", default="kw_v1")
    ap.add_argument("--limit", type=int, default=None, help="시험용 종목 수 제한")
    a = ap.parse_args()
    if not (a.discover or a.load or a.coverage):
        ap.error("--discover / --load / --coverage 중 하나를 지정할 것")

    if a.discover:
        if not a.selection_date:
            ap.error("--discover 에는 --selection-date 가 필요하다")
        df = discover(a.selection_date, a.keyword_version, limit=a.limit)
        freeze(df, a.selection_date)
        vc = df["review_status"].value_counts()
        print(f"\n[결과] {vc.to_dict()}")

    if a.load or a.coverage:
        files = sorted(glob.glob(os.path.join(OUT_DIR,
                                              "candidate_discovery_*.csv")))
        if not files:
            print("[보고] 동결된 후보발굴 파일이 없다 - 전 심사시점 "
                  "'후보발굴 미실시'. 후보군은 v0(33종목) 고정 상태이며, "
                  "선택편향의 방향(상향 가능성)을 백테스트 보고서에 고지할 것.")
            return 0
        allpass = True
        frames = []
        for f in files:
            df = load_frozen(f)
            errs = validate_pit(df)
            frames.append(df)
            print(f"[{os.path.basename(f)}] {len(df)}행 · "
                  + ("검증 통과" if not errs else "검증 실패"))
            for e in errs:
                allpass = False
                print("   " + e)
        if a.coverage:
            print("\n[커버리지]\n", coverage_report().to_string(index=False))
        both = pd.concat(frames, ignore_index=True)
        new = compare_to_ledger(both)
        print(f"\n[판정원장 밖 신규 후보] {len(new)}건")
        if len(new):
            print(new.to_string(index=False))
            print("\n-> 파트2 판정 대상. 후보군 고정의 누락이 실증됐다.")
        else:
            print("-> 현 시점 기준 후보군 고정으로 인한 누락 0건.")
        return 0 if allpass else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
