# -*- coding: utf-8 -*-
"""
buffer_binding_report.py - 히스테리시스 버퍼가 '실제로 일을 했는가' 진단
========================================================================
왜 이 스크립트가 필요한가
-------------------------
out/backtest/policy_comparison.csv 는 none/narrow/mid/wide 네 행이 회전율·
CAGR·MDD까지 소수점 끝자리가 같다. 이 표만 보면 두 가지 해석이 갈리지 않는다.

    (a) 코드가 --policy 를 읽지 않는 배선 오류
    (b) 표본 안에서 유지 임계값이 판정에 개입할 기회가 아예 없었던 정상 동작

발표에서 "정책이 코드에 안 붙은 것 아니냐"는 질문을 받으면 표로는 답할 수 없다.
이 스크립트는 (b)임을 **직접 세어** 증명하고, 왜 0건인지까지 원장 데이터로
설명한다. 가격 데이터·백테스트 엔진이 필요 없어 스냅샷만 있으면 돌아간다.

무엇을 출력하는가
-----------------
  1. 정책별·시점별 버퍼 발동 건수 (핵심 산출물)
  2. 발동 0건의 원인 - 편입 종목 판정값의 단조성 검사
       버퍼는 '기존 종목의 값이 신규 기준 아래로 **떨어졌을 때**' 작동한다.
       값이 한 번도 떨어지지 않으면 정의상 발동할 수 없다.
  3. 반증 실험 - 판정값을 인위적으로 떨어뜨리면 버퍼가 작동하는가
       규칙이 사문(死文)인지, 표본이 조용했던 것인지를 가른다.

사용
----
    python analysis/buffer_binding_report.py --snapshots data/snapshots \
        --out out/backtest
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys
import warnings

import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from src.rebalance import (  # noqa: E402
    ANCHOR, BUFFER_POLICIES, CORE, SAT, ConfigV2, buffer_binding, select_v2,
)

SNAP_RE = re.compile(r"snapshot_(\d{8})\.csv$")


def load_snapshots(d: str) -> dict:
    """스냅샷 디렉터리 -> {날짜: DataFrame}. 종목코드는 문자열 고정."""
    out = {}
    for p in sorted(glob.glob(os.path.join(d, "snapshot_*.csv"))):
        m = SNAP_RE.search(os.path.basename(p))
        if not m:
            continue
        df = pd.read_csv(p, dtype={"ticker": str})
        df["ticker"] = df["ticker"].str.strip().str.zfill(6)
        for c in ("exposure", "mem_ratio", "float_mcap"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["eligible"] = (df["eligible"].astype(str).str.strip().str.lower()
                          .isin({"true", "1", "y", "yes", "참"}))
        out[pd.Timestamp(m.group(1))] = df
    if not out:
        raise SystemExit(f"[중단] 스냅샷을 찾지 못했습니다: {d}")
    return out


def binding_by_policy(snaps: dict) -> pd.DataFrame:
    """정책 4안 × 정기변경 시점의 버퍼 발동 건수.

    prev_members 는 직전 정기변경의 확정 구성을 쓴다(수시편출 미반영 - 이
    스크립트는 규칙 계층만 본다). 최초 시점은 기존 종목이 없으므로 대상 0.
    """
    rows = []
    for name in BUFFER_POLICIES:
        cfg = ConfigV2.with_policy(name)
        prev: set = set()
        for d in sorted(snaps):
            b = buffer_binding(snaps[d], prev, cfg)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                sel = select_v2(snaps[d], prev, cfg)
            rows.append({
                "정책": name,
                "유지선": f"{cfg.hold_core:.2f}/{cfg.hold_sat:.2f}",
                "심사일": d.date(),
                "구성 종목수": sel["n"],
                "기존 비앵커": int(len(b)),
                "버퍼발동": int(b["binding"].sum()) if len(b) else 0,
                "발동 종목": ",".join(b.loc[b["binding"], "ticker"])
                if len(b) else "",
            })
            prev = set(sel["members"]["ticker"])
    return pd.DataFrame(rows)


def monotonicity_report(snaps: dict) -> pd.DataFrame:
    """편입 이력이 있는 종목의 판정값이 시간에 따라 떨어진 적이 있는가.

    버퍼는 '값이 떨어진 기존 종목'을 붙잡는 장치다. 값이 단조 비감소면
    붙잡을 대상이 생기지 않는다 - 발동 0건의 직접 원인이 여기서 드러난다.

    주의: 판정 지표는 군에 따라 다르다(핵심=exposure, 위성=mem_ratio). 군이
    바뀌는 시점의 값을 그냥 이어 붙이면 지표가 갈아끼워진 것을 '하락'으로
    잘못 센다 - 예: 한미반도체는 위성(mem_ratio 0.80) -> 핵심(exposure 0.35)로
    올라섰는데 숫자만 보면 0.80 -> 0.35 하락으로 보인다. 승격을 하락으로
    읽으면 결론이 정반대가 되므로, **같은 군이 이어진 구간 안에서만** 비교하고
    군 전이는 따로 센다.
    """
    dates = sorted(snaps)
    ever: set = set()
    prev: set = set()
    for d in dates:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            sel = select_v2(snaps[d], prev, ConfigV2())
        prev = set(sel["members"]["ticker"])
        ever |= prev

    rows = []
    for t in sorted(ever):
        obs = []                       # [(group, metric, value)]
        name = ""
        for d in dates:
            s = snaps[d]
            r = s[s["ticker"] == t]
            if r.empty:
                continue
            r = r.iloc[0]
            name = r["name"]
            metric = "exposure" if r["group"] in (ANCHOR, CORE) else "mem_ratio"
            obs.append((r["group"], metric, float(r[metric])))
        drops = sum(1 for a, b in zip(obs, obs[1:])
                    if a[0] == b[0] and b[2] < a[2] - 1e-12)   # 같은 군끼리만
        shifts = sum(1 for a, b in zip(obs, obs[1:]) if a[0] != b[0])
        vals = [v[2] for v in obs]
        rows.append({
            "ticker": t,
            "종목명": name,
            "관측 수": len(obs),
            "판정지표": "/".join(dict.fromkeys(v[1] for v in obs)),
            "첫 값": vals[0] if vals else float("nan"),
            "끝 값": vals[-1] if vals else float("nan"),
            "군 전이": shifts,
            "동일군 내 하락": drops,
            "단조 비감소": drops == 0,
        })
    return pd.DataFrame(rows)


def falsification_test(snaps: dict) -> pd.DataFrame:
    """반증 실험 - 마지막 심사일의 판정값을 1~5%p 떨어뜨리면 버퍼가 작동하는가.

    발동 0건을 그대로 두면 "규칙이 사문 아니냐"는 반문이 남는다. 값을 인위적으로
    낮췄을 때 정책별 구성 종목 수가 갈라지면, 규칙은 살아 있고 표본이 조용했던
    것임이 증명된다. (실제 지수 산출과 무관한 진단 전용 조작)
    """
    dates = sorted(snaps)
    prev: set = set()
    for d in dates[:-1]:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            prev = set(select_v2(snaps[d], prev, ConfigV2())["members"]["ticker"])

    last = snaps[dates[-1]]
    rows = []
    for shock in (0.0, 0.01, 0.02, 0.03, 0.04, 0.05):
        s = last.copy()
        inc = s["ticker"].isin(prev)
        s.loc[inc & (s["group"] == CORE), "exposure"] -= shock
        s.loc[inc & (s["group"] == SAT), "mem_ratio"] -= shock
        row = {"판정값 충격(%p)": round(shock * 100, 1)}
        for name in BUFFER_POLICIES:
            cfg = ConfigV2.with_policy(name)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                sel = select_v2(s, prev, cfg)
            b = buffer_binding(s, prev, cfg)
            row[f"{name} 종목수"] = sel["n"]
            row[f"{name} 발동"] = int(b["binding"].sum()) if len(b) else 0
        rows.append(row)
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="히스테리시스 버퍼 발동 진단")
    ap.add_argument("--snapshots", default="data/snapshots")
    ap.add_argument("--out", default="out/backtest")
    a = ap.parse_args()

    snaps = load_snapshots(a.snapshots)
    os.makedirs(a.out, exist_ok=True)
    pd.set_option("display.width", 200)

    tbl = binding_by_policy(snaps)
    tot = int(tbl["버퍼발동"].sum())
    print(f"== 1. 버퍼 발동 건수 (정책 4안 × 심사 {len(snaps)}회) ==")
    print(tbl.groupby("정책", sort=False)[["버퍼발동"]].sum().to_string())
    print(f"\n전 정책 합계 발동: {tot}건")
    tbl.to_csv(os.path.join(a.out, "buffer_binding_by_policy.csv"),
               index=False, encoding="utf-8-sig")

    mono = monotonicity_report(snaps)
    print("\n== 2. 발동 0건의 원인 - 편입 종목 판정값의 단조성 ==")
    print(mono.to_string(index=False))
    n_drop = int((~mono["단조 비감소"]).sum())
    print(f"\n판정값이 한 번이라도 떨어진 편입 종목: {n_drop}종목 / {len(mono)}종목")
    if n_drop == 0:
        print("  -> 편입 종목의 판정값이 전 구간 단조 비감소다. 버퍼는 '값이 떨어진"
              "\n     기존 종목'을 붙잡는 장치이므로, 붙잡을 대상 자체가 없었다."
              "\n     정책 4안이 같은 수치를 내는 것은 이 사실의 산술적 귀결이다.")
    mono.to_csv(os.path.join(a.out, "buffer_monotonicity.csv"),
                index=False, encoding="utf-8-sig")

    fals = falsification_test(snaps)
    print("\n== 3. 반증 실험 - 판정값을 낮추면 정책이 갈라지는가 ==")
    print(fals.to_string(index=False))
    split = fals.filter(like="종목수").nunique(axis=1).max() > 1
    print("\n  -> 정책 간 구성이 갈라짐: "
          f"{'예 - 규칙은 살아 있고 표본이 조용했던 것' if split else '아니오 - 추가 조사 필요'}")
    fals.to_csv(os.path.join(a.out, "buffer_falsification.csv"),
                index=False, encoding="utf-8-sig")

    print(f"\n저장: {a.out}/ (buffer_binding_by_policy.csv · "
          "buffer_monotonicity.csv · buffer_falsification.csv)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
