# -*- coding: utf-8 -*-
"""
regime_robustness.py ― 시장 구간별 성과 (Robustness)
======================================================
"좋은 장에서만 잘 되는 전략 아니냐"에 답한다. 외부 리뷰 요청 항목이다.

구간을 어떻게 나누는가 ― 이 선택이 결론을 지배한다
  **우리 지수의 성과를 보고 구간을 자르면 데이터 스누핑이다.** 잘 된
  구간과 안 된 구간을 사후에 갈라 놓고 "구간별로 봐도 견고하다"고 말하면
  아무것도 증명하지 못한다.

  그래서 두 축을 쓰되 **둘 다 우리 지수를 참조하지 않는다.**

    (1) 캘린더 구간 ― 외부 거시 사건으로 **사전 정의**한 날짜 경계.
        코드에 상수로 박아 두고 결과를 보고 바꾸지 않는다.
    (2) 앵커 대용 구간 ― 앵커 2종목(동일가중)의 고점 대비 낙폭이
        임계 이상이면 '조정', 아니면 '상승'. 기계적이며 사후 조정이 없다.

  앵커는 규칙 0(메모리 제조 + HBM 양산)으로 **필수 편입**되므로 우리
  선정 판단의 결과가 아니다. 그래서 대용치로 쓸 수 있다. 다만 지수
  구성종목이기도 하므로 완전 독립은 아니며, 그 점을 표에 함께 적는다.

상승·하락 포착률 (up/down capture)
  구간 분할과 별개로, 앵커 대용의 **일간 수익률 부호**로 나눠 우리 지수의
  평균 수익률을 비교한다. 상승일 포착률이 높고 하락일 포착률이 낮으면
  비대칭이 유리한 것이고, 둘 다 높으면 단순 고베타다.

경계
  엔진을 수정하지 않는다. 확정 산출물(`index_level.csv`)과 가격 패널만
  읽는다. 지수를 다시 굴리지 않으므로 수치가 갈릴 여지가 없다.

사용
    python analysis/regime_robustness.py --out out/backtest
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

TRADING_DAYS = 252

#: 캘린더 구간 ― 외부 거시 사건으로 **사전 정의**한다.
#: 결과를 보고 경계를 옮기지 않는다. 옮기면 스누핑이다.
CALENDAR_REGIMES = [
    ("2020-06-15", "2021-12-31", "COVID 유동성 랠리",
     "저금리·유동성 확대 구간"),
    ("2022-01-01", "2022-12-31", "긴축 전환",
     "Fed 2022-03 인상 개시 · 한은 연속 인상"),
    ("2023-01-01", "2023-12-31", "AI 전환기",
     "생성형 AI 확산 · HBM 수요 부상"),
    ("2024-01-01", "2024-12-31", "HBM 본격화",
     "HBM3E 양산 경쟁 · 고변동"),
    ("2025-01-01", "2026-07-23", "AI 확산기",
     "설비 투자 사이클 지속"),
]

#: 앵커 대용 구간 판정 임계 (고점 대비 낙폭)
DRAWDOWN_TH = 0.10

ANCHORS = ("005930", "000660")          # 삼성전자 · SK하이닉스 (규칙 0)


def _load(out_dir: str) -> tuple:
    lvl_p = os.path.join(out_dir, "index_level.csv")
    px_p = os.path.join(os.path.dirname(out_dir.rstrip("/\\")), "px.csv")
    if not os.path.exists(lvl_p):
        sys.exit(f"[FAIL] 지수 시계열이 없다: {lvl_p} - 확정 실행을 먼저 할 것")
    if not os.path.exists(px_p):
        sys.exit(f"[FAIL] 가격 캐시가 없다: {px_p}")
    lv = pd.read_csv(lvl_p, index_col=0, parse_dates=True)["level"].astype(float)
    px = pd.read_csv(px_p, index_col=0, parse_dates=True)
    px.columns = [str(c).zfill(6) for c in px.columns]
    miss = [t for t in ANCHORS if t not in px.columns]
    if miss:
        sys.exit(f"[FAIL] 앵커 종목이 가격 패널에 없다: {miss}")
    return lv, px


def anchor_proxy(px: pd.DataFrame) -> pd.Series:
    """앵커 2종목 동일가중 대용 지수. 시장·섹터 레짐 판정에만 쓴다."""
    r = px[list(ANCHORS)].pct_change(fill_method=None)
    if r.isna().all(axis=1).any():
        r = r.dropna(how="all")
    return (1.0 + r.mean(axis=1).fillna(0.0)).cumprod()


def _stats(lv: pd.Series, label: str, note: str = "") -> dict:
    if len(lv) < 3:
        return {"구간": label, "거래일": len(lv), "비고": "관측 부족" + note}
    r = lv.pct_change().dropna()
    yrs = len(lv) / TRADING_DAYS
    total = float(lv.iloc[-1] / lv.iloc[0] - 1.0)
    dd = float((lv / lv.cummax() - 1.0).min())
    vol = float(r.std(ddof=1) * np.sqrt(TRADING_DAYS))
    cagr = float((1.0 + total) ** (1 / yrs) - 1.0) if yrs > 0 else np.nan
    return {"구간": label, "거래일": len(lv),
            "기간수익률": total, "연율화": cagr, "연변동성": vol,
            "MDD": dd, "Sharpe(rf=0)": cagr / vol if vol > 0 else np.nan,
            "비고": note}


def calendar_table(lv: pd.Series) -> pd.DataFrame:
    rows = []
    for a, b, name, note in CALENDAR_REGIMES:
        seg = lv.loc[(lv.index >= a) & (lv.index <= b)]
        rows.append(_stats(seg, f"{name} ({a[:7]}~{b[:7]})", note))
    rows.append(_stats(lv, "전 구간", ""))
    return pd.DataFrame(rows)


def drawdown_table(lv: pd.Series, proxy: pd.Series,
                   th: float = DRAWDOWN_TH) -> pd.DataFrame:
    """앵커 대용의 고점 대비 낙폭으로 상승·조정을 가른다."""
    p = proxy.reindex(lv.index).ffill()
    down = (p / p.cummax() - 1.0) <= -th
    rows = []
    for flag, name in ((False, f"상승 (앵커 대용 낙폭 < {th:.0%})"),
                       (True, f"조정 (앵커 대용 낙폭 >= {th:.0%})")):
        seg = lv[down.eq(flag)]
        if len(seg) < 3:
            rows.append({"구간": name, "거래일": len(seg), "비고": "관측 부족"})
            continue
        r = seg.pct_change().dropna()
        yrs = len(seg) / TRADING_DAYS
        vol = float(r.std(ddof=1) * np.sqrt(TRADING_DAYS))
        rows.append({"구간": name, "거래일": int(len(seg)),
                     "기간수익률": float((1 + r).prod() - 1),
                     "연율화": float((1 + r).prod() ** (1 / yrs) - 1)
                     if yrs > 0 else np.nan,
                     "연변동성": vol, "MDD": np.nan,
                     "Sharpe(rf=0)": np.nan,
                     "비고": "불연속 구간 - MDD 는 산출하지 않는다"})
    return pd.DataFrame(rows)


def capture_table(lv: pd.Series, proxy: pd.Series) -> pd.DataFrame:
    """앵커 대용의 일간 부호로 나눈 포착률. 고베타인지 비대칭인지 가른다."""
    pr = proxy.reindex(lv.index).ffill().pct_change()
    lr = lv.pct_change()
    df = pd.DataFrame({"idx": lr, "proxy": pr}).dropna()
    rows = []
    for sign, name in ((1, "앵커 대용 상승일"), (-1, "앵커 대용 하락일")):
        m = df[np.sign(df["proxy"]) == sign]
        if len(m) < 3:
            rows.append({"구분": name, "일수": len(m)})
            continue
        rows.append({
            "구분": name, "일수": int(len(m)),
            "지수 평균수익률": float(m["idx"].mean()),
            "대용 평균수익률": float(m["proxy"].mean()),
            "포착률": float(m["idx"].mean() / m["proxy"].mean())
            if m["proxy"].mean() != 0 else np.nan,
        })
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out/backtest")
    ap.add_argument("--drawdown", type=float, default=DRAWDOWN_TH)
    a = ap.parse_args()

    lv, px = _load(a.out)
    proxy = anchor_proxy(px)

    cal = calendar_table(lv)
    dd = drawdown_table(lv, proxy, a.drawdown)
    cap = capture_table(lv, proxy)

    os.makedirs(a.out, exist_ok=True)
    cal.to_csv(os.path.join(a.out, "regime_calendar.csv"),
               index=False, encoding="utf-8-sig")
    dd.to_csv(os.path.join(a.out, "regime_drawdown.csv"),
              index=False, encoding="utf-8-sig")
    cap.to_csv(os.path.join(a.out, "regime_capture.csv"),
               index=False, encoding="utf-8-sig")

    pd.set_option("display.width", 200)
    print("[캘린더 구간 ― 외부 사건으로 사전 정의]\n",
          cal.round(4).to_string(index=False))
    print("\n[앵커 대용 낙폭 구간 ― 기계적 판정]\n",
          dd.round(4).to_string(index=False))
    print("\n[상승·하락 포착률]\n", cap.round(4).to_string(index=False))
    print("\n[구간 정의] 우리 지수의 성과를 보고 자르지 않았다. 캘린더 경계는 "
          "코드 상수이며 결과를 보고 옮기지 않는다. 앵커 대용은 규칙 0으로 "
          "필수 편입되는 2종목이라 선정 판단의 결과가 아니다 - 다만 지수 "
          "구성종목이기도 하므로 완전 독립은 아니다.")
    print(f"[저장] {a.out}/regime_calendar.csv · regime_drawdown.csv "
          "· regime_capture.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
