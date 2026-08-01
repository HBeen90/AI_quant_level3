# -*- coding: utf-8 -*-
"""
rule_c_sensitivity.py ― 규칙 C(위성군 3요건)의 지수 성과 기여도
==================================================================
"선택편향을 고려하지 않았다"는 지적에 크기로 답하되, **무엇이 실제로
구속하고 있는지**를 정확히 짚는다.

먼저 오해를 하나 걷어낸다
  스냅샷의 탈락사유 문자열 `공정/위원회 미확인` 은 **판정이 수행되지
  않았다는 뜻이 아니다.** 해당 행의 `judgment_status` 는 전부 `FINAL`
  이며, `process_confirmed=False` 는 **판정 결과 미충족**을 뜻한다.
  테크윙이 그 증거다 ― FY2019~2023 은 False, HBM 공정에 진입한 FY2024
  부터 True 로 바뀐다. PIT 가 제대로 작동한 흔적이지 판정 누락이 아니다.

  (문자열이 오해를 부르므로 `build_pit_snapshots.py` 의 사유 표기를
   `규칙 C 미충족`으로 바꿀 것을 권고한다 ― 별도 안건.)

그러면 무엇을 재는가 ― 규칙 C 의 성과 기여도
  규칙 C 는 세 요건의 **결합**이다.

      (1) 메모리향 매출 비중 70% 이상
      (2) HBM 고유공정(TSV·하이브리드본딩 등) 귀속 매출을 문서로 확인
      (3) 지수위원회 확인 절차 통과

  (1)은 재무 수치라 재현이 쉽고, (2)(3)은 문서 판정이다. 이 스크립트는
  **(2)(3)을 껐을 때 지수가 어떻게 달라지는가**를 잰다. 즉 정량 요건만으로
  위성을 편입하는 대안 지수와 비교한다.

  이는 편향의 추정치가 아니라 **규칙 C 가 성과에 얼마나 기여하는가**의
  측정이다. 기여도가 클수록 그 규칙의 조문·근거가 중요해진다.

왜 이 측정이 필요한가
  방법론 조문(규칙 3)은 위성군을 "HBM 귀속 매출 분리가 어려운 전공정
  장비·소재 기업"이라는 **정성 서술**로만 적고 있다. 3요건은 코드
  (`src/selection.py`)와 판정원장에만 있다. 성과 기여도가 크다면 문서화
  되지 않은 규칙이 성과의 상당 부분을 만들고 있다는 뜻이고, 그것은 조문
  신설로 닫아야 할 결함이다.

경계
  엔진을 수정하지 않는다. 스냅샷의 `eligible`·`group` 만 시나리오대로
  바꿔 기존 `build_event_schedule` -> `simulate_index` 에 넣는다.
  이 스크립트는 조문을 바꾸지 않으며 진단 수치만 만든다.

사용
    python analysis/rule_c_sensitivity.py \\
        --snapshots data/snapshots --prices-cache out/px.csv --out out/backtest
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "analysis"))

from analysis.index_calendar import as_of_today  # noqa: E402
from backtest.backtest import (ann_vol, annualized_turnover,  # noqa: E402
                               build_event_schedule, cagr, max_drawdown,
                               simulate_index)
from src.rebalance import SAT, ConfigV2  # noqa: E402

#: 규칙 C 요건②③ 미충족을 뜻하는 탈락사유 표기. **세대가 둘 있다.**
#:   구: "공정/위원회 미확인"        (오해 소지 - 조문 초안 §2 참조)
#:   신: "규칙 C 요건②③ 미충족"     (위원회 안건 1-4 로 정정)
#: 하나만 인식하면, 표기가 바뀐 스냅샷에서 완화 대상을 **하나도 못 찾고도
#: 죽지 않는다.** 그러면 기여도가 조용히 축소된다 - 2026-08-02 에 실제로
#: 발생했다(스냅샷 5/13 만 신 표기 -> CAGR 차이 13.50%p 가 8.04%p 로 붕괴).
#: 표기 하나가 감사 결론을 바꾼다는 조문 초안의 경고가 이 도구 자신에게
#: 일어난 사례이므로, 목록으로 두고 미인식 표기는 fail-closed 한다.
RULE_C_UNMET_LABELS = (
    "공정/위원회 미확인",
    "규칙 C 요건②③ 미충족",
)
#: 하위호환 별칭. **컬렉션**이다 - 기존 테스트가 `x in RULE_C_UNMET` 으로
#: 멤버십을 검사한다. 문자열 하나로 두면 부분일치가 되어 조용히 통과한다.
RULE_C_UNMET = RULE_C_UNMET_LABELS
SAT_MEM_TH = 0.70


def _rule_c_candidates(frame) -> "pd.Series":
    """규칙 C **심사 대상**인 행. `mem_ratio >= 0.70` 만으로 세면 안 된다.

    `classify_row` 는 규칙 0(앵커) -> A(핵심 30%) -> C(위성) 를 **순차** 적용하며,
    앞 규칙에서 확정된 종목은 규칙 C 를 보지 않는다. SK하이닉스(메모리향 0.95)는
    앵커이므로 규칙 C 후보가 아니고, 이를 세면 규칙 C 의 구속력이 과대계상된다.

    `build_pit_snapshots.to_snapshot()` 의 `group` 라벨이 그 순차 적용 결과와
    정확히 일치한다.

        group == "satellite"  <=>  앵커 아님 AND 노출도 < 30% AND 메모리향 >= 70%

    그러므로 `group` 을 쓴다. 임계값을 여기서 다시 구현하면 스냅샷 생성기와
    갈릴 수 있다(단일 출처 유지).
    """
    if "group" not in frame.columns:
        sys.exit("[FAIL] 스냅샷에 group 열이 없다 - 규칙 C 후보를 특정할 수 없다. "
                 "mem_ratio 만으로 세면 앵커·핵심이 섞여 구속력이 과대계상된다.")
    return frame["group"].astype(str).str.strip().eq(SAT)


def _unmet_mask(frame) -> "pd.Series":
    """탈락사유가 규칙 C 요건②③ 미충족인 행. 신·구 표기 모두 인식한다."""
    rsn = frame.get("탈락사유", pd.Series("", index=frame.index))
    rsn = rsn.fillna("").astype(str).str.strip()
    return rsn.isin(RULE_C_UNMET_LABELS)


def check_label_generation(snaps: dict) -> pd.DataFrame:
    """스냅샷별로 어느 표기 세대를 쓰는지 조사하고, **섞여 있으면 중단**한다.

    왜 중단하는가
      세대가 섞이면 각 회차가 서로 다른 코드로 생성된 것이고, 그 상태의
      측정값은 어느 조문 상태를 재는 것인지 말할 수 없다. 지수 레벨은
      `eligible` 만 쓰므로 무해할 수 있으나, 이 도구는 `탈락사유` 로 완화
      대상을 고르므로 **직접 오염된다.** 부분 재생성으로 봉합하지 말고
      13회차를 한 커밋으로 전량 재생성해야 한다.
    """
    rows = []
    for d in sorted(snaps):
        s = snaps[d]
        rsn = s.get("탈락사유", pd.Series("", index=s.index))
        rsn = rsn.fillna("").astype(str).str.strip()
        gen = sorted({lab for lab in RULE_C_UNMET_LABELS if (rsn == lab).any()})
        cand = s[_rule_c_candidates(s) & (~s["eligible"].astype(bool))]
        crsn = cand.get("탈락사유", pd.Series("", index=cand.index))
        crsn = crsn.fillna("").astype(str).str.strip()
        rows.append({
            "심사시점": str(pd.Timestamp(d).date()),
            "표기 세대": "/".join(gen) if gen else "(없음)",
            "규칙C후보·부적격": len(cand),
            "규칙C 사유": int(crsn.isin(RULE_C_UNMET_LABELS).sum()),
            "기타 사유": int((~crsn.isin(RULE_C_UNMET_LABELS)).sum()),
        })
    rep = pd.DataFrame(rows)
    gens = {g for g in rep["표기 세대"] if g != "(없음)"}
    if len(gens) > 1:
        print(rep.to_string(index=False))
        sys.exit(
            "[FAIL] 스냅샷의 탈락사유 표기가 세대별로 섞여 있다: "
            f"{sorted(gens)}\n"
            "       이 상태에서 낸 규칙 C 기여도는 어느 조문 상태의 값인지 "
            "말할 수 없다.\n"
            "       13회차를 한 코드 커밋으로 전량 재생성한 뒤 재실행할 것 "
            "(부분 재생성 금지).")
    return rep


def rule_c_report(snaps: dict, ledger_path: str) -> pd.DataFrame:
    """심사시점별 규칙 C 요건 통과 현황. (2)(3) 이 구속하는 크기를 보인다."""
    led = pd.read_csv(ledger_path, dtype={"ticker": str})
    led["ticker"] = led["ticker"].str.strip().str.zfill(6)
    fin = set(led.loc[led["judgment_status"].astype(str).eq("FINAL"), "ticker"])
    rows = []
    for d in sorted(snaps):
        s = snaps[d]
        hi = s[_rule_c_candidates(s)]          # 앵커·핵심 제외
        unmet = _unmet_mask(hi)
        rows.append({
            "심사시점": str(pd.Timestamp(d).date()),
            "요건(1) 통과": len(hi),
            "요건(2)(3) 통과": int(hi["eligible"].astype(bool).sum()),
            "요건(2)(3) 미충족": int(unmet.sum()),
            "판정 FINAL 비율": (f"{sum(t in fin for t in hi['ticker'])}/{len(hi)}"
                           if len(hi) else "0/0"),
        })
    return pd.DataFrame(rows)


def relax_rule_c(snaps: dict) -> dict:
    """요건 (2)(3) 을 끈 스냅샷 사본 ― 정량 요건(1)만으로 위성 편입."""
    out = {}
    for d, s in snaps.items():
        t = s.copy()
        flip = _rule_c_candidates(t) & (~t["eligible"].astype(bool)) \
            & _unmet_mask(t)
        t.loc[flip, "eligible"] = True
        t.loc[flip, "group"] = SAT
        out[d] = t
    return out


def summarize(label, bt, hist, base):
    lv = bt["level"]
    md = max_drawdown(lv)
    n = pd.to_numeric(hist.get("n", pd.Series(dtype=float)),
                      errors="coerce").dropna()
    row = {"시나리오": label, "CAGR": cagr(lv), "연변동성": ann_vol(lv),
           "MDD": md["mdd"], "연율화회전율": annualized_turnover(bt),
           "정기변경 평균 종목수": float(n.mean()) if len(n) else np.nan,
           "최종레벨": float(lv.iloc[-1])}
    row["CAGR 차이"] = (row["CAGR"] - cagr(base["level"])) if base is not None else 0.0
    row["최종레벨 차이"] = (float(lv.iloc[-1] / base["level"].iloc[-1] - 1.0)
                     if base is not None else 0.0)
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots", required=True)
    ap.add_argument("--prices-cache", required=True)
    ap.add_argument("--policy", default="mid",
                    choices=("none", "narrow", "mid", "wide"))
    ap.add_argument("--out", default="out/backtest")
    a = ap.parse_args()

    import run_backtest as rb                                   # noqa: E402
    cfg = ConfigV2.with_policy(a.policy)
    snaps = rb.load_snapshots(a.snapshots)
    tickers = sorted({t for df in snaps.values() for t in df["ticker"]})
    px = rb.fetch_prices(tickers, min(snaps).strftime("%Y%m%d"),
                         as_of_today().strftime("%Y%m%d"), cache=a.prices_cache)
    px = px.dropna(axis=1, how="all")

    gen = check_label_generation(snaps)          # 섞여 있으면 여기서 중단
    print("[탈락사유 표기 세대 점검]\n", gen.to_string(index=False))

    rep = rule_c_report(snaps, os.path.join(HERE, "data", "verdict_ledger.csv"))
    print("\n[규칙 C 요건별 통과 현황]\n", rep.to_string(index=False))
    print("      * '요건(1) 통과' = 규칙 C **심사 대상** 수(group=satellite). "
          "규칙 0(앵커)·A(핵심)로 확정된 종목은 순차 적용상 규칙 C 를 보지 않으므로 "
          "제외한다 - 포함하면 구속력이 과대계상된다.")

    n_flip = int(rep["요건(2)(3) 미충족"].sum())
    if n_flip == 0:
        sys.exit("[FAIL] 완화 대상이 전 회차에서 0건이다 - 탈락사유 표기가 "
                 "RULE_C_UNMET_LABELS 와 맞지 않을 가능성이 크다. 0 을 "
                 "결과로 보고하지 않는다(fail-closed).")

    ev0, h0 = build_event_schedule(px, snaps, cfg=cfg)
    bt0 = simulate_index(px, ev0, base=1000.0)
    ev1, h1 = build_event_schedule(px, relax_rule_c(snaps), cfg=cfg)
    bt1 = simulate_index(px, ev1, base=1000.0)

    tbl = pd.DataFrame([summarize("현행 (규칙 C 3요건 전부)", bt0, h0, None),
                        summarize("대안 (요건 1만 - 공정·위원회 해제)", bt1, h1, bt0)])
    os.makedirs(a.out, exist_ok=True)
    rep.to_csv(os.path.join(a.out, "rule_c_requirements.csv"),
               index=False, encoding="utf-8-sig")
    tbl.to_csv(os.path.join(a.out, "rule_c_sensitivity.csv"),
               index=False, encoding="utf-8-sig")

    print("\n[규칙 C 기여도]\n", tbl.round(4).to_string(index=False))
    gap = float(tbl.iloc[1]["CAGR 차이"])
    print(f"\n[해석] 요건 (2)(3)(공정 문서확인·위원회 확인)을 해제하면 CAGR 이 "
          f"{gap:+.2%}p 변한다. 이는 편향의 추정치가 아니라 **규칙 C 가 성과에 "
          f"기여하는 크기**다.")
    print("[함의] 방법론 조문(규칙 3)은 위성군을 정성 서술로만 적고 있고 "
          "3요건은 코드·판정원장에만 있다. 기여도가 이 정도면 조문 신설이 "
          "필요하다 ― 문서화되지 않은 규칙이 성과를 만들고 있다.")
    print(f"[저장] {a.out}/rule_c_sensitivity.csv · rule_c_requirements.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
