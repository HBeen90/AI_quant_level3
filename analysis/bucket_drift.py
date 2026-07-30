# -*- coding: utf-8 -*-
"""
bucket_drift.py - 40/60 버킷 규정과 실제 지수의 괴리 계량
==========================================================
왜 필요한가
-----------
방법론은 "앵커 40% / 비앵커 60%"라고 말한다. 그런데 그 40%가 **실제 지수에서
지켜졌는지**를 잰 자료가 지금까지 없었다. 유일한 근거였던 verify_claims 의
c_bucket_drift 는 **합성 시나리오**(audit_review_claims.scenario_prices)에서
돌아간다. 즉 "40%가 34%로 흘러내린다"는 문장의 34%는 실제 지수의 숫자가 아니다.

이 스크립트는 실제 스냅샷 13회 · 실제 가격 패널로 같은 질문을 다시 던진다.
답은 예상과 다르다. 흘러내리는 것이 문제가 아니라 **애초에 40%에서 출발한
적이 거의 없다.**

세 층이 서로 다른 말을 한다
---------------------------
  (1) 방법론      앵커 40%
  (2) 희소 조항   비앵커의 상한 수용량이 60%에 못 미치면 잔여를 앵커가 흡수
                  -> 정기변경일 앵커 실현치가 85% / 67% / 64%
  (3) 운영 캡     월말 점검(30% 초과 -> 25%)이 정기변경 2거래일 뒤에 개입
                  -> 3종목 구간에서는 균등비중(33.33%)으로 되돌린다

셋 중 어느 것도 40%가 아니다. 발표에서 "앵커 40%"라고 말하면 세 층 모두와
어긋난다. 그래서 이 스크립트의 산출물은 방어 자료가 아니라 **개정 안건**이다.

무엇을 계산하는가
-----------------
1. 정기변경별 비앵커 수용량(폐쇄형) vs 엔진이 실제로 낸 앵커 비중
   두 경로가 독립이므로 일치하면 상호 검증이 된다. 어긋나면 즉시 중단한다
   (조용히 넘어가면 이 표 전체가 근거를 잃는다).

       수용량 = 핵심수 x 0.30 x 0.60 + min(위성수 x 0.25 x 0.60, 0.18)
       앵커   = 1 - min(수용량, 0.60)

2. 일별 버킷 경로. 이벤트 사이 가격 드리프트를 엔진과 같은 규칙으로 재현한다
   (w * (1+r) 후 정규화, 이벤트일에는 목표비중으로 치환).

3. 40% 대비 괴리의 분해. 둘은 성격이 전혀 다르므로 합쳐 말하면 안 된다.

       (앵커_당일 - 0.40) = (앵커_직전이벤트 - 0.40) + (앵커_당일 - 앵커_직전이벤트)
                            \_____ 구조 성분 _____/   \______ 드리프트 성분 ______/

   구조 성분은 규칙이 만든 것이라 가격이 멈춰도 사라지지 않는다.
   드리프트 성분만 리밸런싱 주기로 줄일 수 있다.

4. 구조적 실현가능 하한. 40/60 을 희소 조항 없이 만족시키려면 군별 최소
   몇 종목이 필요한가를 전수 탐색한다. 이 값이 min_constituents(5)보다 크면
   **하한 자체가 가중 규칙과 모순**이라는 뜻이다.

사용
----
    python analysis\\bucket_drift.py --snapshots data\\snapshots ^
        --prices-cache out\\px.csv --out out\\backtest
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import sys
import warnings

import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from src import weighting as W                              # noqa: E402
from src.rebalance import ANCHOR, CORE, SAT, ConfigV2       # noqa: E402

TOL = 1e-9
BAND = 0.05          # 규정 준수로 볼 허용 밴드(+-5%p) - 보고용 기준일 뿐 규칙 아님


# --------------------------------------------------------------------------- #
# 1. 폐쇄형 수용량 - 엔진과 독립인 두 번째 경로
# --------------------------------------------------------------------------- #
def nonanchor_capacity(n_core: int, n_sat: int) -> float:
    """비앵커가 개별·합계 상한을 모두 지키며 담을 수 있는 최대 지수비중.

    weighting.allocate 의 재배분 폭포를 **역으로 푼 값**이다. 엔진을 호출하지
    않고 파라미터만으로 계산하므로, 엔진 실현치와 맞으면 서로가 서로의 검증이
    된다. 상수를 weighting 에서 가져오는 이유: 여기에 숫자를 다시 적으면
    파라미터를 바꿨을 때 조용히 어긋난다.
    """
    core = n_core * W.CORE_CAP_BUCKET * (1.0 - W.ANCHOR_W)      # 개당 18%p
    sat = min(n_sat * W.SAT_CAP_BUCKET * (1.0 - W.ANCHOR_W),    # 개당 15%p
              W.SAT_TOTAL_CAP)                                  # 합계 18%p
    return core + sat


def predicted_anchor(n_core: int, n_sat: int) -> float:
    """희소 조항까지 반영한 앵커 버킷 예측치."""
    return 1.0 - min(nonanchor_capacity(n_core, n_sat), 1.0 - W.ANCHOR_W)


def binding_constraint(n_core: int, n_sat: int) -> str:
    """수용량을 실제로 묶은 제약이 무엇인지 이름을 붙인다.

    '수용량이 모자랐다'만으로는 무엇을 고쳐야 할지 알 수 없다. 개별 상한이
    묶은 것과 합계 상한이 묶은 것은 개정 방향이 정반대다. 특히 핵심군이 0이면
    개별 상한을 아무리 풀어도 위성 합계 상한(18%)이 천장이므로 60%에 닿을
    길이 없다 - 이 경우 손봐야 하는 것은 상한이 아니라 **선정 규칙**이다.
    """
    if nonanchor_capacity(n_core, n_sat) >= (1.0 - W.ANCHOR_W) - TOL:
        return "없음(40/60 성립)"
    if n_core == 0:
        return "핵심군 부재 - 위성 합계 상한(18%)이 천장, 상한 완화로는 해결 불가"
    sat_ind = n_sat * W.SAT_CAP_BUCKET * (1.0 - W.ANCHOR_W)
    if n_sat and sat_ind >= W.SAT_TOTAL_CAP - TOL:
        return "위성 합계 상한(18%)"
    return "개별 상한(핵심/위성 종목 수 부족)"


def feasible_min_composition(max_n: int = 12) -> dict:
    """40/60 을 희소 조항 없이 만족시키는 최소 구성을 전수 탐색.

    앵커도 개별 상한(25%)이 있으므로 40%를 담으려면 최소 2종목이 필요하다.
    비앵커는 위 수용량이 60% 이상이어야 한다.
    """
    best = None
    for na in range(1, max_n + 1):
        if na * W.ANCHOR_CAP < W.ANCHOR_W - TOL:
            continue                                   # 앵커 40% 수용 불가
        for nc in range(0, max_n + 1):
            for ns in range(0, max_n + 1):
                if nonanchor_capacity(nc, ns) < (1.0 - W.ANCHOR_W) - TOL:
                    continue
                tot = na + nc + ns
                if best is None or tot < best["종목수"]:
                    best = {"종목수": tot, "앵커": na, "핵심": nc, "위성": ns}
    return best


# --------------------------------------------------------------------------- #
# 2. 일별 버킷 경로 - 엔진과 동일한 드리프트 규칙
# --------------------------------------------------------------------------- #
def bucket_sums(w: pd.Series, groups: pd.Series) -> dict:
    g = groups.reindex(w.index)
    if g.isna().any():
        raise SystemExit(f"[중단] 군 매핑 누락: {g.index[g.isna()].tolist()} - "
                         "정기변경 스냅샷과 이벤트 종목이 어긋납니다")
    return {"앵커": float(w[g.eq(ANCHOR)].sum()),
            "핵심": float(w[g.eq(CORE)].sum()),
            "위성": float(w[g.eq(SAT)].sum())}


def daily_path(px: pd.DataFrame, events: list, snaps: dict) -> pd.DataFrame:
    """이벤트 사이를 가격으로 드리프트시켜 일별 버킷 비중을 재현한다.

    규칙은 AdhocManagerV2.drift 와 같다(w * (1+r) 후 합계 1 정규화). 별도
    구현인 이유는 엔진이 일별 비중을 내보내지 않기 때문이며, 같은 규칙을 쓰는
    한 결과는 엔진 내부 상태와 일치한다 - 이벤트일 비중이 목표비중과 같은지로
    매 이벤트마다 확인한다(아래 assert 경로).
    """
    ev = {pd.Timestamp(e["effective_date"]): e for e in events}
    rets = px.pct_change(fill_method=None)
    dates = px.index[px.index >= events[0]["effective_date"]]

    groups = None
    last_event_bucket = None
    last_event_date = None
    w = None
    rows = []
    for i, d in enumerate(dates):
        if w is not None and i > 0:
            r = rets.loc[d].reindex(w.index)
            if r.isna().any():
                raise SystemExit(f"[중단] {d.date()} 수익률 결측: "
                                 f"{r.index[r.isna()].tolist()} - 0% 대체 금지")
            w = w * (1.0 + r)
            w = w / w.sum()
        if d in ev:
            e = ev[d]
            if e["reason"] == "regular":
                if d not in snaps:
                    raise SystemExit(f"[중단] {d.date()} 정기변경 스냅샷 없음")
                groups = snaps[d].set_index("ticker")["group"]
            w = e["target_weights"].copy()
            last_event_bucket = bucket_sums(w, groups)["앵커"]
            last_event_date = d
        if w is None:
            continue
        b = bucket_sums(w, groups)
        rows.append({"date": d, "n": int(len(w)),
                     "앵커": b["앵커"], "핵심": b["핵심"], "위성": b["위성"],
                     "최대단일": float(w.max()),
                     "직전이벤트": last_event_date,
                     "직전이벤트_앵커": last_event_bucket,
                     "이벤트": ev[d]["reason"] if d in ev else ""})
    out = pd.DataFrame(rows).set_index("date")
    out["구조성분"] = out["직전이벤트_앵커"] - W.ANCHOR_W
    out["드리프트성분"] = out["앵커"] - out["직전이벤트_앵커"]
    return out


# --------------------------------------------------------------------------- #
# 3. 정기변경별 표 - 예측 vs 실현 교차검증 포함
# --------------------------------------------------------------------------- #
def review_table(events: list, snaps: dict, path: pd.DataFrame) -> pd.DataFrame:
    rows = []
    reg = [e for e in events if e["reason"] == "regular"]
    for e in reg:
        d = pd.Timestamp(e["effective_date"])
        w = e["target_weights"]
        g = snaps[d].set_index("ticker")["group"].reindex(w.index)
        na = int(g.eq(ANCHOR).sum())
        nc = int(g.eq(CORE).sum())
        ns = int(g.eq(SAT).sum())
        real = float(w[g.eq(ANCHOR)].sum())
        pred = predicted_anchor(nc, ns)
        if abs(pred - real) > 1e-9:
            raise SystemExit(
                f"[중단] {d.date()} 폐쇄형 예측({pred:.6f})과 엔진 실현"
                f"({real:.6f})이 다릅니다 - weighting 규칙이 바뀌었거나 이 "
                "스크립트의 수용량 식이 낡았습니다. 표 전체를 신뢰할 수 없습니다.")
        # 정기변경 이후 첫 캡 개입까지의 거래일. path 의 '직전이벤트'는 캡
        # 이벤트에서도 갱신되므로 구간으로 잘라 찾으면 안 된다(항상 비어 있다).
        after = path.index[(path.index > d) & (path["이벤트"] == "cap")]
        lag = (int(path.index.get_loc(after[0]) - path.index.get_loc(d))
               if len(after) else None)
        rows.append({
            "정기변경일": d.date(), "구성": na + nc + ns,
            "앵커수": na, "핵심수": nc, "위성수": ns,
            "비앵커수용량": nonanchor_capacity(nc, ns),
            "구속제약": binding_constraint(nc, ns),
            "앵커_규정": W.ANCHOR_W, "앵커_실현": real,
            "희소조항_흡수": max(real - W.ANCHOR_W, 0.0),
            "위성_실현": float(w[g.eq(SAT)].sum()),
            "최대단일": float(w.max()),
            "캡개입까지_거래일": lag,
        })
    return pd.DataFrame(rows)


def verify_path_reproduces_index(px: pd.DataFrame, events: list,
                                 level: pd.Series, tol: float = 1e-10) -> float:
    """재현한 비중 경로가 엔진 지수 계열과 같은 수익률을 내는지 확인한다.

    이 스크립트는 엔진 안의 일별 비중을 직접 꺼내지 못해 같은 규칙으로 다시
    굴린다. 그 재현이 틀리면 아래 모든 표가 '비슷하지만 다른 지수'의 숫자가
    된다 - 조용히 틀릴 수 있는 자리라서 명시적으로 막는다.

        지수수익률(d) = SUM_i w_i(d-1) x r_i(d)

    제수 방식으로 계산된 엔진 계열과 비중 방식으로 계산한 이 값이 일치하면,
    재현된 비중이 곧 엔진의 비중이다. 반환값은 최대 절대 오차.
    """
    ev = {pd.Timestamp(e["effective_date"]): e for e in events}
    rets = px.pct_change(fill_method=None)
    dates = px.index[px.index >= events[0]["effective_date"]]
    w = events[0]["target_weights"].copy()
    rows = {}
    for i, d in enumerate(dates):
        if i > 0:
            r = rets.loc[d].reindex(w.index)
            rows[d] = float((w * r).sum())
            w = w * (1.0 + r)
            w = w / w.sum()
        if d in ev:
            w = ev[d]["target_weights"].copy()
    mine = pd.Series(rows)
    eng = level.reindex(dates).pct_change().dropna()
    both = pd.concat([mine.rename("m"), eng.rename("e")], axis=1).dropna()
    if both.empty:
        raise SystemExit("[중단] 지수 계열과 겹치는 날짜가 없습니다")
    err = float((both["m"] - both["e"]).abs().max())
    if err > tol:
        raise SystemExit(
            f"[중단] 재현한 비중 경로가 엔진 지수와 어긋납니다(최대 {err:.3e}) - "
            "드리프트 규칙이 달라졌거나 이벤트 처리 순서가 바뀌었습니다. "
            "이 상태의 버킷 표는 다른 지수의 숫자입니다.")
    return err


def segment_table(path: pd.DataFrame) -> pd.DataFrame:
    """이벤트와 이벤트 사이 구간별 앵커 비중의 시작·끝·진폭."""
    rows = []
    for d, seg in path.groupby("직전이벤트", sort=True):
        rows.append({
            "구간시작": pd.Timestamp(d).date(),
            "구간끝": seg.index[-1].date(),
            "거래일": int(len(seg)),
            "이벤트": seg["이벤트"].iloc[0],
            "앵커_시작": float(seg["앵커"].iloc[0]),
            "앵커_끝": float(seg["앵커"].iloc[-1]),
            "앵커_최소": float(seg["앵커"].min()),
            "앵커_최대": float(seg["앵커"].max()),
            "드리프트_최대": float(seg["드리프트성분"].abs().max()),
        })
    return pd.DataFrame(rows).sort_values("구간시작").reset_index(drop=True)


# --------------------------------------------------------------------------- #
# 계산 진입점 - main 과 verify_claims 가 같은 경로를 쓴다
# --------------------------------------------------------------------------- #
def measure(snapshots_dir: str, prices_cache: str) -> dict:
    """정기변경 표 · 일별 경로 · 실현가능 하한을 한 번에 낸다.

    verify_claims 클레임과 CLI 가 **같은 함수**를 쓰게 해 둔다. 두 곳에서
    따로 계산하면 발표 문장과 산출 CSV가 조용히 갈라진다.
    """
    from analysis.run_backtest import load_snapshots
    from backtest.backtest import build_event_schedule, simulate_index

    px = pd.read_csv(prices_cache, index_col=0, parse_dates=True)
    px.columns = [str(c).strip().zfill(6) for c in px.columns]
    snaps = {pd.Timestamp(k): v for k, v in load_snapshots(snapshots_dir).items()}
    cfg = ConfigV2()
    # 희소 조항·핵심군 부재 로그는 여기서 '이상'이 아니라 **측정 대상**이다.
    # 같은 사실을 아래 표가 종목수와 함께 정확히 내므로, 콘솔에 수십 줄을
    # 흘려 표를 밀어내지 않는다(엔진 자체의 로그 설정은 건드리지 않는다).
    wlog = logging.getLogger(W.__name__)
    prev_level = wlog.level
    wlog.setLevel(logging.ERROR)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")   # 하한 미달 경고는 아래에서 따로 센다
            events, _ = build_event_schedule(px, snaps, {}, cfg=cfg)
            bt = simulate_index(px, events, base=1000.0, mode="pr")
    finally:
        wlog.setLevel(prev_level)

    err = verify_path_reproduces_index(px, events, bt["level"])
    path = daily_path(px, events, snaps)
    rev = review_table(events, snaps, path)
    st = float(path["구조성분"].abs().mean())
    dr = float(path["드리프트성분"].abs().mean())
    return {
        "px": px, "events": events, "snaps": snaps, "cfg": cfg,
        "path": path, "reviews": rev, "segments": segment_table(path),
        "재현오차": err,
        "앵커_시간가중평균": float(path["앵커"].mean()),
        "밴드내_비율": float(path["앵커"].sub(W.ANCHOR_W).abs().le(BAND).mean()),
        "구조성분": st, "드리프트성분": dr,
        "구조_비중": st / (st + dr) if st + dr > 0 else float("nan"),
        "규정충족_회차": int((rev["희소조항_흡수"] <= TOL).sum()),
        "총_회차": int(len(rev)),
        "실현가능_최소구성": feasible_min_composition(),
    }


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description="40/60 버킷 규정 대비 실측 괴리")
    ap.add_argument("--snapshots", default=os.path.join(HERE, "data", "snapshots"))
    ap.add_argument("--prices-cache", default=os.path.join(HERE, "out", "px.csv"))
    ap.add_argument("--out", default=os.path.join(HERE, "out", "backtest"))
    a = ap.parse_args()

    m = measure(a.snapshots, a.prices_cache)
    cfg, path, rev, seg = m["cfg"], m["path"], m["reviews"], m["segments"]
    print(f"[검증] 재현 비중 경로 vs 엔진 지수 계열 최대 오차 {m['재현오차']:.2e}"
          " - 동일 지수 확인")
    os.makedirs(a.out, exist_ok=True)
    pd.set_option("display.width", 240)

    print("=" * 78)
    print("1. 정기변경일 - 앵커 40%는 지켜졌는가 (폐쇄형 예측 vs 엔진 실현 대조)")
    print("=" * 78)
    show = rev.drop(columns=["구속제약"]).copy()
    for c in ("비앵커수용량", "앵커_규정", "앵커_실현", "희소조항_흡수",
              "위성_실현", "최대단일"):
        show[c] = (show[c] * 100).round(2)
    print(show.to_string(index=False))
    held = int((rev["희소조항_흡수"] <= TOL).sum())
    print(f"\n  앵커 40% 실현: {len(rev)}회 중 {held}회"
          f" ({held / len(rev) * 100:.0f}%)")
    print("  나머지는 희소 조항 - 비앵커 수용량이 60%에 못 미쳐 앵커가 잔여를 흡수.")
    print("  즉 40%에서 흘러내린 것이 아니라 40%에서 출발한 적이 없다.")
    print("\n  [무엇이 수용량을 묶었는가] - 개정 방향이 여기서 갈린다")
    for k, v in rev["구속제약"].value_counts().items():
        print(f"    {v:2d}회  {k}")

    print("\n" + "=" * 78)
    print("2. 일별 경로 - 규정 대비 괴리의 분해")
    print("=" * 78)
    n = len(path)
    inband = float(path["앵커"].sub(W.ANCHOR_W).abs().le(BAND).mean())
    print(f"  표본 {n}거래일 ({path.index[0].date()} ~ {path.index[-1].date()})")
    print(f"  앵커 시간가중평균 {path['앵커'].mean():.2%} "
          f"(중앙 {path['앵커'].median():.2%} · "
          f"최소 {path['앵커'].min():.2%} · 최대 {path['앵커'].max():.2%})")
    print(f"  규정 {W.ANCHOR_W:.0%} 대비 평균 괴리 "
          f"{path['앵커'].sub(W.ANCHOR_W).mean():+.2%}p")
    print(f"  +-{BAND:.0%}p 밴드 안에 머문 거래일 {inband:.1%}")
    st = float(path["구조성분"].abs().mean())
    dr = float(path["드리프트성분"].abs().mean())
    print(f"\n  [분해] 평균 |구조 성분| {st:.2%}p  ·  평균 |드리프트 성분| {dr:.2%}p"
          f"  (구조가 괴리의 {st / (st + dr):.0%})")
    print("  구조 성분 = 규칙이 마지막 리셋에서 정한 값과 40%의 차. 가격이 멈춰도 남는다.")
    print("  드리프트 성분 = 그 리셋 이후 가격이 움직인 몫. 리셋 주기로만 줄어든다.")
    print(f"  -> 이 표본에서 리밸런싱 주기로 줄일 수 있는 몫은 {dr:.2%}p뿐이다."
          "\n     드리프트 트리거를 먼저 도입해도 괴리는 거의 그대로 남는다"
          "(원인이 가격이 아니라 규칙이므로).\n     다만 아래 [40/60 이 성립한 "
          "구간] 을 함께 볼 것 - 순서의 문제이지 불필요하다는 뜻이 아니다.")

    # 구조가 압도한다고 해서 드리프트가 무해하다는 뜻은 아니다. 구조 문제가
    # 없는 구간만 떼어 보면 드리프트는 곧바로 커진다 - 한쪽만 말하면 왜곡이다.
    okrev = rev.loc[rev["희소조항_흡수"] <= TOL, "정기변경일"]
    clean = path[path["직전이벤트"].isin([pd.Timestamp(d) for d in okrev])]
    if len(clean):
        print(f"\n  [40/60 이 성립한 구간만 따로] {len(clean)}거래일 · "
              f"앵커 {clean['앵커'].iloc[0]:.2%} -> {clean['앵커'].iloc[-1]:.2%} "
              f"(구간 내 최대 {clean['앵커'].max():.2%})")
        print(f"    이 구간 드리프트 최대 {clean['드리프트성분'].abs().max():.2%}p"
              " - 구조 문제가 없어지면 드리프트는 곧바로 커진다.")
        print("    표본 평균이 작은 것은 드리프트가 무해해서가 아니라 3종목 구간에서"
              "\n    월간 캡이 매달 되돌렸기 때문이다. 두 사실을 같이 말할 것.")

    print("\n  [규정 위반 상태로 보낸 거래일]")
    print(f"    위성 합계 {W.SAT_TOTAL_CAP:.0%} 초과 "
          f"{path['위성'].gt(W.SAT_TOTAL_CAP + TOL).mean():.1%}")
    print(f"    개별 앵커 상한 {W.ANCHOR_CAP:.0%} 초과(최대단일 기준) "
          f"{path['최대단일'].gt(W.ANCHOR_CAP + TOL).mean():.1%}")
    print("    주의: 두 상한은 정기변경 시점 규칙이고 기중 감시 대상이 아니다."
          "\n    그래서 '위반'이 아니라 '규칙이 기중을 보지 않는다'가 정확한 표현이다.")

    print("\n" + "=" * 78)
    print("3. 운영 캡의 개입 - 40/60 을 되돌리는 것이 아니라 균등화한다")
    print("=" * 78)
    lag = rev["캡개입까지_거래일"].dropna()
    ncap = int((path["이벤트"] == "cap").sum())
    print(f"  캡 이벤트 {ncap}회 · 정기변경 후 첫 캡 개입까지 "
          f"중앙 {lag.median():.0f}거래일 (최소 {lag.min():.0f} · 최대 {lag.max():.0f})")
    nolag = rev[rev["캡개입까지_거래일"].isna()]
    if len(nolag):
        print("  캡이 한 번도 걸리지 않은 정기변경: "
              + ", ".join(str(x) for x in nolag["정기변경일"])
              + "\n  -> 40/60 이 성립한 유일한 회차와 같다. 우연이 아니라 같은 원인"
                "(종목 수)의 두 얼굴이다.")
    three = path[path["n"] == 3]
    if len(three):
        print(f"  3종목 구간 {len(three)}거래일 ({len(three) / n:.1%}) - "
              f"이 구간 캡 이벤트 직후 최대단일 "
              f"{path.loc[(path['n'] == 3) & (path['이벤트'] == 'cap'), '최대단일'].max():.4%}")
        print("  3종목에서 균등비중은 33.33%다. 개별 상한 25%는 산술적으로 도달"
              "할 수 없고,\n  점검 트리거 30%도 마찬가지다. 캡은 매달 발동하고 매달"
              " 해소되지 않는다.")

    print("\n" + "=" * 78)
    print("4. 구조적 실현가능 하한 - 5종목 하한은 가중 규칙과 정합한가")
    print("=" * 78)
    best = feasible_min_composition()
    print(f"  40/60 을 희소 조항 없이 만족시키는 최소 구성: "
          f"{best['종목수']}종목 (앵커 {best['앵커']} · 핵심 {best['핵심']} · "
          f"위성 {best['위성']})")
    print(f"  현행 하한 min_constituents = {cfg.min_constituents}")
    if best["종목수"] > cfg.min_constituents:
        print(f"  -> 하한({cfg.min_constituents})이 실현가능 하한"
              f"({best['종목수']})보다 낮다. 하한을 지켜도 40/60 은 깨진다."
              "\n     두 규칙이 서로 다른 최소치를 전제하고 있다는 뜻이므로, "
              "하한만 손보는 것으로는\n     해결되지 않는다(가중 규칙과 같이 봐야 한다).")
    n_cap = math.ceil(1.0 / W.ANCHOR_CAP)
    print(f"  개별 상한 {W.ANCHOR_CAP:.0%} 가 균등비중에서 도달 가능한 최소 "
          f"종목수: {n_cap}종목")
    print(f"  월간 점검 트리거 30% 가 균등비중에서 도달 가능한 최소 종목수: "
          f"{math.ceil(1.0 / 0.30)}종목")

    ok_rev = int((rev["구성"] >= best["종목수"]).sum())
    print(f"\n  [하한을 {best['종목수']}종목으로 올리면] 13회 중 {ok_rev}회만 "
          f"충족 - 나머지 {len(rev) - ok_rev}회는 산출 자체가 불가해진다.")
    print("  즉 하한 상향도 해법이 아니다. 남는 선택지는 두 가지뿐이다."
          "\n    (a) 가중 규칙을 고친다 (위성 합계 상한 · 핵심군 요건)"
          "\n    (b) 방법론 문구를 실제 규칙에 맞춘다 "
          "(40%는 목표치이고 수용량 제약이 우선한다고 명시)")

    rev.to_csv(os.path.join(a.out, "bucket_drift_reviews.csv"),
               index=False, encoding="utf-8-sig")
    seg.to_csv(os.path.join(a.out, "bucket_drift_segments.csv"),
               index=False, encoding="utf-8-sig")
    path.drop(columns=["직전이벤트_앵커"]).to_csv(
        os.path.join(a.out, "bucket_drift_daily.csv"), encoding="utf-8-sig")
    print(f"\n저장: {a.out}/ (bucket_drift_reviews.csv · bucket_drift_segments.csv"
          " · bucket_drift_daily.csv)")
    print("\n이 산출물은 방어 자료가 아니라 개정 안건이다 - "
          "docs/버킷규정_개정안.md 참조.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
