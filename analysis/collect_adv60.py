# -*- coding: utf-8 -*-
"""
collect_adv60.py - 용량 분석용 ADV60 패널 수집
===============================================
무엇을 만드는가
---------------
`capacity_v2.py --adv <CSV>` 가 요구하는 계약 파일을 만든다.

    ticker, adv60_krw        (6자리 종목코드 · 60거래일 평균 거래대금, 원)

`capacity_v2` 는 이미 완성돼 있다. 소요일수도 역산 상한도 다 계산한다.
없던 것은 **실측 ADV60 하나**였고, 그래서 지금까지 가정 ADV(15·45·500억)
시나리오로만 돌았다. 그 결과가 인용 금지 목록의 "종목별 ADV60 시계열" 항목이다.

정의를 새로 만들지 않는다
-------------------------
ADV60 은 이미 `build_pit_snapshots.market_facts()` 안에서 계산되고 있고,
그 값으로 사전 스크린(ADV60 < 10억 탈락)이 돌아간다. 여기서 다시 구현하면
**같은 이름의 두 정의**가 생기고, 어느 쪽 숫자를 말하는지 아무도 확신하지
못하게 된다. 그래서 그 함수를 그대로 호출한다.

    ADV60 = get_market_cap(구간, 종목)["거래대금"].tail(60).mean()

pykrx 1.2.x 에서 get_market_ohlcv 단일종목 조회에 거래대금이 빠진 뒤
get_market_cap 기간 조회를 단일 원천으로 쓰기로 한 결정도 그대로 승계된다.

기준일을 왜 고정하는가
----------------------
용량은 "지금 이 지수를 얼마까지 담을 수 있는가"의 문제이므로 **확정 기준일
(INDEX_ASOF)** 에서 재는 것이 맞다. 기본값을 가격 패널의 마지막 거래일로
두어 백테스트 종료일과 자동으로 일치시킨다 - 손으로 날짜를 넣으면 언젠가
어긋난다.

사용
----
    python analysis\\collect_adv60.py
    python analysis\\capacity_v2.py --snapshots data\\snapshots ^
        --prices out\\px.csv --adv data\\adv60.csv --aum 3000 --policy mid
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

EOK = 1e8          # 1억원


def panel_tickers_and_asof(px_path: str):
    """가격 패널에서 종목 목록과 기준일(마지막 거래일)을 뽑는다.

    날짜를 인자로 받지 않고 패널에서 유도하는 이유: 백테스트 종료일과
    ADV 기준일이 어긋나면 '이 지수의 용량'이라는 말이 성립하지 않는다.
    """
    px = pd.read_csv(px_path, index_col=0, parse_dates=True)
    if px.empty:
        raise SystemExit(f"[중단] 가격 패널이 비었습니다: {px_path}")
    tickers = [str(c).strip().zfill(6) for c in px.columns]
    return tickers, px.index[-1]


def collect(tickers: list[str], asof: pd.Timestamp) -> pd.DataFrame:
    """market_facts 를 그대로 호출해 ADV60 을 받는다(정의 단일화)."""
    from analysis.build_pit_snapshots import market_facts
    facts = market_facts(tickers, asof)
    if "adv60" not in facts.columns:
        raise SystemExit("[중단] market_facts 응답에 adv60 컬럼이 없습니다 - "
                         "build_pit_snapshots 변경 여부 확인")
    out = facts.reset_index()[["ticker", "listed", "adv60"]].copy()
    out["adv60"] = pd.to_numeric(out["adv60"], errors="coerce")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="용량 분석용 ADV60 수집")
    ap.add_argument("--prices-cache", default=os.path.join(HERE, "out", "px.csv"))
    ap.add_argument("--out", default=os.path.join(HERE, "data", "adv60.csv"))
    ap.add_argument("--asof", default=None,
                    help="기준일 YYYY-MM-DD (기본: 가격 패널 마지막 거래일)")
    a = ap.parse_args()

    tickers, panel_end = panel_tickers_and_asof(a.prices_cache)
    asof = pd.Timestamp(a.asof) if a.asof else panel_end
    if a.asof and asof != panel_end:
        print(f"[주의] 기준일({asof.date()})이 가격 패널 마지막 거래일"
              f"({panel_end.date()})과 다릅니다. 백테스트와 다른 시점의 "
              "용량을 재는 것이므로 보고 시 명시하십시오.")

    print(f"기준일 {asof.date()} · {len(tickers)}종목 · ADV60 수집")
    facts = collect(tickers, asof)

    listed = facts[facts["listed"].astype(bool)].copy()
    missing = facts[~facts["listed"].astype(bool)]["ticker"].tolist()
    bad = listed[listed["adv60"].isna() | (listed["adv60"] <= 0)]["ticker"].tolist()
    if bad:
        raise SystemExit(f"[중단] ADV60 결측·비양수: {bad} - "
                         "조용히 0으로 두면 소요일수가 무한대가 됩니다")

    out = listed[["ticker", "adv60"]].rename(columns={"adv60": "adv60_krw"})
    out = out.sort_values("adv60_krw", ascending=False).reset_index(drop=True)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    out.to_csv(a.out, index=False, encoding="utf-8-sig")

    manifest = {
        "asof": asof.strftime("%Y-%m-%d"),
        "종목수": int(len(out)),
        "미상장_제외": missing,
        "정의": "get_market_cap(구간, 종목)['거래대금'].tail(60).mean()",
        "원천": "pykrx get_market_cap · build_pit_snapshots.market_facts 재사용",
        "주의": "사전 스크린(ADV60<10억 탈락)과 같은 정의·같은 함수. "
                "다른 값이 나오면 둘 중 하나가 잘못된 것.",
    }
    mpath = os.path.splitext(a.out)[0] + "_manifest.json"
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    eok = out["adv60_krw"] / EOK
    print(f"\n[완료] {len(out)}종목 수집" +
          (f" · 미상장 제외 {len(missing)}종목 {missing}" if missing else ""))
    print(f"저장: {a.out}")
    print(f"      {mpath}")
    print("\n[ADV60 분포 (억원)]")
    print(f"  최대 {eok.max():>10,.1f}   상위25% {eok.quantile(0.75):>10,.1f}")
    print(f"  중앙 {eok.median():>10,.1f}   하위25% {eok.quantile(0.25):>10,.1f}")
    print(f"  최소 {eok.min():>10,.1f}")
    thin = out[out["adv60_krw"] < 50 * EOK]
    if len(thin):
        print(f"\n  ADV60 50억 미만 {len(thin)}종목 - 용량 병목 후보")
    print("\n다음 - 실측 용량 분석")
    print("  python analysis\\capacity_v2.py --snapshots data\\snapshots ^")
    print("      --prices out\\px.csv --adv data\\adv60.csv --aum 3000 --policy mid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
