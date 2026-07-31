# -*- coding: utf-8 -*-
"""
build_pit_snapshots.py - 시점별(PIT) 심사 스냅샷 생성기
========================================================
백테스트의 유일한 병목은 "그 시점에 규칙이 무엇을 골랐을 것인가"이다.
지금 레포에는 2026-07-23 확정 구성 1회분만 있고, 그래서 회전율·MDD·상관계수를
보고할 수 없다. 유지 임계값 27/67은 현행 운영값이며, 이 스냅샷은 향후
개정 절차(7.3)의 사후 점검 근거를 만드는 데 사용한다.

이 스크립트는 그 스냅샷을 **look-ahead 없이** 13회분 만든다.

두 갈래로 나눈 이유
-------------------
  (a) 기계가 할 수 있는 것 : 시가총액 · 거래대금 · 상장경과 · 가격 (pykrx)
  (b) 사람이 판단해야 하는 것 : HBM노출도 · 메모리향비중 · HBM양산 · 공정확인
      -> hbm_evidence.py 가 근거를 차려주고 사람이 숫자를 적은 **판정 원장**

이 스크립트는 (b)를 원장에서 읽어 (a)와 결합한다. 절대 (b)를 지어내지 않는다.

★ PIT 규율의 핵심 (as_of_ledger)
--------------------------------
판정값은 사업보고서에서 나오고, 사업보고서는 **회계연도 종료 후 3개월쯤 뒤에
공시된다.** 2020년 6월 심사에 2020년 사업보고서(2021년 3월 공시)를 쓰면
미래를 훔쳐본 것이고, 백테스트 성과가 통째로 무효가 된다.
그래서 원장의 각 행은 `disclosed_at`(공시일)을 갖고, 심사 시점 d 에는
`disclosed_at <= d` 인 행 중 가장 최신만 쓴다. 이 규칙 하나가
"백테스트가 믿을 만한가"를 결정한다.

판정 원장 계약 : data/verdict_ledger.csv
----------------------------------------
    ticker            6자리 문자열
    name              종목명
    disclosed_at      이 판정의 근거 자료가 공개된 날 (YYYY-MM-DD) ★PIT 기준
    fiscal_year       근거 사업연도 (예: 2019)
    sector            '메모리제조' 등 유형
    hbm_massproduction  bool  (규칙 0)
    hbm_exposure      0~1     (규칙 A) HBM 밸류체인 매출 / 전사매출
    mem_ratio         0~1     (규칙 C①) 메모리 반도체향 매출 / 전사매출
    process_confirmed bool    (규칙 C②) HBM 고유공정 귀속 문서 확인
    committee_ok      bool    (규칙 C③) 위원회 확인
    free_float        0~1     유동비율 (DART 최대주주+자기주식 차감 등)
    source            근거 출처 문자열 (계보)
    audit_opinion     감사의견 - 비적정이면 하드 탈락
    admin_issue       bool    그 시점 관리종목 여부 - 하드 탈락
    reviewer          판정자
    judgment_status   FINAL (확정 원장) / DRAFT (초안)

사용
----
    python analysis/build_pit_snapshots.py --ledger data/verdict_ledger.csv --out data/snapshots --start 2020 --end 2026
    python analysis/build_pit_snapshots.py --ledger ... --template-only
        -> 원장 템플릿만 생성(어떤 종목·어떤 시점 행이 필요한지 표로 뽑아준다)
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from analysis.index_calendar import as_of_today, pair_selection_to_rebalance  # noqa: E402

LEDGER_COLS = ["ticker", "name", "disclosed_at", "fiscal_year", "sector",
               "hbm_massproduction", "hbm_exposure", "mem_ratio",
               "process_confirmed", "committee_ok", "free_float", "source",
               "audit_opinion", "admin_issue", "reviewer", "judgment_status"]

# 기초 유니버스 필터 (README '기초 유니버스 필터')
MIN_MCAP = 350e8          # 시가총액 350억원
MIN_ADV = 10e8            # 직전 60영업일 평균 거래대금 10억원
ADV_DAYS = 60
MIN_LISTED_DAYS = 90      # 상장 후 3개월
MIN_FREE_FLOAT = 0.10     # 유동비율 10%
TOP_MCAP_EXEMPT = 50      # 시총 50위 이내는 상장기간 예외

_TRUE = {"true", "1", "y", "yes", "참"}
_FALSE = {"false", "0", "n", "no", "거짓"}


def review_pairs(trading_days: pd.DatetimeIndex, start: int, end: int,
                 asof: pd.Timestamp | None = None) -> list:
    """분석 기준일에 이미 도래한 선정일·시행일 짝만 반환한다.

    연도 `end`의 12월까지 달력을 미리 만들더라도 미래 정기변경을 조회하지
    않는다. 재현 시점은 INDEX_ASOF(기본: 오늘)로 한 곳에서 통제한다.
    """
    cutoff = pd.Timestamp(asof) if asof is not None else as_of_today()
    return [(s, r) for s, r in pair_selection_to_rebalance(trading_days)
            if start <= r.year <= end and r <= cutoff]


def _strict_bool(s: pd.Series, name: str) -> pd.Series:
    def parse(v):
        if isinstance(v, (bool, np.bool_)):
            return bool(v)
        key = str(v).strip().lower()
        if key in _TRUE:
            return True
        if key in _FALSE:
            return False
        raise ValueError(f"{name} 허용하지 않는 Boolean 값: {v!r}")

    try:
        return s.map(parse).astype(bool)
    except ValueError as exc:
        sys.exit(f"[FAIL] {exc}")


# ----------------------------------------------------------------------
# PIT 규율
# ----------------------------------------------------------------------
def as_of_ledger(ledger: pd.DataFrame, asof: pd.Timestamp) -> pd.DataFrame:
    """심사 시점 asof 에 '공개돼 있던' 최신 판정만 남긴다 - look-ahead 차단.

    이 함수가 백테스트 신뢰성의 급소다. disclosed_at > asof 인 행은 그 시점에
    존재하지 않던 정보이므로 무조건 버린다. 종목별로 남은 행 중
    disclosed_at 이 가장 늦은 것 하나만 채택한다(동률이면 fiscal_year 최신).
    """
    d = ledger.copy()
    d["disclosed_at"] = pd.to_datetime(d["disclosed_at"])
    d = d[d["disclosed_at"] <= pd.Timestamp(asof)]
    if d.empty:
        return d
    d = d.sort_values(["ticker", "disclosed_at", "fiscal_year"])
    return d.groupby("ticker", as_index=False).tail(1).sort_values("ticker")


def load_ledger(path: str, allow_provisional: bool = False) -> pd.DataFrame:
    if not os.path.exists(path):
        sys.exit(f"[FAIL] 판정 원장 없음: {path} - --template-only 로 템플릿 생성")
    d = pd.read_csv(path, dtype={"ticker": str, "코드": str})
    d = d.rename(columns={"코드": "ticker", "종목명": "name"})
    miss = [c for c in LEDGER_COLS if c not in d.columns]
    if miss:
        sys.exit(f"[FAIL] 원장 컬럼 누락: {miss}")
    d["ticker"] = d["ticker"].astype(str).str.strip().str.zfill(6)
    if (d["ticker"].str.len() != 6).any() or d["ticker"].eq("000nan").any():
        sys.exit("[FAIL] ticker는 6자리 문자열이어야 합니다")
    d["disclosed_at"] = pd.to_datetime(d["disclosed_at"], errors="coerce")
    if d["disclosed_at"].isna().any():
        sys.exit("[FAIL] disclosed_at 날짜 파싱 실패")
    if d["source"].isna().any() or d["source"].astype(str).str.strip().eq("").any():
        sys.exit("[FAIL] source 계보 값 누락")
    if not allow_provisional:
        status = d["judgment_status"].astype(str).str.strip().str.upper()
        if status.ne("FINAL").any():
            sys.exit("[FAIL] 확정 스냅샷은 judgment_status=FINAL만 허용합니다")
        if d["source"].astype(str).str.contains("TODO", case=False, na=True).any():
            sys.exit("[FAIL] 확정 스냅샷 source에 TODO가 남아 있습니다")
        if d["reviewer"].isna().any() or d["reviewer"].astype(str).str.strip().eq("").any():
            sys.exit("[FAIL] 확정 스냅샷 reviewer가 비어 있습니다")
        if (d["audit_opinion"].isna().any()
                or d["audit_opinion"].astype(str).str.strip().eq("").any()):
            sys.exit("[FAIL] 확정 스냅샷 audit_opinion이 비어 있습니다")
    for c in ("hbm_massproduction", "process_confirmed", "committee_ok",
              "admin_issue"):
        d[c] = _strict_bool(d[c], c)
    for c in ("hbm_exposure", "mem_ratio", "free_float"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    bad = d.loc[d["free_float"].isna(), "ticker"].tolist()
    if bad:
        sys.exit(f"[FAIL] free_float 결측: {bad} - 유동시총을 만들 수 없다")
    for c in ("hbm_exposure", "mem_ratio", "free_float"):
        bad = d.loc[d[c].notna() & ~d[c].between(0.0, 1.0), "ticker"].tolist()
        if bad:
            sys.exit(f"[FAIL] {c} 0~1 범위 위반: {bad}")
    return d


# ----------------------------------------------------------------------
# 기계가 채우는 부분 (pykrx)
# ----------------------------------------------------------------------
def market_facts(tickers: list, asof: pd.Timestamp) -> pd.DataFrame:
    """선정일 시점의 시총·거래대금·상장경과일. PIT - 미래 데이터를 안 본다."""
    try:
        from pykrx import stock
    except ImportError:
        sys.exit("[FAIL] pykrx 미설치 - pip install pykrx")
    ymd = pd.Timestamp(asof).strftime("%Y%m%d")
    cap = stock.get_market_cap(ymd, market="ALL")
    if cap is None or cap.empty:
        sys.exit(f"[FAIL] {ymd} 시가총액 미수신 (휴장일 또는 KRX 접근 실패)")
    # KRX 가 JSON 대신 HTML(로그인/차단 안내)을 돌려주면 pykrx 는 예외를
    # 삼키고 빈·부분 결과를 준다. 그 결과를 그대로 쓰면 시총 0 인 종목이
    # 대량 발생하고, 기초 필터가 그것을 "소형주 탈락"으로 처리해
    # **조용히 오염된 스냅샷**이 만들어진다(2026-07-31 실제 발생).
    bad_cap = (~np.isfinite(cap["시가총액"].astype(float))) \
        | (cap["시가총액"].astype(float) <= 0)
    if float(bad_cap.mean()) > 0.05:
        sys.exit(f"[FAIL] {ymd} 시가총액이 비정상입니다 - 전체 {len(cap)}종목 중 "
                 f"{int(bad_cap.sum())}종목이 결측·0. KRX 접근 상태를 확인하십시오"
                 " (조회 실패를 소형주 탈락으로 오인하면 스냅샷이 오염됩니다).")
    cap.index = [str(i).zfill(6) for i in cap.index]
    cap["시총순위"] = cap["시가총액"].rank(ascending=False, method="min")

    adv_start = (pd.Timestamp(asof) - pd.Timedelta(days=ADV_DAYS * 2 + 40)) \
        .strftime("%Y%m%d")
    rows = []
    for t in tickers:
        if t not in cap.index:
            rows.append({"ticker": t, "listed": False})
            continue
        # ADV60 원천: 시가총액 일별 시계열의 '거래대금' 컬럼.
        # pykrx 1.2.x(2026 KRX 로그인 전환 재작성)부터 get_market_ohlcv
        # 단일종목 기간 조회에 '거래대금'이 없어졌다. get_market_cap 기간
        # 조회는 구·신 버전 모두 (시가총액·거래량·거래대금·상장주식수)를
        # 반환하므로 이를 단일 원천으로 쓴다. 값의 정의(일별 거래대금)는
        # 기존과 동일하며 원천 통계(KRX)도 같다.
        rng = stock.get_market_cap(adv_start, ymd, t)
        if rng is None or rng.empty:
            # 이 지점에 도달했다는 것은 t 가 위 전체 시장 조회에 **존재**한다는
            # 뜻이다(없으면 앞에서 listed=False 로 빠졌다). 즉 상장 종목인데
            # 시계열만 비었다 - 미상장이 아니라 조회 실패다. 이를 미상장으로
            # 기록하면 실패가 탈락으로 둔갑한다.
            sys.exit(f"[FAIL] {t} 시가총액 시계열 조회 실패 - 해당 종목은 {ymd} "
                     "전체 시장 조회에 존재하므로 미상장이 아닙니다. KRX 접근 "
                     "상태를 확인하고 재실행하십시오(부분 산출물은 폐기).")
        if "거래대금" not in rng.columns:
            sys.exit(f"[FAIL] {t} 시가총액 시계열에 거래대금 컬럼 없음 - "
                     "pykrx 버전 확인 (pip install -U pykrx)")
        adv = float(rng["거래대금"].tail(ADV_DAYS).mean())
        first = stock.get_market_ohlcv("19950101", ymd, t, freq="m")
        if first is None or len(first) == 0:
            sys.exit(f"[FAIL] {t} 상장 이력 조회 실패 - 상장경과일을 확인할 수 "
                     "없습니다. NaN 으로 두면 상장 3개월 요건이 조용히 통과됩니다.")
        listed_days = (pd.Timestamp(asof) - pd.Timestamp(first.index[0])).days
        rows.append({"ticker": t, "listed": True,
                     "close": float(cap.loc[t, "종가"]),
                     "market_cap": float(cap.loc[t, "시가총액"]),
                     "mcap_rank": float(cap.loc[t, "시총순위"]),
                     "adv60": adv, "listed_days": listed_days})
    out = pd.DataFrame(rows).set_index("ticker")
    live = out[out["listed"].astype(bool)] if "listed" in out.columns else out
    if len(live):
        mc = pd.to_numeric(live.get("market_cap"), errors="coerce")
        bad = mc.isna() | (mc <= 0)
        if bad.any():
            sys.exit(f"[FAIL] 상장 종목 중 시총 결측·비양수: "
                     f"{sorted(live.index[bad])} - 조회 실패 가능성. 중단합니다.")
    return out


def screen(facts: pd.DataFrame, led: pd.DataFrame) -> pd.DataFrame:
    """기초 유니버스 필터 -> eligible(bool) + 탈락사유. 하드 탈락은 여기서 확정."""
    j = led.set_index("ticker").join(facts, how="left")
    reasons = []
    for t, r in j.iterrows():
        why = []
        if not bool(r.get("listed", False)):
            why.append("비상장/미수신")
        else:
            if r["market_cap"] < MIN_MCAP:
                why.append(f"시총<{MIN_MCAP/1e8:.0f}억")
            if r["adv60"] < MIN_ADV:
                why.append(f"ADV60<{MIN_ADV/1e8:.0f}억")
            if (r["listed_days"] < MIN_LISTED_DAYS
                    and r["mcap_rank"] > TOP_MCAP_EXEMPT):
                why.append("상장<3개월")
        if r["free_float"] < MIN_FREE_FLOAT:
            why.append("유동비율<10%")
        if bool(r.get("admin_issue", False)):
            why.append("관리종목")
        opinion = str(r.get("audit_opinion", "적정")).strip().lower()
        if opinion not in {"적정", "unqualified", "unmodified"}:
            why.append("감사의견 비적정")
        reasons.append(";".join(why))
    j["탈락사유"] = reasons
    j["eligible"] = j["탈락사유"] == ""
    return j.reset_index()


def to_snapshot(j: pd.DataFrame) -> pd.DataFrame:
    """screen 결과 -> rebalance.validate_snapshot 계약 + 데이터 계약 v2 계보."""
    is_anchor = j["sector"].eq("메모리제조") & j["hbm_massproduction"]
    is_core = ~is_anchor & (j["hbm_exposure"].fillna(0) >= 0.30)
    grp = np.where(is_anchor, "anchor",
                   np.where(is_core, "core",
                            np.where(j["mem_ratio"].fillna(0) >= 0.70,
                                     "satellite", "core")))
    # group 은 '어느 규칙으로 심사받는가'의 라벨일 뿐이다. 실제 편입 판정은
    # select_v2 가 임계값(신규 30/70 · 유지 hold)으로 수행한다 - 여기서
    # 편입 여부를 미리 정하지 않는다(귀속: 판정은 selection/rebalance 소관).
    out = pd.DataFrame({
        "ticker": j["ticker"], "name": j["name"], "group": grp,
        "exposure": j["hbm_exposure"], "mem_ratio": j["mem_ratio"],
        "float_mcap": j["market_cap"] * j["free_float"],
        "eligible": j["eligible"].astype(bool),
        # 위성 하드요건(규칙 C 요건②③)은 eligible 에 접어 넣는다.
        # 주의: 미충족은 **판정 결과**이지 판정 미수행이 아니다 -
        # 해당 행의 judgment_status 는 FINAL 이다. 사유 문자열이
        # "미확인"이면 감사자가 "판정을 안 했다"로 오독한다(실제 발생).
        "탈락사유": j["탈락사유"],
    })
    sat = out["group"].eq("satellite")
    hard = sat & ~(j["process_confirmed"].fillna(False)
                   & j["committee_ok"].fillna(False))
    out.loc[hard, "eligible"] = False
    out.loc[hard, "탈락사유"] = (out.loc[hard, "탈락사유"] + ";규칙 C 요건②③ 미충족") \
        .str.strip(";")
    out["float_mcap"] = out["float_mcap"].fillna(0.0)
    out.loc[~out["eligible"] & (out["float_mcap"] <= 0), "float_mcap"] = 1.0
    return out.sort_values("ticker").reset_index(drop=True)


# ----------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--out", default="data/snapshots")
    ap.add_argument("--start", type=int, default=2020)
    ap.add_argument("--end", type=int, default=2026)
    ap.add_argument("--template-only", action="store_true",
                    help="필요한 (종목 x 심사시점) 원장 행 목록만 출력")
    ap.add_argument("--allow-provisional", action="store_true",
                    help="DRAFT/TODO 원장을 탐색용으로만 허용")
    ap.add_argument("--code-commit", default="unknown")
    a = ap.parse_args()

    td = pd.bdate_range(f"{a.start}-01-01", f"{a.end}-12-31")   # 근사 캘린더
    pairs = review_pairs(td, a.start, a.end)

    if a.template_only:
        led = (load_ledger(a.ledger, a.allow_provisional)
               if os.path.exists(a.ledger) else None)
        print("필요한 심사 시점(선정일 -> 시행일):")
        for s, r in pairs:
            n = len(as_of_ledger(led, s)) if led is not None else 0
            print(f"  {s.date()} -> {r.date()}   원장에서 사용 가능한 종목 {n}개")
        print(f"\n총 {len(pairs)}회. 각 시점마다 disclosed_at <= 선정일 인 판정이"
              " 종목별로 최소 1행 있어야 한다.")
        print("행이 0개인 시점은 그 시점 사업보고서 판정이 원장에 없다는 뜻이다 -"
              " hbm_evidence.py 로 근거를 뽑아 채울 것.")
        return 0

    led = load_ledger(a.ledger, a.allow_provisional)
    os.makedirs(a.out, exist_ok=True)
    made = 0
    for sel_d, reb_d in pairs:
        pit = as_of_ledger(led, sel_d)
        if pit.empty:
            print(f"[건너뜀] {sel_d.date()}: 공개된 판정 0건 (원장 disclosed_at 확인)")
            continue
        facts = market_facts(pit["ticker"].tolist(), sel_d)
        snap = to_snapshot(screen(facts, pit))
        snap["selection_date"] = sel_d.date()
        snap["ff_market_cap_asof"] = sel_d.date()
        snap["ff_market_cap_source"] = "pykrx 시총 x 원장 free_float"
        snap["free_float_asof"] = pit.set_index("ticker")["disclosed_at"] \
            .reindex(snap["ticker"]).dt.date.values
        snap["code_commit"] = a.code_commit
        dst = os.path.join(a.out, f"snapshot_{reb_d.strftime('%Y%m%d')}.csv")
        snap.to_csv(dst, index=False, encoding="utf-8-sig")
        ok = int(snap["eligible"].sum())
        print(f"[생성] {dst}  후보 {len(snap)} · 자격 {ok} "
              f"(선정일 {sel_d.date()} 기준 PIT)")
        made += 1
    print(f"\n{made}/{len(pairs)}회 생성. 다음: "
          f"python analysis/run_backtest.py --snapshots {a.out} --policy all")
    print("주의(생존편향): 원장이 '오늘 살아있는 종목'만 담고 있으면 과거 시점의"
          " 상장폐지 종목이 빠져 성과가 위로 편향된다. run_backtest.py 의"
          " listing_check() 로 대조하고, 한계점에 명시할 것.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
