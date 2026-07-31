# -*- coding: utf-8 -*-
"""
cap_feasibility.py ― 월간 캡의 퇴화 구간 판정 (회전율 60.5%의 정체)
=====================================================================
확정 실행에서 회전율의 60.5%가 정기변경이 아니라 월말 캡에서 나왔다. 그 원인을
"3종목 구성이라 앵커 둘이 계속 상한을 때린다"로 설명해 왔는데, 엔진을 직접
돌려 보면 실제 동작은 그보다 강하다.

  종목 수가 적으면 월간 캡은 '상한 초과분 절삭'이 아니라
  **매월 균등비중으로 되돌리는 리밸런싱**으로 퇴화한다.

퇴화 메커니즘
  cap_algorithm 은 초과 종목을 target(25%)으로 누르고 잔여를 미실링 종목에
  비례 배분한 뒤 재검증을 반복한다. 종목이 적으면 이 폭포가 **전 종목을
  실링 집합에 넣고** 끝나며, 그 상태의 비중은 전부 target 으로 같다. 이어지는
  monitor() 의 정규화가 합계를 1로 맞추면 결과는 정확히 1/n ― 균등비중이다.
  즉 상한값(25%)은 사라지고 종목 수만 남는다.

측정된 임계 (엔진 실측, 무작위 비중 3,000회 x n)
  n <= 3 : 발동 시 100% 균등비중으로 퇴화 (안정 상태 없음 ― 매월 재발)
  n  = 4 : 발동 시 64.5% 퇴화 (균등비중 25% = target 과 같아 경계)
  n >= 5 : 퇴화 0% (상한이 상한으로 작동)

왜 중요한가
  퇴화 구간에서는 캡이 집중도를 낮추지 못한다. 3종목을 33.33%씩 균등하게
  되돌릴 뿐이고, 그 결과 최대비중은 25%가 아니라 33.33%로 유지된다.
  회전율만 매월 발생한다. "월말 30% 캡이 쏠림을 25%로 누른다"는 문장은
  n >= 5 에서만 참이며, 표본의 81.9%를 차지한 3~4종목 구간에는 적용되지 않는다.

경계 ― 조문을 바꾸지 않는다
  이 모듈은 판정만 한다. 캡 파라미터(30/25)나 하한(5)을 바꾸는 것은 방법론
  개정 절차 대상이며, 여기서는 '어느 구성이 퇴화 구간인가'와 '그 구간이
  회전율에 얼마를 기여했는가'만 낸다.

사용
    python analysis/cap_feasibility.py                          # 임계 표만
    python analysis/cap_feasibility.py --backtest out/backtest  # 실측 귀속까지
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from src.rebalance import ConfigV2, monitor  # noqa: E402


def is_degenerate(w: pd.Series, cfg: ConfigV2 = ConfigV2(),
                  tol: float = 1e-9) -> tuple:
    """한 비중 벡터에 캡을 걸었을 때 균등비중으로 퇴화하는가.

    반환: (발동 여부, 퇴화 여부, 편도 회전율, 캡 후 최대비중)
    """
    w = w.astype(float)
    w = w / w.sum()
    adj, changed = monitor(w, cfg)
    n = len(w)
    turnover = 0.5 * float((adj - w).abs().sum())
    degenerate = bool(np.allclose(adj.to_numpy(), 1.0 / n, atol=tol))
    return changed, changed and degenerate, turnover, float(adj.max())


def threshold_table(n_range=range(2, 11), trials: int = 3000, seed: int = 0,
                    cfg: ConfigV2 = ConfigV2()) -> pd.DataFrame:
    """종목 수별 캡 동작. 무작위 비중으로 발동률·퇴화율·회전율을 잰다.

    무작위 비중은 '어떤 구성에서도 그런가'를 보기 위한 것이지 시장 분포의
    추정이 아니다. 절대 확률이 아니라 **n 에 따른 정성적 전환**만 읽을 것.
    """
    rng = np.random.default_rng(seed)
    rows = []
    for n in n_range:
        fired = deg = 0
        tno, mx = [], []
        for _ in range(trials):
            w = pd.Series(rng.dirichlet(np.ones(n) * 1.5))
            changed, degenerate, t, m = is_degenerate(w, cfg)
            if changed:
                fired += 1
                tno.append(t)
                mx.append(m)
                deg += int(degenerate)
        rows.append({
            "종목수": n,
            "캡 발동률": fired / trials,
            "퇴화율(발동 중)": deg / fired if fired else np.nan,
            "발동시 평균 편도회전율": float(np.mean(tno)) if tno else np.nan,
            "캡 후 평균 최대비중": float(np.mean(mx)) if mx else np.nan,
            "판정": _verdict(deg / fired if fired else 0.0),
        })
    return pd.DataFrame(rows)


def _verdict(rate: float) -> str:
    if rate >= 0.99:
        return "퇴화 ― 캡이 균등비중 리밸런싱으로 동작"
    if rate > 0.0:
        return "경계 ― 구성에 따라 퇴화"
    return "정상 ― 상한이 상한으로 작동"


def min_safe_n(cfg: ConfigV2 = ConfigV2(), trials: int = 2000,
               seed: int = 1) -> int:
    """퇴화가 한 번도 관측되지 않는 최소 종목 수."""
    tbl = threshold_table(range(2, 12), trials=trials, seed=seed, cfg=cfg)
    ok = tbl[tbl["퇴화율(발동 중)"].fillna(0.0) == 0.0]
    return int(ok["종목수"].min()) if len(ok) else -1


# ----------------------------------------------------------------------
# 실측 귀속 ― 확정 실행 산출물이 있을 때
# ----------------------------------------------------------------------
def attribute(out_dir: str) -> pd.DataFrame:
    """event_log.csv 의 사유별 편도 회전율 귀속.

    캡 지분이 크면 그 자체가 문제인 것이 아니라, 위 임계표와 함께 읽어야
    의미가 생긴다 ― 퇴화 구간에서 발생한 캡 회전율은 집중도를 낮추지 않으므로
    '비용만 지불한 회전율'이다.
    """
    path = os.path.join(out_dir, "event_log.csv")
    if not os.path.exists(path):
        sys.exit(f"[FAIL] 이벤트 원장이 없다: {path} ― 확정 실행을 먼저 수행할 것")
    log = pd.read_csv(path)
    need = {"reason", "one_way_turnover"}
    if not need <= set(log.columns):
        sys.exit(f"[FAIL] event_log.csv 필수 컬럼 누락: {sorted(need - set(log.columns))}")
    g = log.groupby("reason")["one_way_turnover"].agg(["count", "sum"])
    g.columns = ["이벤트 수", "편도회전율 합"]
    total = float(g["편도회전율 합"].sum())
    g["지분"] = g["편도회전율 합"] / total if total else np.nan
    if "n_members" in log.columns:
        g["평균 종목수"] = log.groupby("reason")["n_members"].mean()
    return g.sort_values("편도회전율 합", ascending=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", default=None,
                    help="확정 실행 산출 폴더(out/backtest). 있으면 실측 귀속까지")
    ap.add_argument("--trials", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default=None, help="임계표 CSV 저장 경로")
    a = ap.parse_args()

    cfg = ConfigV2()
    tbl = threshold_table(trials=a.trials, seed=a.seed, cfg=cfg)
    print("[캡 파라미터] 트리거 30% -> 목표 25% "
          "(monitor() 고정값 · README 3장 수시변경 (1))")
    print("\n[종목 수별 캡 동작]\n", tbl.round(4).to_string(index=False))
    safe = min_safe_n(cfg, trials=max(500, a.trials // 4), seed=a.seed + 1)
    print(f"\n[임계] 퇴화가 관측되지 않는 최소 종목 수 = {safe}종목")
    print("      현행 하한 5종목은 이 임계와 일치한다 ― 다만 표본의 81.9%가"
          " 하한 미달이었으므로 실제로는 퇴화 구간에서 운영됐다.")
    print("\n[정밀] '캡 후 평균 최대비중'이 25%를 넘는 것은 결함이 아니다."
          " 30/25 히스테리시스는 30% 초과 종목만 25%로 누르므로,"
          " 25~30% 구간 종목은 그대로 남는다. 따라서 캡의 보장은"
          " '최대비중 25% 이하'가 아니라 '30% 초과 종목 소거'다."
          " 발표 문안도 후자로 적을 것.")

    if a.out:
        os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
        tbl.to_csv(a.out, index=False, encoding="utf-8-sig")
        print(f"[저장] {a.out}")

    if a.backtest:
        att = attribute(a.backtest)
        print("\n[실측 회전율 귀속]\n", att.round(4).to_string())
        cap = float(att.loc["cap", "지분"]) if "cap" in att.index else 0.0
        if cap > 0.5:
            print(f"\n[주의] 회전율의 {cap:.1%}가 월말 캡에서 발생했다. 위 임계표의"
                  " 퇴화 구간과 겹치는지 확인할 것 ― 겹친다면 그 회전율은"
                  " 집중도를 낮추지 않고 비용만 발생시킨 것이다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
