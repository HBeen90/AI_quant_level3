# -*- coding: utf-8 -*-
"""
backtest/backtest.py — HBM 밸류체인 지수: 이벤트 소비형 백테스트 엔진
====================================================================
파이프라인(팀 합의): rebalance.py -> 이벤트 목록 -> backtest.py
  * rebalance.py  : 규칙 계층. 가격을 모르며 event만 생산.
  * backtest.py   : 이벤트 스케줄 생성(가격 필요 구간) + 지수 재생 + 지표.
  * 제수(divisor) 모듈은 건드리지 않는다. "제수와 수학적 동치"는 정기·수시·
    캡 이벤트가 '모두' 이벤트 목록에 포함될 때만 성립하며, 그 포함을
    build_event_schedule()이 담당한다.

가격 데이터 계약 (PR/TR — 제6조)
--------------------------------
prices : 배당 미반영 수정주가여야 한다(무상증자·액면분할 등 주식수 이벤트만
  반영, 현금배당 미반영). 공급처의 "수정주가"가 배당 재투자까지 반영한
  총수익 기준이면 PR 백테스트가 TR처럼 부풀려지므로 반드시 확인할 것.
dividends : (선택) 주당 현금배당, 배당락일 기준, prices와 동일 축.
  mode="pr"       -> dividends 무시 (기본)
  mode="gross_tr" -> 배당락일에 세전 재투자: r_i = Δp/p + dps/p_prev (제6조)

지표 산출 원칙: 회전율의 유일한 공식 수치는 bt["turnover"]다. 목표비중끼리의
비교는 drift를 놓치므로 회전율로 보고하지 않는다(리뷰 반영).
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.rebalance import (  # noqa: E402
    AdhocManagerV2, ConfigV2, MethodologyReviewRequired, monitor,
    regular_rebalance_v2,
)


def make_event(date, reason: str, weights: pd.Series) -> dict:
    """이벤트 인터페이스(팀 합의 규격) — v1과 동일."""
    w = weights.astype(float)
    return {"effective_date": pd.Timestamp(date), "reason": reason,
            "target_weights": w / w.sum()}

TRADING_DAYS = 252


# ----------------------------------------------------------------------
# 1) 이벤트 스케줄 생성 — v8 전체 방법론(정기 + 월간 캡 + 수시변경) 연결
# ----------------------------------------------------------------------
def build_event_schedule(prices: pd.DataFrame,
                         review_snapshots: dict,
                         adhoc_exclusions: dict | None = None,
                         cfg: ConfigV2 = ConfigV2()) -> tuple:
    """v2 이벤트 스케줄: 정기변경 + 무대체 수시편출(D+2) + 월간 캡(D+2).

    review_snapshots : {정기변경 시행일: 심사 스냅샷 DataFrame}
    adhoc_exclusions : {공지일: [(ticker, 사유), ...]} -> 공지일 기준 D+2에 편출 집행
    원칙:
      - 편출 후 대체 편입 없음. 편출 결과 5종목 미만이면
        MethodologyReviewRequired 전파(산출 중단).
      - 월말 점검에서 30% 초과 시 25% 캡을 D+2에 집행(README 3장 ①).
      - 구성 변경(정기·편출) 발생 시 기존 캡 예약은 전부 취소하고 새 구성
        기준으로 재점검·재예약한다(예약이 낡은 구성을 되살리지 않도록).
    반환 : (events 리스트[날짜순], 정기변경 교체 이력 DataFrame)
    """
    adhoc = {pd.Timestamp(k): v for k, v in (adhoc_exclusions or {}).items()}
    snaps = {pd.Timestamp(k): v for k, v in review_snapshots.items()}
    reg_dates = sorted(snaps)
    hist: list = []

    rets = prices.pct_change(fill_method=None)
    dates = prices.index[prices.index >= reg_dates[0]]
    is_month_end = pd.Series(dates, index=dates).groupby(
        [dates.year, dates.month]).transform("max") == pd.Series(dates, index=dates)

    events: list = []
    vm = None
    pending_caps: dict = {}          # {집행일: 목표비중}
    pending_excl: dict = {}          # {집행일: [(ticker, 사유), ...]}

    def recheck_and_book_cap(i: int) -> None:
        pending_caps.clear()
        if vm is None:
            return
        adj, changed = monitor(vm.weights, cfg)
        if changed and i + 2 < len(dates):
            pending_caps[dates[i + 2]] = adj

    for i, d in enumerate(dates):
        if vm is not None:
            vm.drift(rets.loc[d])

        if d in snaps:                                  # 정기변경 (지연 계산)
            # [리뷰 P1-1] prev_members는 사전 계산된 직전 정기변경 결과가 아니라
            # '시행일 현재 실제 운영 구성'이다 - 기중 편출 종목은 비구성종목이므로
            # 유지 임계값이 아닌 신규 임계값(30%/70%)을 적용받는다.
            prev = set(vm.weights.index) if vm is not None else set()
            # [리뷰 P1-2] 정기변경일과 수시편출 D+2가 겹치면 하드 편출을 스냅샷에
            # 먼저 반영하고 최종 목표비중 이벤트 '하나'로 원자 병합한다.
            # 이벤트를 둘로 나누면 회전율이 이중 계상되고, 기존 코드처럼
            # pending_excl.clear()만 하면 상장폐지 대상이 바스켓에 남는다.
            same_day = pending_excl.pop(d, [])
            drop = {t for t, _ in same_day}
            snap = snaps[d]
            if drop:
                snap = snap[~snap["ticker"].isin(drop)]
                prev -= drop
            res = regular_rebalance_v2(snap, prev, cfg)
            hist.append({"date": d, "n": res["n"], "added": sorted(res["added"]),
                         "dropped": sorted(res["dropped"] | drop)})
            vm = AdhocManagerV2(res["weights"].copy(),
                                res["members"].set_index("ticker")["group"].copy(),
                                cfg)
            events.append(make_event(d, "regular", vm.weights))
            pending_excl.clear()
            recheck_and_book_cap(i)
            continue

        if d in adhoc and i + 2 < len(dates):           # 편출 공지 -> D+2 예약
            pending_excl.setdefault(dates[i + 2], []).extend(adhoc[d])

        if vm is not None and d in pending_excl:        # 편출 집행 (무대체)
            batch = [(t, r) for t, r in pending_excl.pop(d)
                     if t in vm.weights.index]
            if batch:
                vm.apply_exclusions(batch)              # 5 미만 -> 예외 전파
                events.append(make_event(d, "exclusion", vm.weights))
                recheck_and_book_cap(i)                 # 예약 무효화 + 재점검
                continue

        if vm is not None and d in pending_caps:        # 월간 캡 집행
            tgt = pending_caps.pop(d)
            tgt = tgt[tgt.index.intersection(vm.weights.index)]
            vm.weights = (tgt / tgt.sum()).copy()
            events.append(make_event(d, "cap", vm.weights))

        if vm is not None and bool(is_month_end.loc[d]):  # 월말 점검 -> D+2 예약
            recheck_and_book_cap(i)

    return events, pd.DataFrame(hist)


def simulate_index(prices: pd.DataFrame, events: list | dict,
                   base: float = 1000.0, mode: str = "pr",
                   ordinary_dividends: pd.DataFrame | None = None) -> pd.DataFrame:
    """이벤트 목록으로 지수 레벨을 재생한다.

    events : make_event() 규격 리스트(권장) 또는 {날짜: 비중} dict(하위호환)
    반환 : DataFrame[level, turnover, reason]
      turnover = 이벤트 당일 편도 회전율 = 0.5 * Σ|목표 - drift 후 비중|
    """
    if isinstance(events, dict):
        events = [make_event(k, "regular", v) for k, v in events.items()]
    ev: dict = {}
    for e in sorted(events, key=lambda x: x["effective_date"]):
        ev.setdefault(e["effective_date"], []).append(e)

    rets = prices.pct_change(fill_method=None)
    px_rets = prices.pct_change(fill_method=None)  # 비중 drift 전용(항상 가격만)
    if mode == "gross_tr":
        if ordinary_dividends is None:
            raise ValueError("mode='gross_tr'에는 ordinary_dividends가 필요합니다"
                             "(제6조). 보통배당(주당, 락일 기준)만 넣을 것 — "
                             "특별배당·자본환급은 PR 제수 조정으로 반영되므로 "
                             "포함 시 이중반영이 된다.")
        dy = ordinary_dividends.reindex_like(prices).fillna(0.0) / prices.shift(1)
        rets = rets + dy.fillna(0.0)                            # 락일 세전 재투자
    elif ordinary_dividends is not None:
        raise ValueError("mode='pr'에서는 ordinary_dividends를 넣지 마십시오 - "
                         "가격이 배당 미반영인지 계약을 먼저 확인할 것.")

    dates = prices.index[prices.index >= min(ev)]
    level = pd.Series(index=dates, dtype=float)
    tno = pd.Series(0.0, index=dates)
    reason = pd.Series("", index=dates)
    event_log: list = []                    # 이벤트 '건별' 회전율 (감사 원장용)
    w, lvl = None, base
    for d in dates:
        if w is not None:
            raw_px = prices.loc[d].reindex(w.index)
            bad_px = raw_px.index[raw_px.isna() | (raw_px <= 0)]
            if len(bad_px):
                raise ValueError(
                    f"Active constituent price missing/invalid on {pd.Timestamp(d).date()}: "
                    f"{bad_px.tolist()} (not treated as a 0% return)")
            r = rets.loc[d].reindex(w.index)
            if r.isna().any():
                raise ValueError(
                    f"Active constituent prior price missing on {pd.Timestamp(d).date()}: "
                    f"{r.index[r.isna()].tolist()}")
            lvl *= 1.0 + float((w * r).sum())
            # 비중 drift는 '가격' 수익률만 사용한다(제6조): Gross TR의 배당은
            # 바스켓 전체에 비례 재투자되므로 상대비중에 중립이며, 리셋 시의
            # 실매매(회전율)는 가격 drift 기준으로 계상되어야 한다.
            pr = px_rets.loc[d].reindex(w.index)
            if pr.isna().any():
                raise ValueError(
                    f"Active constituent price return missing on "
                    f"{pd.Timestamp(d).date()}: {pr.index[pr.isna()].tolist()}")
            w = w * (1.0 + pr)
            w = w / w.sum()
        if d in ev:
            for seq, e in enumerate(ev[d]):
                tgt = e["target_weights"]
                event_px = prices.loc[d].reindex(tgt.index)
                bad_event_px = event_px.index[event_px.isna() | (event_px <= 0)]
                if len(bad_event_px):
                    raise ValueError(
                        f"Event constituent price missing/invalid on {pd.Timestamp(d).date()}: "
                        f"{bad_event_px.tolist()}")
                t_ev = 0.0
                if w is not None:
                    u = tgt.reindex(tgt.index.union(w.index), fill_value=0.0)
                    v = w.reindex(u.index, fill_value=0.0)
                    t_ev = 0.5 * float((u - v).abs().sum())
                    tno[d] += t_ev
                event_log.append({"effective_date": d, "seq": seq,
                                  "reason": e["reason"],
                                  "one_way_turnover": t_ev,
                                  "n_members": len(tgt)})
                w = tgt
                reason[d] = (reason[d] + "+" + e["reason"]).strip("+")
        level[d] = lvl
    out = pd.DataFrame({"level": level, "turnover": tno, "reason": reason})
    out.attrs["event_log"] = pd.DataFrame(event_log)   # 같은 날 복수 이벤트 분리 기록
    return out


# ----------------------------------------------------------------------
# 3) 성과지표 (수익률·변동성·MDD·회전율·상관계수)
# ----------------------------------------------------------------------
def cagr(level: pd.Series) -> float:
    yrs = (level.index[-1] - level.index[0]).days / 365.25
    return float((level.iloc[-1] / level.iloc[0]) ** (1 / yrs) - 1) if yrs > 0 else np.nan


def ann_vol(level: pd.Series) -> float:
    return float(level.pct_change().dropna().std(ddof=1) * np.sqrt(TRADING_DAYS))


def max_drawdown(level: pd.Series) -> dict:
    peak = level.cummax()
    dd = level / peak - 1.0
    t = dd.idxmin()
    return {"mdd": float(dd.min()), "peak": level.loc[:t].idxmax(), "trough": t}


def annualized_turnover(bt: pd.DataFrame) -> float:
    """연율화 편도 회전율 = 총 회전율 / 실제 경과연수 — 공식 수치는 이것이다.
    달력연도별 합계(annual_turnover)를 단순 평균하면 부분 연도(예: 6월~익년
    6월)가 두 조각으로 갈라져 절반으로 과소계상되므로 요약치로 쓰지 않는다."""
    yrs = (bt.index[-1] - bt.index[0]).days / 365.25
    return float(bt["turnover"].sum() / yrs) if yrs > 0 else np.nan


def annual_turnover(bt: pd.DataFrame) -> pd.Series:
    """달력연도별 편도 회전율 합 — 연도 추이 확인용 보조 표. 요약·비교에는
    annualized_turnover를 사용할 것(부분 연도 과소계상 방지)."""
    return bt["turnover"].groupby(bt.index.year).sum()


def turnover_by_reason(bt: pd.DataFrame) -> pd.Series:
    """발표용 분해: 정기/캡/수시가 회전율에 각각 얼마나 기여했는가."""
    event_log = bt.attrs.get("event_log")
    if event_log is not None and len(event_log):
        return event_log.groupby("reason")["one_way_turnover"].sum() \
            .sort_values(ascending=False)
    x = bt[bt["turnover"] > 0]
    return x.groupby("reason")["turnover"].sum().sort_values(ascending=False)


def correlation(level: pd.Series, benchmark: pd.Series, min_obs: int = 30) -> float:
    a, b = level.pct_change().dropna(), benchmark.pct_change().dropna()
    idx = a.index.intersection(b.index)
    return float(np.corrcoef(a[idx], b[idx])[0, 1]) if len(idx) >= min_obs else np.nan


def _aligned_benchmark_returns(level: pd.Series, benchmark: pd.Series,
                               min_obs: int = 60) -> pd.DataFrame:
    """Keep only paired daily returns and fail closed on a short common sample."""
    pair = pd.concat(
        [level.pct_change(fill_method=None).rename("index"),
         benchmark.pct_change(fill_method=None).rename("benchmark")],
        axis=1, join="inner").dropna()
    if len(pair) < min_obs:
        raise ValueError(
            f"Benchmark common return observations {len(pair)} < {min_obs}; "
            "extend the sample or verify PR/TR consistency")
    if (pair <= -1.0).any().any():
        raise ValueError("Cannot use log relative returns with a return <= -100%")
    return pair


def _newey_west_mean(x: np.ndarray, lag: int | None = None) -> tuple[float, float, int]:
    """Newey-West/Bartlett long-run variance estimate for a sample mean."""
    x = np.asarray(x, dtype=float)
    n = len(x)
    if n < 2:
        raise ValueError("Newey-West requires at least two observations")
    if lag is None:
        lag = int(np.floor(4.0 * (n / 100.0) ** (2.0 / 9.0)))
    lag = max(0, min(int(lag), n - 1))
    centered = x - x.mean()
    long_run_var = float(centered @ centered / n)
    for k in range(1, lag + 1):
        gamma = float(centered[k:] @ centered[:-k] / n)
        long_run_var += 2.0 * (1.0 - k / (lag + 1.0)) * gamma
    return float(x.mean()), float(np.sqrt(max(long_run_var, 0.0) / n)), lag


def _stationary_bootstrap_indices(n: int, samples: int, block_size: int,
                                  rng: np.random.Generator) -> np.ndarray:
    """Circular stationary-bootstrap indices with geometric block lengths."""
    if samples < 1 or block_size < 1:
        raise ValueError("bootstrap samples and block_size must be positive")
    out = np.empty((samples, n), dtype=int)
    out[:, 0] = rng.integers(0, n, size=samples)
    restart = rng.random((samples, n - 1)) < (1.0 / block_size)
    fresh = rng.integers(0, n, size=(samples, n - 1))
    for j in range(1, n):
        out[:, j] = np.where(restart[:, j - 1], fresh[:, j - 1],
                             (out[:, j - 1] + 1) % n)
    return out


def benchmark_inference(level: pd.Series, benchmark: pd.Series,
                        min_obs: int = 60, nw_lag: int | None = None,
                        bootstrap_samples: int = 2000,
                        bootstrap_block_size: int = 20,
                        seed: int = 7) -> pd.Series:
    """Reproducible benchmark-relative inference for a fixed methodology.

    The HAC interval follows Newey-West (1987). The second interval uses the
    Politis-Romano (1994) stationary bootstrap to retain time-series dependence.
    ``Bootstrap positive share`` is a resample summary, not a p-value or PBO.
    White (2000) / Bailey et al. (2017) address a separate multiple-testing
    problem when performance is used to choose among many specifications.
    """
    pair = _aligned_benchmark_returns(level, benchmark, min_obs=min_obs)
    index_ret = pair["index"].to_numpy()
    bench_ret = pair["benchmark"].to_numpy()
    active = index_ret - bench_ret
    active_log = np.log1p(index_ret) - np.log1p(bench_ret)

    mean_active, hac_se, used_lag = _newey_west_mean(active, nw_lag)
    hac_low = (mean_active - 1.96 * hac_se) * TRADING_DAYS
    hac_high = (mean_active + 1.96 * hac_se) * TRADING_DAYS
    tracking_error = float(np.std(active, ddof=1) * np.sqrt(TRADING_DAYS))
    information_ratio = float(mean_active * TRADING_DAYS / tracking_error) \
        if tracking_error > 0 else np.nan

    rng = np.random.default_rng(seed)
    sampled = _stationary_bootstrap_indices(
        len(active_log), bootstrap_samples, bootstrap_block_size, rng)
    boot_relative = np.expm1(active_log[sampled].mean(axis=1) * TRADING_DAYS)
    ci_low, ci_high = np.quantile(boot_relative, [0.025, 0.975])

    common_benchmark_level = benchmark.reindex(pair.index).dropna()
    return pd.Series({
        "공통 수익률 관측치": int(len(pair)),
        "공통기간 시작": str(pair.index[0].date()),
        "공통기간 종료": str(pair.index[-1].date()),
        "벤치마크 CAGR(공통 구간)": cagr(common_benchmark_level),
        "상대수익률(연율 로그)": float(np.expm1(active_log.mean() * TRADING_DAYS)),
        "추적오차(연율)": tracking_error,
        "정보비율": information_ratio,
        "HAC lag": int(used_lag),
        "활성수익률(산술 연율)": float(mean_active * TRADING_DAYS),
        "HAC 95% 하한": float(hac_low),
        "HAC 95% 상한": float(hac_high),
        "HAC t 통계량": float(mean_active / hac_se) if hac_se > 0 else np.nan,
        "Stationary Bootstrap 95% 하한": float(ci_low),
        "Stationary Bootstrap 95% 상한": float(ci_high),
        "Bootstrap 양(+) 표본비율": float((boot_relative > 0).mean()),
        "Bootstrap 표본수": int(bootstrap_samples),
        "Bootstrap 평균 블록길이": int(bootstrap_block_size),
        "Bootstrap 시드": int(seed),
    })


def tracking_metrics(index_level: pd.Series, tracker_level: pd.Series,
                     min_obs: int = 30) -> pd.Series:
    """패시브 추종 상품 또는 독립 복제 포트폴리오의 추종오차를 산출한다.

    ``tracker_level``은 ETF/ETN NAV, 펀드 기준가 또는 독립적으로 계산한 복제
    포트폴리오 레벨이다. 지수 레벨만으로 계산한 0% 추종오차는 검증이 아니므로
    허용하지 않는다.
    """
    pair = _aligned_benchmark_returns(index_level, tracker_level, min_obs=min_obs)
    index_ret = pair["index"].to_numpy()
    tracker_ret = pair["benchmark"].to_numpy()
    gap = tracker_ret - index_ret
    log_gap = np.log1p(tracker_ret) - np.log1p(index_ret)
    tracking_error = float(np.std(gap, ddof=1) * np.sqrt(TRADING_DAYS))
    return pd.Series({
        "공통 수익률 관측치": int(len(pair)),
        "공통기간 시작": str(pair.index[0].date()),
        "공통기간 종료": str(pair.index[-1].date()),
        "누적 추종차이": float(np.expm1(log_gap.sum())),
        "추종오차(연율)": tracking_error,
        "평균 일간 추종차이": float(gap.mean()),
        "최대 일간 절대 추종차이": float(np.abs(gap).max()),
        "일간 수익률 상관계수": float(np.corrcoef(index_ret, tracker_ret)[0, 1]),
    })


# ----------------------------------------------------------------------
# 3b) 거래비용 시나리오 (버퍼룰 효과의 정량 근거)
# ----------------------------------------------------------------------
def apply_costs(bt: pd.DataFrame, cost_bp: float) -> pd.Series:
    """비용 차감 후 지수: 이벤트 당일 net_level *= (1 - 비용률 x 편도 회전율).

    비용률은 편도 회전율에 대한 왕복 체결비용(bp)으로 해석한다. Solactive의
    Rebalance Fee(편출 비중 합 + |ΔW| 합의 별도 합산)와는 다른 척도이며,
    본 지수는 단순·투명성을 위해 실제 편도 회전율(bt["turnover"], drift 반영)
    x 비용률 모델을 공식 채택한다."""
    factor = (1.0 - cost_bp / 1e4 * bt["turnover"]).cumprod()
    return bt["level"] * factor


def cost_sensitivity(bt: pd.DataFrame,
                     bps: tuple = (10, 30, 50)) -> pd.DataFrame:
    """0/10/30/50bp 시나리오별 누적수익률·CAGR 비교표 (발표용)."""
    rows = {}
    for b in (0,) + tuple(bps):
        lv = bt["level"] if b == 0 else apply_costs(bt, b)
        rows[f"{b:g}bp"] = {"누적수익률": float(lv.iloc[-1] / lv.iloc[0] - 1),
                           "CAGR": cagr(lv)}
    return pd.DataFrame(rows).T


def summary(bt: pd.DataFrame, benchmark: pd.Series | None = None) -> pd.Series:
    lv = bt["level"]
    md = max_drawdown(lv)
    out = {"누적수익률": float(lv.iloc[-1] / lv.iloc[0] - 1),
           "CAGR": cagr(lv), "연변동성": ann_vol(lv), "MDD": md["mdd"],
           "MDD_고점": md["peak"].date(), "MDD_저점": md["trough"].date(),
           "연율화회전율(편도)": annualized_turnover(bt)}
    if benchmark is not None:
        out["상관계수(벤치마크)"] = correlation(lv, benchmark)
    return pd.Series(out)


if __name__ == "__main__":
    # 데모: 정기 2회 + 수시 편출 2건(핵심·위성 동시) + 월간 캡이 모두
    # 이벤트로 연결된 "v8 전체 방법론" 백테스트
    from rebalance import ANCHOR, CORE, SAT

    rng = np.random.default_rng(7)
    days = pd.bdate_range("2025-06-13", "2026-06-19")
    tickers = [f"S{i:02d}" for i in range(14)]
    px = pd.DataFrame(100 * np.exp(np.cumsum(
        rng.normal(0.0006, 0.02, (len(days), 14)), axis=0)),
        index=days, columns=tickers)
    bench = px.mean(axis=1)

    def snap(mcaps):
        return pd.DataFrame({
            "ticker": tickers, "name": tickers,
            "group": [ANCHOR] * 2 + [CORE] * 8 + [SAT] * 4,
            "exposure": [np.nan] * 2
                        + [0.9, 0.8, 0.7, 0.6, 0.5, 0.45, 0.4, 0.35]
                        + [0.5, 0.4, 0.3, 0.2],
            "mem_ratio": [np.nan] * 10 + [0.90, 0.80, 0.75, 0.70],
            "float_mcap": mcaps, "eligible": True})

    snaps = {days[0]: snap([300e12, 250e12, 30e12, 25e12, 20e12, 15e12, 10e12,
                            8e12, 6e12, 5e12, 7e12, 5e12, 3e12, 2e12]),
             pd.Timestamp("2025-12-15"): snap([280e12, 260e12, 28e12, 27e12,
                                               21e12, 14e12, 11e12, 7e12, 6.5e12,
                                               5e12, 7e12, 5.5e12, 3e12, 2e12])}
    adhoc = {pd.Timestamp("2025-09-19"): [("S05", "상장폐지"), ("S10", "관리종목")]}

    events, hist = build_event_schedule(px, snaps, adhoc)
    bt = simulate_index(px, events)
    print("[이벤트]", [(e["effective_date"].date(), e["reason"]) for e in events])
    print("\n[교체 이력]\n", hist.to_string(index=False))
    print("\n[회전율 분해]\n", turnover_by_reason(bt).round(4).to_string())
    print("\n[성과 요약]\n", summary(bt, bench).to_string())
    print("\n[거래비용 민감도]\n", cost_sensitivity(bt).round(4).to_string())
