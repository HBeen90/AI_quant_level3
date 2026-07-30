# -*- coding: utf-8 -*-
"""
backtest/backtest.py - HBM 밸류체인 지수: 이벤트 소비형 백테스트 엔진
====================================================================
파이프라인(팀 합의): rebalance.py -> 이벤트 목록 -> backtest.py
  * rebalance.py  : 규칙 계층. 가격을 모르며 event만 생산.
  * backtest.py   : 이벤트 스케줄 생성(가격 필요 구간) + 지수 재생 + 지표.
  * 제수(divisor) 모듈은 건드리지 않는다. "제수와 수학적 동치"는 정기·수시·
    캡 이벤트가 '모두' 이벤트 목록에 포함될 때만 성립하며, 그 포함을
    build_event_schedule()이 담당한다.

가격 데이터 계약 (PR/TR - 제6조)
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
    AdhocManagerV2, ConfigV2, MethodologyReviewRequired, assign_weights_v2,
    monitor, regular_rebalance_v2, select_v2,
)


def make_event(date, reason: str, weights: pd.Series) -> dict:
    """이벤트 인터페이스(팀 합의 규격) - v1과 동일."""
    w = weights.astype(float)
    if w.empty or w.index.has_duplicates:
        raise ValueError("이벤트 목표비중은 비어 있지 않고 티커가 고유해야 합니다")
    if not np.isfinite(w).all() or (w < 0).any() or float(w.sum()) <= 0:
        raise ValueError("이벤트 목표비중은 유한한 비음수이며 합계가 양수여야 합니다")
    return {"effective_date": pd.Timestamp(date), "reason": reason,
            "target_weights": w / w.sum()}

TRADING_DAYS = 252


def apply_suspensions(prices: pd.DataFrame,
                      suspensions: dict | None) -> pd.DataFrame:
    """안건 1(2026-07-23 확정) - 거래정지 가격 처리.

    suspensions : {ticker: [(start, end), ...]} 거래소가 정지를 '확인한' 기간.
    등록된 기간 안의 결측 가격만 최종 체결가로 유지(carry)하고, 재개일에는
    시장가격으로 복귀한다(복귀 수익률 = 재개가/최종체결가 - 1).
    등록되지 않은 결측은 그대로 두어 기존 fail-closed 검증이 잡는다 -
    일반 결측의 임의 forward-fill 금지 조문을 코드로 강제하는 구조다.

    편출가 워터폴(데이터 계약): 상장폐지·파산 편출일의 가격은 호출자가
    ① 정리매매 종가 -> ② 거래소 평가가/합병조건가 -> ③ 사전 정의 최소가격
    순으로 prices 에 주입한다. 엔진은 그 값을 최종 drift에 사용할 뿐
    가격을 창작하지 않는다. (현금합병·정지 중 합병의 거래조건가도 동일 -
    안건 2)
    """
    if not suspensions:
        return prices
    out = prices.copy()
    for tkr, windows in suspensions.items():
        if tkr not in out.columns:
            raise ValueError(f"정지 등록 티커가 가격 데이터에 없음: {tkr}")
        filled = out[tkr].ffill()
        for start, end in windows:
            start, end = pd.Timestamp(start), pd.Timestamp(end)
            if start > end:
                raise ValueError(f"정지 기간 역전: {tkr} {start}~{end}")
            mask = (out.index >= start) & (out.index <= end)
            out.loc[mask, tkr] = filled[mask]
    return out


# ----------------------------------------------------------------------
# 1) 이벤트 스케줄 생성 - v8 전체 방법론(정기 + 월간 캡 + 수시변경) 연결
# ----------------------------------------------------------------------
def build_event_schedule(prices: pd.DataFrame,
                         review_snapshots: dict,
                         adhoc_exclusions: dict | None = None,
                         emergency_reviews: dict | None = None,
                         suspensions: dict | None = None,
                         cfg: ConfigV2 = ConfigV2()) -> tuple:
    """v2 이벤트 스케줄: 정기변경 + 무대체 수시편출(D+2) + 월간 캡(D+2).

    review_snapshots : {정기변경 시행일: 심사 스냅샷 DataFrame}
    adhoc_exclusions : {공지일: [(ticker, 사유), ...]} -> 공지일 기준 D+2에 편출 집행
    원칙:
      - 편출 후 대체 편입 없음. 편출 결과 5종목 미만이어도 산출을 지속하고
        긴급 재심사 절차로 회복한다(안건 3 확정 - 전 종목 편출만 산출 불가).
      - [지원 범위] 합병은 현금합병·단순 편출만 백테스트로 지원한다.
        주식교부 합병의 존속회사 비중 승계(주식수 증가)는 index_calc(제수)
        영역이며, 그 산출물(최종 목표비중 이벤트)을 본 simulate_index가
        소비하는 방식으로 반영한다 - 자체 승계 계산은 하지 않는다.
      - 월말 점검에서 30% 초과 시 25% 캡을 D+2에 집행(README 3장 ①).
      - 구성 변경(정기·편출) 발생 시 기존 캡 예약은 전부 취소하고 새 구성
        기준으로 재점검·재예약한다(예약이 낡은 구성을 되살리지 않도록).
    emergency_reviews : {긴급심사 결과 공표일 A: PIT 스냅샷} (안건 3 확정)
      - 스냅샷은 '결원 발생일까지 공개된 최신 확정자료'로 작성(호출자 책임).
      - 하한 미달 상태에서만 처리한다. 신규 기준(30%/70%)을 충족한 종목
        전원을 공표일 A 기준 A+2 거래일에 편입(reason="emergency_fill").
        스냅샷에는 현 구성종목의 float_mcap 도 포함되어야 한다(비중 재산정).
      - 충족 종목이 없으면 편입 없이 하한 미달 상태로 산출을 계속한다(폴백).
      - cfg.emergency_window_days(팀 운영안) 내 미해소 시 이력에
        termination_review_due 마커를 남기고 산출은 계속한다(7.3 이관은
        위원회 절차 - 엔진은 중단하지 않는다).
    반환 : (events 리스트[날짜순], 이력 DataFrame - 정기 교체 + 긴급심사 마커)
    """
    prices = apply_suspensions(prices, suspensions)   # 안건 1: 정지 기간만 carry
    adhoc = {pd.Timestamp(k): v for k, v in (adhoc_exclusions or {}).items()}
    emrg = {pd.Timestamp(k): v for k, v in (emergency_reviews or {}).items()}
    snaps = {pd.Timestamp(k): v for k, v in review_snapshots.items()}
    reg_dates = sorted(snaps)
    hist: list = []

    # fail-closed: 정기변경 시행일이 실제 거래일 달력(prices.index)에 있어야 한다.
    # 루프는 거래일만 순회하며 `d in snaps` 로 정기변경을 트리거하므로, 시행일이
    # 휴장일이면 그 정기변경이 **조용히 누락**된다(에러도 없이 구간 전체가 직전
    # 구성으로 이어짐). build_pit_snapshots 는 bdate_range(주말만 제외)로 시행일을
    # 잡는데 run_backtest 는 pykrx 실거래일을 쓰므로, 대체공휴일 등에서 어긋날 수
    # 있다. 조용한 누락 대신 어긋난 날짜를 지목해 멈춘다.
    if not reg_dates:
        raise ValueError("정기변경 스냅샷이 최소 1건 필요합니다")
    off_cal = [d for d in reg_dates if d not in prices.index]
    if off_cal:
        raise ValueError(
            "정기변경 시행일이 거래일 달력에 없습니다(휴장일 등): "
            f"{[str(d.date()) for d in off_cal]} - 스냅샷 시행일을 실제 거래일로 "
            "맞추십시오. 그대로 두면 해당 정기변경이 조용히 누락됩니다 "
            "(build_pit_snapshots 의 bdate_range 와 실거래일 달력 불일치).")
    off_adhoc = sorted(d for d in adhoc if d not in prices.index)
    off_emrg = sorted(d for d in emrg if d not in prices.index)
    if off_adhoc or off_emrg:
        details = []
        if off_adhoc:
            details.append(f"수시편출 공지일 {[str(d.date()) for d in off_adhoc]}")
        if off_emrg:
            details.append(f"긴급심사 공표일 {[str(d.date()) for d in off_emrg]}")
        raise ValueError(
            "이벤트 기준일이 거래일 달력에 없습니다: " + " · ".join(details)
            + " - D+2 기산 거래일을 명시해 입력하십시오.")

    rets = prices.pct_change(fill_method=None)
    dates = prices.index[prices.index >= reg_dates[0]]
    # 다음 실제 거래일이 다음 달에 있어야 월말로 확정한다. 패널이 월중에
    # 끝났을 때 마지막 관측일을 월말로 오인해 캡을 조기 점검하지 않는다.
    next_dates = pd.Series(dates, index=dates).shift(-1)
    is_month_end = next_dates.notna() & (next_dates.dt.month != dates.month)

    events: list = []
    vm = None
    pending_caps: dict = {}          # {집행일: 목표비중}
    pending_excl: dict = {}          # {집행일: [(ticker, 사유), ...]}
    pending_emrg: dict = {}          # {집행일 A+2: (weights, groups)}
    excluded_cum: set = set()        # 누적 하드 편출 - 긴급 편입 부활 방지
    breach_idx = None                # 하한 미달 시작 인덱스 (안건 3)
    term_logged = False

    def track_breach(i: int, d) -> None:
        nonlocal breach_idx, term_logged
        if vm is None:
            return
        under = len(vm.weights) < cfg.min_constituents
        if under and breach_idx is None:
            breach_idx = i
            hist.append({"date": d, "event": "under_min_start",
                         "n": len(vm.weights)})
        elif not under and breach_idx is not None:
            breach_idx = None
            term_logged = False        # [리뷰 P1-4] 두 번째 미달도 마커가 나오도록
            hist.append({"date": d, "event": "under_min_resolved",
                         "n": len(vm.weights)})

    def mark_deferred(d, kind: str, **details) -> None:
        row = {"date": d, "event": "deferred_beyond_panel", "kind": kind,
               "lag_trading_days": 2, "panel_end": dates[-1]}
        row.update(details)
        hist.append(row)

    def recheck_and_book_cap(i: int) -> None:
        pending_caps.clear()
        if vm is None:
            return
        adj, changed = monitor(vm.weights, cfg)
        if changed:
            if i + 2 < len(dates):
                pending_caps[dates[i + 2]] = adj
            else:
                mark_deferred(dates[i], "cap")

    for i, d in enumerate(dates):
        if vm is not None:
            vm.drift(rets.loc[d])

        # 공지는 정기변경 여부와 무관하게 먼저 D+2 예약한다. 정기변경일 공지나
        # 정기변경을 가로지르는 예약도 하드 편출 효력을 잃으면 안 된다.
        if d in adhoc:
            if i + 2 < len(dates):
                pending_excl.setdefault(dates[i + 2], []).extend(adhoc[d])
            else:
                mark_deferred(d, "exclusion",
                              tickers=sorted(t for t, _ in adhoc[d]))

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
            excluded_cum.clear()      # 새 정기 심사 - 재편입 여부는 스냅샷 eligible이 결정
            excluded_cum |= drop      # 단, 동일자 하드 편출은 그대로 누적
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
            pending_emrg.clear()
            recheck_and_book_cap(i)
            track_breach(i, d)
            continue

        if vm is not None and d in pending_excl:        # 편출 집행 (무대체)
            batch = [(t, r) for t, r in pending_excl.pop(d)
                     if t in vm.weights.index]
            if batch:
                excluded_cum |= {t for t, _ in batch}
                vm.apply_exclusions(batch)   # 안건 3: 5 미만이어도 산출 지속
                events.append(make_event(d, "exclusion", vm.weights))
                if pending_emrg:             # [리뷰 P1-3] 구성 변경 -> 예약 긴급
                    pending_emrg.clear()     # 편입 무효화(재공표 필요) - 예약이
                    hist.append({"date": d,  # 방금 편출된 종목을 되살리지 않도록
                                 "event": "emergency_booking_cancelled"})
                recheck_and_book_cap(i)                 # 캡 예약 무효화 + 재점검
                track_breach(i, d)
                continue

        if vm is not None and d in pending_emrg:        # 긴급 편입 집행 (A+2)
            w_new, g_new = pending_emrg.pop(d)
            vm = AdhocManagerV2(w_new.copy(), g_new.copy(), cfg)
            events.append(make_event(d, "emergency_fill", vm.weights))
            recheck_and_book_cap(i)
            track_breach(i, d)
            continue

        if vm is not None and d in emrg:                # 긴급심사 결과 공표일 A
            if breach_idx is None:
                hist.append({"date": d, "event": "emergency_review_skipped",
                             "n": len(vm.weights)})     # 하한 충족 상태면 무시
            else:
                snap_e = emrg[d]
                cur = set(vm.weights.index)
                qual = select_v2(snap_e, prev_members=set(), cfg=cfg)["members"]
                # 누적 하드 편출 종목은 긴급심사 스냅샷에 남아 있어도 부활 금지
                # (상장폐지·합병 소멸 종목이 '결원 발생일까지 자료'에는 자격으로
                #  보일 수 있으므로 엔진이 방어한다)
                adds = qual[~qual["ticker"].isin(cur | excluded_cum)]
                if len(adds) == 0:                      # 폴백: 미달 상태 지속
                    hist.append({"date": d, "event": "emergency_no_candidate",
                                 "n": len(vm.weights)})
                else:
                    mcap = snap_e.set_index("ticker")["float_mcap"]
                    missing = [t for t in cur if t not in mcap.index]
                    if missing:
                        raise ValueError(
                            f"긴급심사 스냅샷에 현 구성종목 유동시총 누락: {missing}")
                    combined = pd.concat([
                        pd.DataFrame({"ticker": list(cur),
                                      "group": vm.groups.reindex(list(cur)),
                                      "float_mcap": mcap.reindex(list(cur))}),
                        adds[["ticker", "group", "float_mcap"]]],
                        ignore_index=True).sort_values("ticker")
                    w_new = assign_weights_v2(combined, cfg)
                    g_new = combined.set_index("ticker")["group"]
                    if i + 2 < len(dates):              # 공표일 A 기준 A+2 편입
                        pending_emrg[dates[i + 2]] = (w_new, g_new)
                        hist.append({"date": d, "event": "emergency_fill_booked",
                                     "adds": sorted(adds["ticker"])})
                    else:
                        mark_deferred(d, "emergency_fill",
                                      adds=sorted(adds["ticker"]))

        if vm is not None and d in pending_caps:        # 월간 캡 집행
            tgt = pending_caps.pop(d)
            tgt = tgt[tgt.index.intersection(vm.weights.index)]
            vm.weights = (tgt / tgt.sum()).copy()
            events.append(make_event(d, "cap", vm.weights))

        if vm is not None and bool(is_month_end.loc[d]):  # 월말 점검 -> D+2 예약
            recheck_and_book_cap(i)

        # 안건 3: 미해소 기간 초과 -> 7.3 이관 마커(산출은 계속, 중단 없음)
        if breach_idx is not None and not term_logged \
                and i - breach_idx >= cfg.emergency_window_days:
            term_logged = True
            hist.append({"date": d, "event": "termination_review_due",
                         "n": len(vm.weights) if vm is not None else 0})

    return events, pd.DataFrame(hist)


def simulate_index(prices: pd.DataFrame, events: list | dict,
                   base: float = 1000.0, mode: str = "pr",
                   ordinary_dividends: pd.DataFrame | None = None,
                   suspensions: dict | None = None) -> pd.DataFrame:
    """이벤트 목록으로 지수 레벨을 재생한다.

    events : make_event() 규격 리스트(권장) 또는 {날짜: 비중} dict(하위호환)
    반환 : DataFrame[level, turnover, reason]
      turnover = 이벤트 당일 편도 회전율 = 0.5 * Σ|목표 - drift 후 비중|
    """
    prices = apply_suspensions(prices, suspensions)   # 안건 1 (미등록 결측은 불변)
    if isinstance(events, dict):
        events = [make_event(k, "regular", v) for k, v in events.items()]
    if not events:
        raise ValueError("지수 재생에는 이벤트가 최소 1건 필요합니다")
    if not prices.index.is_monotonic_increasing or prices.index.has_duplicates:
        raise ValueError("가격 인덱스는 날짜 오름차순·중복 없음이어야 합니다")
    missing_event_dates = sorted(
        {pd.Timestamp(e["effective_date"]) for e in events} - set(prices.index))
    if missing_event_dates:
        raise ValueError("가격 인덱스에 없는 이벤트 시행일: "
                         f"{[d.date() for d in missing_event_dates]}")
    ev: dict = {}
    for e in sorted(events, key=lambda x: x["effective_date"]):
        ev.setdefault(e["effective_date"], []).append(e)

    rets = prices.pct_change(fill_method=None)
    px_rets = prices.pct_change(fill_method=None)  # 비중 drift 전용(항상 가격만)
    if mode == "gross_tr":
        if ordinary_dividends is None:
            raise ValueError("mode='gross_tr'에는 ordinary_dividends가 필요합니다"
                             "(제6조). 보통배당(주당, 락일 기준)만 넣을 것 - "
                             "특별배당·자본환급은 PR 제수 조정으로 반영되므로 "
                             "포함 시 이중반영이 된다.")
        divs = ordinary_dividends.apply(pd.to_numeric, errors="coerce")
        bad_numeric = ordinary_dividends.notna() & divs.isna()
        if bad_numeric.any().any():
            raise ValueError("ordinary_dividends에 숫자가 아닌 값이 있습니다")
        if (divs.fillna(0.0) < 0).any().any():
            raise ValueError("ordinary_dividends는 음수일 수 없습니다")
        extra_cols = divs.columns.difference(prices.columns)
        extra_dates = divs.index.difference(prices.index)
        if (len(extra_cols) and divs[extra_cols].fillna(0.0).ne(0).any().any()) \
                or (len(extra_dates) and divs.loc[extra_dates].fillna(0.0).ne(0).any().any()):
            raise ValueError("가격 패널 밖 종목/날짜에 배당이 있습니다")
        divs = divs.reindex_like(prices).fillna(0.0)
        prev_prices = prices.shift(1)
        bad_prev = (divs > 0) & (prev_prices.isna() | (prev_prices <= 0))
        if bad_prev.any().any():
            where = list(zip(*np.where(bad_prev.to_numpy())))[:5]
            raise ValueError(f"배당락일 직전 가격 결측·비양수: {where}")
        dy = divs / prev_prices
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
    """연율화 편도 회전율 = 총 회전율 / 실제 경과연수 - 공식 수치는 이것이다.
    달력연도별 합계(annual_turnover)를 단순 평균하면 부분 연도(예: 6월~익년
    6월)가 두 조각으로 갈라져 절반으로 과소계상되므로 요약치로 쓰지 않는다."""
    yrs = (bt.index[-1] - bt.index[0]).days / 365.25
    return float(bt["turnover"].sum() / yrs) if yrs > 0 else np.nan


def annual_turnover(bt: pd.DataFrame) -> pd.Series:
    """달력연도별 편도 회전율 합 - 연도 추이 확인용 보조 표. 요약·비교에는
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
    pair = pd.concat(
        [level.pct_change(fill_method=None).rename("index"),
         benchmark.pct_change(fill_method=None).rename("benchmark")],
        axis=1, join="inner").dropna()
    return float(pair["index"].corr(pair["benchmark"])) \
        if len(pair) >= min_obs else np.nan


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
