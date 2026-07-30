# -*- coding: utf-8 -*-
"""
survivorship_check.py - 생존편향 정면 대응: 사라진 종목을 찾아낸다
====================================================================
왜 이 스크립트가 필요한가
-------------------------
판정 원장 33종목은 **오늘 살아있는 회사**로 구성돼 있다. 2026년에 유니버스를
만들었으니 당연한 일이지만, 그 원장으로 2020년부터 백테스트를 돌리면 그때
상장돼 있다가 이후 사라진 회사는 애초에 후보에 들어갈 수조차 없다. 사라진
회사는 대체로 성과가 나빴을 것이므로 지수 성과는 **위쪽으로 편향**된다.

기존 `run_backtest.listing_check()` 는 쉬운 방향만 본다 - "우리 33종목이 그
시점에 상장돼 있었는가". 이건 미상장 구간을 걸러내는 데는 쓸모 있지만
생존편향과는 방향이 반대다. 정작 필요한 것은:

    그 시점엔 상장·자격이었으나 **지금은 없는** 종목이 몇 개인가

이 스크립트가 그 절반(기계가 할 수 있는 부분)을 채운다. 나머지 절반(각
후보가 우리 편입 규칙을 통과했겠는가)은 판정이므로 사람이 해야 한다.

3단계 구조
----------
  1단계 [기계·네트워크]  --collect
      심사기준일 13개 시점의 **전체 상장 종목 명단**을 수집해 보존한다.
      시점 t 에 있고 시점 t+1 에 없는 종목 = 그 사이 소멸(상폐·합병·이전).

  2단계 [기계·오프라인]  --report
      소멸 종목 중 **소멸 직전 시점에 KRX 반도체 지수(5044) 구성종목이었던
      것**만 추린다. 전 시장 소멸 종목을 다 보면 수백 개라 사람이 판정할 수
      없다. 우리 유니버스가 반도체 밸류체인이므로 이 교집합이 후보의 상한이다.
      결과를 판정 기입 양식(CSV)으로 낸다.

  3단계 [사람]           --judgments
      각 후보에 대해 원장과 **같은 규칙**(노출도 30% / 메모리향 70% + 공정·
      위원회 확인)으로 판정한다. 기입된 CSV를 다시 넣으면 요약을 낸다.

무엇을 주장할 수 있게 되는가
----------------------------
  판정 결과 자격 0건  -> "전수 조사했고 편입 자격 후보는 없었다. 편향의
                        방향은 위쪽이나 이 유니버스에서는 실현되지 않았다"
  판정 결과 N건       -> 그 종목을 스냅샷에 넣고 재실행하면 **편향의 크기를
                        실측**할 수 있다(4단계, 별도 작업)

어느 쪽이든 "생존편향을 고려하지 않았다"는 지적은 막힌다. 지금은 크기가
미측정이라 인용 금지 목록에 올라 있는데, 최소한 **조사 범위와 후보 수**는
말할 수 있게 된다.

주의 - 이 조사로도 못 잡는 것
-----------------------------
  · 상장 자체를 한 적 없는 비상장 기업 (애초에 지수 대상 아님)
  · KRX 반도체 지수에 편입된 적 없는 소부장 기업 - 지수 편입 기준과 우리
    유니버스 기준이 다르므로 놓칠 수 있다. 이 한계는 보고서에 명시할 것.
  · 티커 소멸 사유 구분(상폐/합병/이전상장)은 자동으로 안 된다. 후보가
    적으면 사람이 개별 확인한다.

사용
----
    # 1단계 (KRX 로그인 필요, 수 분)
    python analysis\\survivorship_check.py --collect

    # 2단계 (오프라인)
    python analysis\\survivorship_check.py --report

    # 3단계 (판정 기입 후)
    python analysis\\survivorship_check.py --judgments evidence\\survivorship\\candidates_judged.csv
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys

import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

SNAP_RE = re.compile(r"snapshot_(\d{8})\.csv$")

#: KRX 반도체 지수 - benchmark.yaml 에서 확정한 코드(2026-07-30, 김소연 승인).
#: 후보 압축의 기준이므로 벤치마크와 같은 코드를 쓴다(다른 코드를 쓰면
#: '무엇을 반도체로 봤는가'가 두 곳에서 갈린다).
SEMI_INDEX_CODE = "5044"

DEFAULT_EVIDENCE = os.path.join(HERE, "evidence", "survivorship")


# ----------------------------------------------------------------------
# 공통 - 심사기준일
# ----------------------------------------------------------------------
def review_dates(snapshot_dir: str) -> list[str]:
    """스냅샷 파일명에서 심사 시행일을 뽑는다(YYYYMMDD 문자열).

    백테스트와 **같은 시점**을 봐야 한다. 임의 날짜를 쓰면 "그 시점에 자격이
    있었는가"라는 질문 자체가 어긋난다.
    """
    out = []
    for p in sorted(glob.glob(os.path.join(snapshot_dir, "snapshot_*.csv"))):
        m = SNAP_RE.search(os.path.basename(p))
        if m:
            out.append(m.group(1))
    if not out:
        raise SystemExit(f"[중단] 스냅샷을 찾지 못했습니다: {snapshot_dir}")
    return out


def ledger_tickers(snapshot_dir: str) -> set:
    """원장 33종목. 후보에서 제외해야 한다(이미 조사 대상)."""
    tickers: set = set()
    for p in glob.glob(os.path.join(snapshot_dir, "snapshot_*.csv")):
        d = pd.read_csv(p, dtype={"ticker": str})
        tickers |= set(d["ticker"].str.strip().str.zfill(6))
    return tickers


# ----------------------------------------------------------------------
# 1단계 - 상장 명단 수집 (네트워크)
# ----------------------------------------------------------------------
def collect(snapshot_dir: str, out_dir: str) -> int:
    """심사 시점별 전 시장 상장 명단 + 반도체 지수 구성종목을 보존한다.

    보존하는 이유: KRX 조회는 시점이 지나면 재현이 어렵고, 세션도 1시간이면
    끊긴다. 원응답을 파일로 남겨야 나중에 "그때 명단이 이랬다"를 증명할 수
    있다(KIND 조사와 같은 규율).
    """
    try:
        from pykrx import stock
    except ImportError:
        raise SystemExit("[중단] pykrx 미설치 - pip install pykrx")

    os.makedirs(out_dir, exist_ok=True)
    dates = review_dates(snapshot_dir)
    print(f"심사 시점 {len(dates)}개: {dates[0]} ~ {dates[-1]}")

    rows = []
    for ymd in dates:
        listed_path = os.path.join(out_dir, f"listed_{ymd}.csv")
        semi_path = os.path.join(out_dir, f"semi_{ymd}.csv")

        if os.path.exists(listed_path) and os.path.exists(semi_path):
            print(f"  {ymd}  [캐시 재사용]")
            continue

        try:
            listed = sorted(set(stock.get_market_ticker_list(ymd, market="ALL")))
        except Exception as exc:
            raise SystemExit(f"[중단] {ymd} 상장 명단 조회 실패: {exc}")
        if not listed:
            raise SystemExit(f"[중단] {ymd} 상장 명단이 비었습니다 - 휴장일이거나 "
                             "로그인 만료. 재실행하십시오.")
        pd.DataFrame({"ticker": listed}).to_csv(
            listed_path, index=False, encoding="utf-8-sig")

        try:
            semi = sorted(set(stock.get_index_portfolio_deposit_file(
                SEMI_INDEX_CODE, ymd)))
        except Exception as exc:
            print(f"  [주의] {ymd} 반도체 지수 구성 조회 실패({exc}) - 빈 목록으로 둡니다")
            semi = []
        pd.DataFrame({"ticker": semi}).to_csv(
            semi_path, index=False, encoding="utf-8-sig")

        rows.append((ymd, len(listed), len(semi)))
        print(f"  {ymd}  전체 {len(listed):,}종목 · 반도체지수 {len(semi)}종목")

    if rows:
        print(f"\n저장: {out_dir}/  (listed_*.csv · semi_*.csv)")
    print("다음: python analysis\\survivorship_check.py --report")
    return 0


# ----------------------------------------------------------------------
# 2단계 - 소멸 종목 추출 + 후보 압축 (오프라인)
# ----------------------------------------------------------------------
def _read_set(path: str) -> set:
    if not os.path.exists(path):
        return set()
    d = pd.read_csv(path, dtype={"ticker": str})
    return set(d["ticker"].str.strip().str.zfill(6))


def find_disappeared(dates: list[str], out_dir: str) -> pd.DataFrame:
    """연속한 두 시점을 비교해 사라진 종목을 찾는다.

    소멸 = 앞 시점 명단에 있고 뒤 시점 명단에 없음. 사유(상폐·합병·이전상장)는
    이 정보만으로 구분되지 않으므로 후보 단계에서 사람이 확인한다.
    마지막 시점 이후의 소멸은 표본 밖이라 보지 않는다.

    반도체 여부를 두 기준으로 낸다
    -----------------------------
      소멸직전_반도체지수 : 소멸 **직전** 시점에 지수 구성종목이었는가
      반도체지수_이력     : 소멸 이전 **어느 시점이든** 구성종목이었던 적이 있는가

    직전 시점만 보면 놓치는 경우가 있다 - 2021년에 반도체지수에 있다가 2022년에
    지수에서 빠지고(상장은 유지) 2023년에 상폐된 회사는 소멸 직전엔 이미 지수
    밖이다. 그런 회사도 우리 유니버스 후보였을 수 있으므로 이력 기준이 맞다.
    두 값을 다 실어서 **기준을 바꿔도 결론이 같은지**를 보이게 한다 - 결론이
    기준 선택에 의존하면 그 조사는 신뢰할 수 없다.
    """
    rows = []
    seen_semi: set = set()          # 지금까지 관측된 반도체지수 구성종목 누적
    for prev_ymd, next_ymd in zip(dates, dates[1:]):
        prev = _read_set(os.path.join(out_dir, f"listed_{prev_ymd}.csv"))
        nxt = _read_set(os.path.join(out_dir, f"listed_{next_ymd}.csv"))
        if not prev or not nxt:
            raise SystemExit(f"[중단] 상장 명단 캐시 없음({prev_ymd}/{next_ymd}) - "
                             "--collect 를 먼저 실행하십시오")
        semi_prev = _read_set(os.path.join(out_dir, f"semi_{prev_ymd}.csv"))
        seen_semi |= semi_prev      # 소멸 시점 **이전**까지만 누적(미래 참조 금지)
        for t in sorted(prev - nxt):
            rows.append({
                "ticker": t,
                "소멸구간_시작": prev_ymd,
                "소멸구간_종료": next_ymd,
                "소멸직전_반도체지수": t in semi_prev,
                "반도체지수_이력": t in seen_semi,
            })
    return pd.DataFrame(rows)


def build_candidates(snapshot_dir: str, out_dir: str) -> pd.DataFrame:
    """소멸 종목 중 반도체 후보만 남긴다. 원장 33종목은 제외.

    넓은 쪽(`반도체지수_이력`)을 쓴다. 좁은 기준으로 후보를 놓치는 것이
    넓은 기준으로 몇 개 더 판정하는 것보다 훨씬 나쁘다 - 놓친 후보는
    '조사했는데 없었다'로 둔갑한다.
    """
    dates = review_dates(snapshot_dir)
    gone = find_disappeared(dates, out_dir)
    if gone.empty:
        return gone
    known = ledger_tickers(snapshot_dir)
    cand = gone[gone["반도체지수_이력"] & ~gone["ticker"].isin(known)].copy()
    cand = cand.drop(columns=["소멸직전_반도체지수", "반도체지수_이력"])
    # 판정 기입 칸 - 원장과 **같은 규칙**을 쓴다. 다른 잣대를 대면 비교가 안 된다.
    for col in ("종목명", "소멸사유", "HBM노출도", "메모리향비중",
                "HBM공정확인", "위원회확인", "편입자격", "판정자", "판정일", "근거"):
        cand[col] = ""
    return cand.reset_index(drop=True)


def report(snapshot_dir: str, out_dir: str) -> int:
    dates = review_dates(snapshot_dir)
    gone = find_disappeared(dates, out_dir)
    known = ledger_tickers(snapshot_dir)

    print("=" * 74)
    print("생존편향 조사 - 표본 기간 중 소멸한 종목")
    print("=" * 74)
    print(f"조사 구간   : {dates[0]} ~ {dates[-1]} (심사 시점 {len(dates)}개)")
    print(f"원장 종목   : {len(known)}종목 (조사 대상에서 제외)")

    if gone.empty:
        print("\n소멸 종목 0건 - 표본 기간에 상장 소멸이 관측되지 않았습니다.")
        print("(전 시장 기준이므로 0건은 이례적입니다. 캐시가 온전한지 확인하십시오.)")
        return 0

    n_strict = int(gone["소멸직전_반도체지수"].sum())
    n_ever = int(gone["반도체지수_이력"].sum())
    ever_pool = set()
    for ymd in dates:
        ever_pool |= _read_set(os.path.join(out_dir, f"semi_{ymd}.csv"))
    print(f"소멸 종목   : 전 시장 {len(gone)}건")
    print(f"반도체지수  : 표본 기간 편입 이력 {len(ever_pool)}종목(합집합)")

    print("\n[반도체 여부 - 두 기준]")
    print(f"  소멸 직전 시점 구성종목      : {n_strict}건")
    print(f"  소멸 이전 어느 시점이든 편입 : {n_ever}건   <- 후보 판정에 쓰는 기준")
    if n_strict == n_ever:
        print("  -> 두 기준이 같습니다. 결론이 기준 선택에 의존하지 않습니다.")
    else:
        print("  -> 기준에 따라 갈립니다. 넓은 쪽(이력)으로 판정하십시오.")

    print("\n[구간별 소멸 건수]")
    per = (gone.groupby(["소멸구간_시작", "소멸구간_종료"])
           .agg(전체=("ticker", "size"),
                반도체_직전=("소멸직전_반도체지수", "sum"),
                반도체_이력=("반도체지수_이력", "sum")))
    print(per.to_string())

    cand = build_candidates(snapshot_dir, out_dir)
    os.makedirs(out_dir, exist_ok=True)
    gone_path = os.path.join(out_dir, "disappeared_all.csv")
    cand_path = os.path.join(out_dir, "candidates_template.csv")
    gone.to_csv(gone_path, index=False, encoding="utf-8-sig")
    cand.to_csv(cand_path, index=False, encoding="utf-8-sig")

    print(f"\n[판정 대상 후보] {len(cand)}건")
    if len(cand):
        print(cand[["ticker", "소멸구간_시작", "소멸구간_종료"]].to_string(index=False))
        print("\n  -> candidates_template.csv 를 복사해 판정을 기입하십시오.")
        print("     원장과 **같은 규칙**을 씁니다(노출도 30% / 메모리향 70% +")
        print("     공정·위원회 확인). 다른 잣대를 대면 비교가 성립하지 않습니다.")
    else:
        print("  반도체 지수 구성종목 중 소멸한 종목이 없습니다.")
        print("  -> 편향의 방향은 위쪽이나, 이 유니버스에서는 후보가 실현되지")
        print("     않았다고 보고할 수 있습니다.")

    print(f"\n저장: {gone_path}")
    print(f"      {cand_path}")
    print("\n한계(보고서에 명시할 것): KRX 반도체 지수에 편입된 적 없는 소부장")
    print("기업은 이 압축에서 빠집니다. 지수 편입 기준과 우리 유니버스 기준이")
    print("다르므로 후보 수는 '하한'이 아니라 '반도체지수 기준 전수'입니다.")
    return 0


# ----------------------------------------------------------------------
# 3단계 - 판정 병합 요약
# ----------------------------------------------------------------------
_TRUE = {"true", "1", "y", "yes", "예", "o", "자격"}


def summarize_judgments(path: str) -> int:
    """기입된 판정 CSV -> 발표에 쓸 요약.

    자격 판정이 하나라도 있으면 편향의 **크기 측정**으로 넘어가야 한다
    (해당 종목을 스냅샷에 넣고 재실행 -> 성과 차이). 그 전까지는 크기를
    숫자로 말하지 않는다 - 지금 인용 금지 목록에 올라 있는 이유가 그것이다.
    """
    if not os.path.exists(path):
        raise SystemExit(f"[중단] 판정 파일 없음: {path}")
    d = pd.read_csv(path, dtype={"ticker": str})
    if "편입자격" not in d.columns:
        raise SystemExit("[중단] '편입자격' 열이 없습니다 - "
                         "candidates_template.csv 양식을 쓰십시오")
    blank = d["편입자격"].astype(str).str.strip().eq("")
    if blank.any():
        raise SystemExit(f"[중단] 판정 미기입 {int(blank.sum())}건 - "
                         f"{sorted(d.loc[blank, 'ticker'])}")
    qualified = d[d["편입자격"].astype(str).str.strip().str.lower().isin(_TRUE)]

    print("=" * 74)
    print("생존편향 판정 요약")
    print("=" * 74)
    print(f"후보 {len(d)}건 중 편입 자격 판정 **{len(qualified)}건**")
    if len(qualified):
        print(qualified[["ticker", "종목명", "소멸구간_시작",
                         "소멸구간_종료", "근거"]].to_string(index=False))
        print("\n다음 단계: 이 종목들을 해당 시점 스냅샷에 넣고 백테스트를 재실행해")
        print("성과 차이를 측정하십시오. 그 차이가 생존편향의 크기입니다.")
        print("측정 전까지는 크기를 숫자로 말하지 마십시오(인용 금지 항목).")
    else:
        print("\n편입 자격 후보 0건.")
        print("발표 문안: \"표본 기간 중 소멸한 반도체지수 구성종목을 전수")
        print("조사했고, 우리 편입 규칙을 통과할 종목은 없었습니다. 생존편향의")
        print("방향은 위쪽이지만 이 유니버스에서는 실현되지 않았습니다.\"")
    return 0


# ----------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="생존편향 조사 - 사라진 종목 추적")
    ap.add_argument("--snapshots", default=os.path.join(HERE, "data", "snapshots"))
    ap.add_argument("--out", default=DEFAULT_EVIDENCE)
    ap.add_argument("--collect", action="store_true", help="1단계: 상장 명단 수집(네트워크)")
    ap.add_argument("--report", action="store_true", help="2단계: 소멸 종목·후보 추출")
    ap.add_argument("--judgments", default=None, help="3단계: 기입된 판정 CSV 경로")
    a = ap.parse_args()

    pd.set_option("display.width", 200)
    if a.judgments:
        return summarize_judgments(a.judgments)
    if a.collect:
        rc = collect(a.snapshots, a.out)
        if rc or not a.report:
            return rc
    if a.report or not a.collect:
        return report(a.snapshots, a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
