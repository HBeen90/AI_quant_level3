# -*- coding: utf-8 -*-
"""
run_backtest.py - 실데이터 백테스트 드라이버 (지금 비어 있는 '마지막 배선')
============================================================================
현재 레포에는 규칙 엔진(rebalance.py) · 스케줄러/지표(backtest.py) ·
지수 산출기(index_calc.py)가 모두 있지만, **실제 시장
데이터로 지수 시계열을 만든 적이 없다.** 그래서 방법론 3장 '성과 보고'
조문(누적·연환산수익률·변동성·MDD·연율화 회전율·상관계수)이 비어 있다.
27/67은 현행 운영값이며, 실측 결과는 향후 7.3 개정 검토에만 사용한다.

이 스크립트가 그 배선을 잇는다:

    PIT 심사 스냅샷(13회분)  +  pykrx 종가 패널
        -> build_event_schedule()      정기·수시·캡·긴급 이벤트 시간표
        -> simulate_index()            지수 레벨 재생
        -> summary / turnover_by_reason / cost_sensitivity / benchmark_inference
        -> 버퍼 정책 4안 동일 조건 재실행  => 27/67 사후 점검 자료

데이터 계약
-----------
스냅샷 : data/snapshots/snapshot_<시행일YYYYMMDD>.csv
    필수 : ticker(6자리 문자열) · name · group(anchor/core/satellite) ·
           exposure · mem_ratio · float_mcap · eligible(bool)
    계보(권장, 교차검증 리포트 '데이터 계약 v2') :
           selection_date · ff_market_cap_asof · ff_market_cap_source ·
           free_float_asof · code_commit
    -> 계보 컬럼은 --require-lineage 로 강제할 수 있다.

편출 : data/adhoc_exclusions.csv  (공지일 announce_date, ticker, reason)
    상장폐지·관리종목 지정·합병 소멸. 공지일 기준 D+2 집행은 엔진이 한다.

긴급심사 : snapshot_<공표일YYYYMMDD>.csv 디렉터리
    구성종목 5개 미만 상태에서 공표일 A 기준 A+2 편입에 사용한다.

거래정지 : ticker,start_date,end_date
    거래소 확인 기간만 등록하며, 해당 기간에만 최종 체결가를 유지한다.

가격 : pykrx get_market_ohlcv(adjusted=True) 종가.
    ** PR 계약(제6조) ** - 주식수 이벤트만 반영된 수정주가이며 배당은 미반영.
    TR 지수를 볼 때만 --mode gross_tr + 배당 데이터를 별도 주입한다.

사용 (로컬 PowerShell, 레포 루트)
---------------------------------
    python analysis/run_backtest.py --snapshots data/snapshots --out out/backtest
    python analysis/run_backtest.py --snapshots data/snapshots --policy all
    python analysis/run_backtest.py --snapshots data/snapshots --prices-cache out/px.csv

fail-closed 원칙을 그대로 승계한다. 활성 구성종목의 가격 결측은 0%로
대체하지 않고 중단하며, 중단 전에 **어느 종목·어느 구간이 문제인지**
사람이 읽을 수 있는 커버리지 리포트를 먼저 출력한다.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from analysis.index_calendar import as_of_today, rebalance_dates  # noqa: E402
from backtest.backtest import (  # noqa: E402
    annual_turnover, benchmark_inference, build_event_schedule,
    cost_sensitivity, simulate_index, summary, turnover_by_reason,
)
from src.rebalance import (  # noqa: E402
    BUFFER_POLICIES, ConfigV2, buffer_binding, validate_snapshot,
)

LINEAGE = ["selection_date", "ff_market_cap_asof", "ff_market_cap_source",
           "free_float_asof", "code_commit"]
SNAP_RE = re.compile(r"snapshot_(\d{8})\.csv$")
THEME_RELEVANCE_ALERT = 0.35
#: 캐시 커버리지 공백 허용치(달력일). 한국 최장 연휴 클러스터(설·추석+대체공휴일
#: +주말, 최대 ~10일)보다 크게 둬 '거래일 결측 0'인 완전한 캐시를 오판하지 않게
#: 한다. 실제 KRX 거래일 달력을 연결하면 거래일수 기준으로 대체할 값.
CACHE_GAP_DAYS = 16
_TRUE = {"true", "1", "y", "yes", "참"}
_FALSE = {"false", "0", "n", "no", "거짓"}


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


def _validate_lineage(df: pd.DataFrame, effective_date: pd.Timestamp,
                      filename: str) -> None:
    for c in ("selection_date", "ff_market_cap_asof", "free_float_asof"):
        parsed = pd.to_datetime(df[c], errors="coerce")
        if parsed.isna().any():
            sys.exit(f"[FAIL] 계보 날짜 파싱 실패({filename}, {c})")
        df[c] = parsed
    if df["selection_date"].nunique() != 1:
        sys.exit(f"[FAIL] selection_date 복수값({filename})")
    selection_date = df["selection_date"].iloc[0]
    if selection_date >= effective_date:
        sys.exit(f"[FAIL] selection_date가 시행일보다 이르지 않음({filename})")
    if not (df["ff_market_cap_asof"] == df["selection_date"]).all():
        sys.exit(f"[FAIL] ff_market_cap_asof != selection_date({filename})")
    if (df["free_float_asof"] > df["selection_date"]).any():
        sys.exit(f"[FAIL] free_float_asof가 selection_date 이후({filename})")
    for c in ("ff_market_cap_source", "code_commit"):
        if df[c].isna().any() or df[c].astype(str).str.strip().eq("").any():
            sys.exit(f"[FAIL] 계보 값 누락({filename}, {c})")


# ----------------------------------------------------------------------
# 1) 입력 로드
# ----------------------------------------------------------------------
def load_snapshots(path: str, require_lineage: bool = False) -> dict:
    """data/snapshots/snapshot_YYYYMMDD.csv 전부 -> {시행일: DataFrame}."""
    files = sorted(glob.glob(os.path.join(path, "snapshot_*.csv")))
    if not files:
        sys.exit(f"[FAIL] 스냅샷이 없다: {path}/snapshot_YYYYMMDD.csv")
    snaps: dict = {}
    for f in files:
        m = SNAP_RE.search(os.path.basename(f))
        if not m:
            sys.exit(f"[FAIL] 파일명 규칙 위반(snapshot_YYYYMMDD.csv): {f}")
        d = pd.Timestamp(m.group(1))
        df = pd.read_csv(f, dtype={"ticker": str, "코드": str})
        df = df.rename(columns={"코드": "ticker", "종목명": "name",
                                "bucket": "group", "ff_market_cap": "float_mcap"})
        if "ticker" not in df.columns or "eligible" not in df.columns:
            sys.exit(f"[FAIL] 스냅샷 필수 컬럼 누락: {os.path.basename(f)}")
        df["ticker"] = df["ticker"].astype(str).str.strip().str.zfill(6)
        df["eligible"] = _strict_bool(df["eligible"], "eligible")
        if require_lineage:
            miss = [c for c in LINEAGE if c not in df.columns]
            if miss:
                sys.exit(f"[FAIL] 계보 컬럼 누락({os.path.basename(f)}): {miss} - "
                         "데이터 계약 v2. --require-lineage 를 끄거나 채울 것")
            _validate_lineage(df, d, os.path.basename(f))
        validate_snapshot(df)                              # 기존 계약 재사용
        snaps[d] = df
    return snaps


def load_exclusions(path: str | None) -> dict:
    """{공지일: [(ticker, 사유), ...]}. 엔진이 공지일 기준 D+2에 집행한다."""
    if not path:
        return {}
    if not os.path.exists(path):
        sys.exit(f"[FAIL] 편출 파일 없음: {path}")
    df = pd.read_csv(path, dtype={"ticker": str, "코드": str})
    df = df.rename(columns={"코드": "ticker", "공지일": "announce_date",
                            "사유": "reason"})
    need = {"announce_date", "ticker", "reason"}
    if need - set(df.columns):
        sys.exit(f"[FAIL] 편출 파일 컬럼 누락: {sorted(need - set(df.columns))}")
    df["ticker"] = df["ticker"].astype(str).str.strip().str.zfill(6)
    out: dict = {}
    for d, g in df.groupby(pd.to_datetime(df["announce_date"])):
        out[pd.Timestamp(d)] = list(zip(g["ticker"], g["reason"]))
    return out


def load_suspensions(path: str | None) -> dict:
    """거래정지 CSV -> {ticker: [(start, end), ...]}.

    필수 컬럼: ticker,start_date,end_date. 거래소 확인 기간만 입력한다.
    """
    if not path:
        return {}
    if not os.path.exists(path):
        sys.exit(f"[FAIL] 거래정지 파일 없음: {path}")
    df = pd.read_csv(path, dtype={"ticker": str, "코드": str})
    df = df.rename(columns={"코드": "ticker", "정지시작일": "start_date",
                            "정지종료일": "end_date"})
    need = {"ticker", "start_date", "end_date"}
    if need - set(df.columns):
        sys.exit(f"[FAIL] 거래정지 파일 컬럼 누락: {sorted(need - set(df.columns))}")
    df["ticker"] = df["ticker"].astype(str).str.strip().str.zfill(6)
    df["start_date"] = pd.to_datetime(df["start_date"], errors="coerce")
    df["end_date"] = pd.to_datetime(df["end_date"], errors="coerce")
    if df[["start_date", "end_date"]].isna().any().any():
        sys.exit("[FAIL] 거래정지 시작일/종료일 파싱 실패")
    if (df["start_date"] > df["end_date"]).any():
        sys.exit("[FAIL] 거래정지 시작일이 종료일보다 늦음")
    out: dict = {}
    for _, r in df.sort_values(["ticker", "start_date"]).iterrows():
        out.setdefault(r["ticker"], []).append((r["start_date"], r["end_date"]))
    return out


def fetch_prices(tickers: list, start: str, end: str,
                 cache: str | None = None) -> pd.DataFrame:
    """pykrx 수정주가 종가 패널. 캐시가 있으면 재사용(재현성·속도)."""
    start_ts = pd.to_datetime(start, format="%Y%m%d")
    end_ts = pd.to_datetime(end, format="%Y%m%d")
    if cache and os.path.exists(cache):
        px = pd.read_csv(cache, index_col=0, parse_dates=True,
                         dtype={c: float for c in []})
        px.columns = [str(c).zfill(6) for c in px.columns]
        if not px.index.is_monotonic_increasing or px.index.has_duplicates:
            sys.exit("[FAIL] 가격 캐시 인덱스는 날짜 오름차순·중복 없음이어야 합니다")
        if px.columns.has_duplicates:
            sys.exit("[FAIL] 가격 캐시 티커가 6자리 정규화 후 중복됩니다")
        missing = sorted(set(tickers) - set(px.columns))
        if missing:
            sys.exit(f"[FAIL] 캐시에 없는 종목: {missing} - 캐시를 지우고 재수집")
        px = px.loc[(px.index >= start_ts) & (px.index <= end_ts)]
        if px.empty:
            sys.exit(f"[FAIL] 가격 캐시에 요청 기간({start}~{end}) 관측치가 없습니다")
        # 낡거나 잘린 캐시는 성과·MDD·회전율을 짧은 구간에서 조용히 계산하므로
        # fail-closed 한다. 단 임계값 CACHE_GAP_DAYS(달력일)는 한국 최장 연휴
        # 클러스터(설·추석+대체공휴일+주말, 최대 ~10일)보다 넉넉히 크게 둔다.
        # 7일로 좁히면 추석 뒤 as_of 에서 '거래일 결측 0'인 완전한 캐시도 오판한다.
        # (정밀 판정은 실제 KRX 거래일 달력 연결 후 거래일수 기준으로 대체할 것)
        start_gap = (px.index.min() - start_ts).days
        end_gap = (end_ts - px.index.max()).days
        if start_gap > CACHE_GAP_DAYS or end_gap > CACHE_GAP_DAYS:
            sys.exit(
                "[FAIL] 가격 캐시가 요청 기간을 충분히 덮지 않습니다 - "
                f"요청 {start_ts.date()}~{end_ts.date()} · "
                f"캐시 {px.index.min().date()}~{px.index.max().date()} "
                f"(앞 {start_gap}일·뒤 {end_gap}일 공백 > {CACHE_GAP_DAYS}일). "
                "캐시를 지우고 재수집하십시오.")
        print(f"[캐시] {cache} 재사용 ({px.shape[0]}일 x {px.shape[1]}종목, "
              f"~{px.index.max().date()})")
        return px[sorted(set(tickers))]
    try:
        from pykrx import stock
    except ImportError:
        sys.exit("[FAIL] pykrx 미설치 - pip install pykrx")
    cols = {}
    for i, t in enumerate(sorted(set(tickers)), 1):
        df = stock.get_market_ohlcv(start, end, t, adjusted=True)
        if df is None or df.empty:
            print(f"  [주의] {t}: 가격 미수신 (상장 전·폐지 가능) - NaN 유지")
            cols[t] = pd.Series(dtype=float)
        else:
            s = df["종가"].astype(float)
            cols[t] = s.replace(0.0, np.nan)          # 0원은 결측으로 간주
        if i % 10 == 0:
            print(f"  ... {i}/{len(set(tickers))} 종목 수집")
    px = pd.DataFrame(cols).sort_index()
    px.index = pd.DatetimeIndex(px.index)
    px = px.loc[(px.index >= start_ts) & (px.index <= end_ts)]
    if cache:
        os.makedirs(os.path.dirname(cache) or ".", exist_ok=True)
        px.to_csv(cache, encoding="utf-8-sig")
        print(f"[캐시] 저장: {cache}")
    return px


_BM_MARKET_ORDER = ("KOSPI", "KOSDAQ", "KRX", "테마")


def _is_tr_name(nm: str) -> bool:
    """'KRX 반도체 TR' 처럼 총수익(TR) 계열인가. 기본 지수는 PR 이므로 배제 우선.

    프로젝트 지수는 기본이 PR(배당락 하락 반영)이라, 비교는 PR 계열끼리 해야
    한다. TR 을 골라 PR 과 비교하면 배당 기여도만큼 벤치마크가 부풀려진다.
    """
    return "TR" in re.split(r"[\s()]+", nm.upper())


def choose_benchmark(found: list, return_type: str = "PR") -> tuple:
    """이름 매치 후보 중 **결정론적으로** 하나를 고른다(입력 순서 무관).

    과거 `found[0]`은 pykrx 반환 순서에 의존해, 같은 키워드가 실행마다 다른
    지수를(때로 PR 대신 TR 을) 물어올 수 있었다 - 재현성·비교 정합성 훼손.
    우선순위: (1) 요청 계열(PR/TR) (2) 이름이 짧은 것(기본 지수)
    (3) 시장 순위 (4) 티커. found = [(market, ticker, name), ...]."""
    if return_type not in {"PR", "TR"}:
        raise ValueError(f"return_type은 PR/TR이어야 합니다: {return_type}")
    rank = {m: i for i, m in enumerate(_BM_MARKET_ORDER)}
    want_tr = return_type == "TR"
    return sorted(found, key=lambda it: (_is_tr_name(it[2]) != want_tr,
                                         len(it[2]),
                                         rank.get(it[0], 99), it[1]))[0]


#: 위원회 관리 벤치마크 지정 파일(기본). --benchmark-config 로 덮을 수 있다.
DEFAULT_BENCHMARK_CONFIG = os.path.join(HERE, "data", "benchmark.yaml")


def load_benchmark_config(path: str | None):
    """벤치마크 지정 설정(.yaml/.json) 로드. 파일이 없으면 None(→ 이름기반 잠정)."""
    if not path or not os.path.exists(path):
        return None
    if path.endswith((".yaml", ".yml")):
        try:
            import yaml
        except ImportError:
            sys.exit("[FAIL] benchmark 설정이 .yaml 인데 PyYAML 미설치 - "
                     "pip install pyyaml 또는 .json 형식을 쓰십시오")
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def resolve_benchmark_spec(cfg: dict | None, mode: str = "pr",
                           default_keyword: str = "반도체") -> dict:
    """설정 + 산출 모드 -> 벤치마크 지정 스펙(코드 조회 없이 순수 결정).

    · status=CONFIRMED 이고 계열 코드가 있으면 코드 고정:
        {"status":"confirmed","code":..,"name":..,"return_type":"PR"|"TR"}
    · 그 외에는 이름기반 잠정:
        {"status":"provisional","keyword":..,"return_type":..}
    계열은 --mode 에 맞춘다(gross_tr→TR, 그 외→PR). PR 지수를 TR 벤치와
    비교하면 배당 기여만큼 왜곡되므로, 여기서 pr_code/tr_code 를 자동 선택한다.
    CONFIRMED 인데 코드·이름·확정 계보가 비면 fail-closed.
    """
    rt = "TR" if mode == "gross_tr" else "PR"          # both -> 헤드라인 PR
    if not cfg:
        return {"status": "provisional", "keyword": default_keyword,
                "return_type": rt}
    kw = str(cfg.get("fallback_keyword") or default_keyword)
    if str(cfg.get("status", "")).upper() != "CONFIRMED":
        return {"status": "provisional", "keyword": kw, "return_type": rt}
    headline = str(cfg.get("headline_return_type") or "").upper()
    if headline not in {"PR", "TR"}:
        sys.exit("[FAIL] 벤치마크 CONFIRMED 이나 headline_return_type이 PR/TR이 아닙니다")
    effective_date = str(cfg.get("effective_date") or "").strip()
    resolved_by = str(cfg.get("resolved_by") or "").strip()
    if not effective_date or pd.isna(pd.to_datetime(effective_date, errors="coerce")):
        sys.exit("[FAIL] 벤치마크 CONFIRMED 이나 effective_date가 비었거나 잘못됐습니다")
    if not resolved_by:
        sys.exit("[FAIL] 벤치마크 CONFIRMED 이나 resolved_by가 비었습니다")
    prim = cfg.get("primary") or {}
    code_field = "tr_code" if rt == "TR" else "pr_code"
    name_field = "tr_name" if rt == "TR" else "pr_name"
    code = str(prim.get(code_field) or "").strip()
    # 기존 primary.name 설정은 PR/TR 표기명이 같은 지수에 한해 하위 호환한다.
    name = str(prim.get(name_field) or prim.get("name") or "").strip()
    if not code:
        sys.exit(f"[FAIL] 벤치마크 status=CONFIRMED 이나 primary.{code_field} 가 비었습니다"
                 f"({rt} 계열) - 데이터담당이 실제 지수 코드를 기입해야 합니다")
    if not name:
        sys.exit(f"[FAIL] 벤치마크 CONFIRMED 이나 primary.{name_field} 이 비었습니다"
                 " - 코드-이름 대조가 불가합니다")
    return {"status": "confirmed", "code": code, "name": name, "return_type": rt}


def fetch_benchmark(start: str, end: str, keyword: str = "반도체",
                    cache: str | None = None, mode: str = "pr",
                    config_path: str | None = None,
                    require_confirmed: bool = False) -> pd.Series:
    """벤치마크 지수 종가.

    우선순위: benchmark.yaml 이 status=CONFIRMED 면 **코드로 고정 조회**하고
    조회된 지수명이 지정명과 다르면 중단(KRX 코드 개편 방어). 미확정이면 이름기반
    '잠정' 선택을 하되 공식 발표 금지 경고를 남긴다. --mode 에 맞춰 PR/TR 계열을
    선택한다(config_path 기본 = data/benchmark.yaml).

    require_confirmed=True 면 미확정 벤치마크를 **경고가 아니라 중단**으로
    처리한다. 확정 실행에서 status!=CONFIRMED 인 채로 통과하면, 이름기반으로
    임시 선택된 지수의 추종오차가 FINAL 매니페스트에 실린다 - 표준출력의
    경고 한 줄은 나중에 아무도 다시 읽지 않는다. 게이트 d2 가 사람 쪽에서
    막는 것을 기계 쪽에서도 막는다(같은 사실을 두 곳에서 확인).
    """
    if require_confirmed:
        # 캐시 분기보다 먼저 본다 - 캐시가 있으면 설정을 안 읽고 반환하므로,
        # 뒤에 두면 낡은 캐시로 검사를 통째로 건너뛸 수 있다.
        cfg0 = load_benchmark_config(config_path if config_path is not None
                                     else DEFAULT_BENCHMARK_CONFIG)
        if resolve_benchmark_spec(cfg0, mode,
                                  default_keyword=keyword)["status"] != "confirmed":
            sys.exit(
                "[FAIL] 확정 실행인데 benchmark.yaml status != CONFIRMED 입니다.\n"
                "  선택지 1) resolve_benchmark_code.py 로 코드를 확인하고 "
                "pr_code/effective_date/resolved_by 기입 후 status: CONFIRMED\n"
                "  선택지 2) --no-benchmark 로 벤치마크를 명시적으로 제외 "
                "(게이트 d2 에 제외 결정과 사유를 기록할 것)")

    if cache and os.path.exists(cache):
        s = pd.read_csv(cache, index_col=0, parse_dates=True).iloc[:, 0]
        if not s.index.is_monotonic_increasing or s.index.has_duplicates:
            sys.exit("[FAIL] 벤치마크 캐시 인덱스는 날짜 오름차순·중복 없음이어야 합니다")
        start_ts = pd.to_datetime(start, format="%Y%m%d")
        end_ts = pd.to_datetime(end, format="%Y%m%d")
        s = s.loc[(s.index >= start_ts) & (s.index <= end_ts)]
        if s.empty:
            sys.exit(f"[FAIL] 벤치마크 캐시에 요청 기간({start}~{end}) 관측치가 없습니다")
        # 벤치마크는 가격 패널과 달리 **부분 겹침이 정상**이다: 테마지수는 2020년
        # 이후 출범한 것이 많아 시작이 늦고, 하위 로직(correlation 은 공통구간만
        # inner-join, benchmark_inference 는 표본 부족 시 예외를 run_one 이 흡수)이
        # 이를 견딘다. 따라서 start_gap 은 fail 사유가 아니라 정보성 경고로만 남긴다.
        start_gap = (s.index.min() - start_ts).days
        end_gap = (end_ts - s.index.max()).days
        if start_gap > CACHE_GAP_DAYS or end_gap > CACHE_GAP_DAYS:
            print("[주의] 벤치마크 캐시가 요청 기간의 일부만 덮습니다 - "
                  f"요청 {start_ts.date()}~{end_ts.date()} · "
                  f"캐시 {s.index.min().date()}~{s.index.max().date()} "
                  f"(앞 {start_gap}일·뒤 {end_gap}일 공백). "
                  "지수 출범이 늦거나 캐시가 낡았을 수 있음 - 상관계수는 공통구간만 사용.")
        print(f"[캐시] 벤치마크 재사용: {cache}")
        return s
    cfg = load_benchmark_config(config_path if config_path is not None
                                else DEFAULT_BENCHMARK_CONFIG)
    spec = resolve_benchmark_spec(cfg, mode, default_keyword=keyword)
    try:
        from pykrx import stock
    except ImportError:
        sys.exit("[FAIL] pykrx 미설치")

    if spec["status"] == "confirmed":                  # 코드 고정(공식)
        code, name, rt = spec["code"], spec["name"], spec["return_type"]
        got = stock.get_index_ticker_name(code)
        if got is None or str(got).strip() != name:    # 코드->이름 대조(개편 방어)
            sys.exit(f"[FAIL] 벤치마크 코드 {code} 의 지수명이 '{got}' 로 지정명 "
                     f"'{name}' 와 다릅니다 - KRX 코드 개편 가능. benchmark.yaml 재확인")
        print(f"[벤치마크 확정] {code} {name} · {rt} 계열 (benchmark.yaml)")
        df = stock.get_index_ohlcv(start, end, code)
        s = df["종가"].astype(float)
        s.index = pd.DatetimeIndex(s.index); s.name = name
        if cache:
            s.to_frame().to_csv(cache, encoding="utf-8-sig")
        return s

    # 미확정 -> 이름기반 잠정 선택(공식 발표 금지)
    kw = spec["keyword"]
    print("[벤치마크 잠정-미확정] benchmark.yaml status!=CONFIRMED - 이름기반 임시 "
          "선택입니다. 이 벤치마크로 산출한 추종오차·상관계수는 공식 수치가 "
          "아닙니다(위원회 코드 확정 필요).")
    found = []
    for market in ("KOSPI", "KOSDAQ", "KRX", "테마"):
        try:
            for t in stock.get_index_ticker_list(market=market):
                nm = stock.get_index_ticker_name(t)
                if kw in nm:
                    found.append((market, t, nm))
        except Exception:
            continue
    if not found:
        sys.exit(f"[FAIL] '{kw}' 포함 지수를 못 찾음 - benchmark.yaml fallback_keyword 확인")
    print("[벤치마크 후보]")
    for mk, t, nm in found:
        print(f"   {mk:6s} {t}  {nm}")
    rt = spec["return_type"]
    mk, tkr, nm = choose_benchmark(found, rt)
    print(f"[벤치마크 채택(잠정)] {mk} {tkr} {nm}  "
          f"({rt} 우선·최단명·시장순)")
    df = stock.get_index_ohlcv(start, end, tkr)
    s = df["종가"].astype(float)
    s.index = pd.DatetimeIndex(s.index); s.name = nm
    if cache:
        s.to_frame().to_csv(cache, encoding="utf-8-sig")
    return s


def fetch_benchmarks(start: str, end: str, keyword: str,
                     cache: str | None, mode: str,
                     config_path: str | None,
                     require_confirmed: bool = False) -> dict[str, pd.Series]:
    """요청 모드별 벤치마크를 조회한다. both는 PR/TR을 분리한다."""
    modes = ("pr", "gross_tr") if mode == "both" else (mode,)
    result = {}
    for current in modes:
        current_cache = cache
        if cache and mode == "both":
            root, ext = os.path.splitext(cache)
            suffix = "tr" if current == "gross_tr" else "pr"
            current_cache = f"{root}_{suffix}{ext or '.csv'}"
        result[current] = fetch_benchmark(
            start, end, keyword, current_cache,
            mode=current, config_path=config_path,
            require_confirmed=require_confirmed,
        )
    return result


# ----------------------------------------------------------------------
# 2) 커버리지 진단 - fail-closed 전에 사람이 읽을 리포트를 먼저 낸다
# ----------------------------------------------------------------------
def coverage_report(px: pd.DataFrame, snaps: dict) -> pd.DataFrame:
    """각 정기변경 구간에서 '편입 후보'의 가격 결측을 미리 표로 보여준다.

    엔진은 결측을 만나면 예외로 멈춘다(설계대로). 그런데 예외 메시지만으로는
    '2020년엔 아직 상장 전이라 없는 것'인지 '수집 실패'인지 구분이 안 된다.
    그 구분을 여기서 먼저 만들어 준다 - 상장 전이면 스냅샷의 eligible=False
    (또는 그 시점 스냅샷에서 제외)로 처리하는 게 옳고, 수집 실패면 재수집이다.
    """
    rows = []
    dates = sorted(snaps)
    for i, d in enumerate(dates):
        end = dates[i + 1] if i + 1 < len(dates) else px.index[-1]
        window = px.loc[(px.index >= d) &
                        (px.index < end if i + 1 < len(dates)
                         else px.index <= end)]
        for t in snaps[d].loc[snaps[d]["eligible"], "ticker"]:
            if t not in px.columns:
                rows.append({"시행일": d.date(), "ticker": t, "상태": "패널에 없음",
                             "결측일": np.nan, "구간일수": len(window)})
                continue
            miss = int(window[t].isna().sum())
            if miss:
                first_ok = window[t].first_valid_index()
                rows.append({"시행일": d.date(), "ticker": t,
                             "상태": ("구간 전체 결측" if miss == len(window)
                                    else f"부분 결측(첫 유효 {first_ok.date() if first_ok is not None else '-'})"),
                             "결측일": miss, "구간일수": len(window)})
    return pd.DataFrame(rows)


def listing_check(tickers: list, asof: str) -> pd.DataFrame:
    """생존편향 진단 보조: 기준일 시점의 상장 종목 명단과 대조한다.

    현재 스냅샷이 '오늘 살아있는 종목'만으로 만들어졌다면 2020년 백테스트는
    생존편향이 들어간다. 이 함수로 '그 시점에 상장돼 있었는가'를 확인하고,
    반대로 그 시점엔 자격이었으나 지금 없는 종목이 빠졌는지는 사람이 판단한다.
    """
    try:
        from pykrx import stock
    except ImportError:
        sys.exit("[FAIL] pykrx 미설치")
    live = set(stock.get_market_ticker_list(asof, market="ALL"))
    return pd.DataFrame({"ticker": sorted(set(tickers)),
                         f"{asof} 상장": [t in live for t in sorted(set(tickers))]})


# ----------------------------------------------------------------------
# 3) 실행
# ----------------------------------------------------------------------
def load_dividends(path: str | None, px: pd.DataFrame) -> pd.DataFrame | None:
    """배당락일 주당 배당금 패널. 계약: 배당락일(ex-date) 기준, 보통배당만.

    CSV 형식: ex_date, ticker, dps       (특별배당·자본환급은 넣지 말 것 -
    그것들은 3.4 제수 조정으로 반영되므로 여기 넣으면 이중반영이다)
    """
    if not path:
        return None
    d = pd.read_csv(path, dtype={"ticker": str, "코드": str})
    d = d.rename(columns={"코드": "ticker", "배당락일": "ex_date",
                          "주당배당금": "dps"})
    need = {"ex_date", "ticker", "dps"}
    if need - set(d.columns):
        sys.exit(f"[FAIL] 배당 CSV 컬럼 누락: {sorted(need - set(d.columns))}")
    d["ticker"] = d["ticker"].astype(str).str.strip().str.zfill(6)
    d["ex_date"] = pd.to_datetime(d["ex_date"])
    d["dps"] = pd.to_numeric(d["dps"], errors="coerce")
    if d["dps"].isna().any():
        sys.exit("[FAIL] 배당 dps 숫자 변환 실패")
    if (d["dps"] < 0).any():
        sys.exit("[FAIL] 음수 배당 - 자본환급은 제수 조정 경로로 처리할 것")
    unknown = sorted(set(d["ticker"]) - set(px.columns))
    if unknown:
        sys.exit(f"[FAIL] 가격 패널에 없는 배당 종목: {unknown}")
    out = pd.DataFrame(0.0, index=px.index, columns=px.columns)
    off = d.loc[~d["ex_date"].isin(px.index), "ex_date"].dt.date.unique()
    if len(off):
        sys.exit(f"[FAIL] 거래일이 아닌 배당락일: {sorted(off)} - "
                 "휴장일 보정 후 재입력(임의 이동 금지)")
    for _, r in d.iterrows():
        if r["ticker"] in out.columns:
            out.loc[r["ex_date"], r["ticker"]] += float(r["dps"])
    print(f"[배당] {len(d)}건 · 배당락일 {d['ex_date'].nunique()}일 로드")
    return out


def run_one(px: pd.DataFrame, snaps: dict, adhoc: dict, cfg: ConfigV2,
            bench: pd.Series | None, mode: str = "pr",
            divs: pd.DataFrame | None = None,
            emergency_reviews: dict | None = None,
            suspensions: dict | None = None) -> dict:
    events, hist = build_event_schedule(
        px, snaps, adhoc, emergency_reviews=emergency_reviews,
        suspensions=suspensions, cfg=cfg)
    bt = simulate_index(px, events, base=1000.0, mode=mode,
                        ordinary_dividends=divs, suspensions=suspensions)
    relevance = theme_relevance_history(events, snaps)
    res = {"cfg": cfg, "events": events, "hist": hist, "bt": bt, "mode": mode,
           "theme_relevance": relevance,
           "buffer_binding": buffer_binding_history(events, snaps, cfg),
           "summary": summary(bt, bench), "by_reason": turnover_by_reason(bt),
           "cost": cost_sensitivity(bt), "annual_turnover": annual_turnover(bt)}
    if bench is not None:
        try:
            res["inference"] = benchmark_inference(bt["level"], bench)
        except ValueError as e:
            print(f"[주의] 벤치마크 추론 생략: {e}")
    return res


def buffer_binding_history(events: list, snapshots: dict,
                           cfg: ConfigV2) -> pd.DataFrame:
    """정기변경 시점별 **버퍼 발동 건수**. 정책 비교표의 해석 근거가 된다.

    '네 정책이 같은 숫자를 낸다'는 결과는 그 자체로는 버그와 구분되지 않는다.
    발동 건수를 같이 실으면 구분된다 - 발동 0건이면 동일 결과가 **정상**이고,
    발동이 있는데도 동일하면 그때는 버그를 의심해야 한다. 표만 내고 끝내지
    않는 이유이며, 발표에서 "버퍼를 왜 뒀나"에 답하는 자료이기도 하다.

    prev_members 는 직전 이벤트(정기·수시 무관)의 구성으로 잡는다 - 수시편출로
    빠진 종목은 그 다음 정기변경의 '기존 종목'이 아니기 때문이다.
    """
    rows = []
    snaps = {pd.Timestamp(k): v for k, v in snapshots.items()}
    prev: set = set()
    for event in sorted(events, key=lambda e: e["effective_date"]):
        d = pd.Timestamp(event["effective_date"])
        if event["reason"] == "regular" and d in snaps:
            b = buffer_binding(snaps[d], prev, cfg)
            bind = b[b["binding"]] if len(b) else b
            rows.append({
                "effective_date": d,
                "기존 비앵커 수": int(len(b)),
                "버퍼발동 건수": int(len(bind)),
                "버퍼발동 종목": ",".join(bind["ticker"].tolist()),
                "버퍼구간 관측": int(b["in_band"].sum()) if len(b) else 0,
            })
        prev = set(event["target_weights"].index)
    return pd.DataFrame(rows)


def theme_relevance_history(events: list, snapshots: dict,
                            alert: float = THEME_RELEVANCE_ALERT) -> pd.DataFrame:
    """정기변경별 **비앵커** 테마 적합도. 경보만 만들며 이벤트를 발생시키지 않는다.

    정의: 비앵커(핵심·위성) 종목의 테마 지표(핵심=HBM노출도, 위성=메모리향비중)를
    **비앵커 비중으로 정규화한 가중평균**. 즉 비앵커 바스켓 안에서의 평균 적합도다.

        score = Σ_{i∈비앵커}(w_i × metric_i) / Σ_{i∈비앵커} w_i

    앵커(규칙0으로 판정 - 노출도를 보지 않음)는 계산에서 제외한다. 과거 구현은
    앵커를 metric 0으로 두고 **전체 비중**에 곱했는데, 그러면 앵커 비중이 클수록
    (=테마 순도가 높을수록) 점수가 기계적으로 낮아져 고정 경보선(0.35)과의 비교가
    뒤집힌다(테마가 순수할수록 '적합도 미달' 오경보). 정규화하면 앵커 비중과
    무관하게 '비앵커가 얼마나 테마에 가까운가'만 재므로 경보선이 해석 가능해진다.
    """
    rows = []
    snaps = {pd.Timestamp(k): v for k, v in snapshots.items()}
    for event in events:
        d = pd.Timestamp(event["effective_date"])
        if event["reason"] != "regular" or d not in snaps:
            continue
        w = event["target_weights"]
        s = snaps[d].set_index("ticker").reindex(w.index)
        if s["group"].isna().any():
            raise ValueError(f"{d.date()} 적합도 스냅샷 종목 누락")
        core = s["group"].eq("core")
        sat = s["group"].eq("satellite")
        nonanchor = core | sat
        metric = pd.Series(np.nan, index=w.index)
        metric.loc[core] = pd.to_numeric(s.loc[core, "exposure"], errors="coerce")
        metric.loc[sat] = pd.to_numeric(s.loc[sat, "mem_ratio"], errors="coerce")
        bad = nonanchor & metric.isna()
        if bad.any():
            raise ValueError(f"{d.date()} 테마 적합도 입력 결측: "
                             f"{metric.index[bad].tolist()}")
        bad_range = nonanchor & ~metric.between(0.0, 1.0)
        if bad_range.any():
            raise ValueError(f"{d.date()} 테마 적합도 0~1 범위 위반: "
                             f"{metric.index[bad_range].tolist()}")
        # 비앵커 비중으로 정규화 - 앵커 비중이 점수를 누르지 않게 한다.
        na_w = float(w[nonanchor].sum())
        score = (float((w[nonanchor] * metric[nonanchor]).sum() / na_w)
                 if na_w > 0 else float("nan"))     # 비앵커 부재 → 적합도 정의 불가
        rows.append({"effective_date": d, "score": score,
                     "alert_line": float(alert),
                     "below_alert": bool(score < alert)})   # NaN이면 False
    return pd.DataFrame(rows)


def policy_table(results: dict) -> pd.DataFrame:
    """버퍼 정책 4안 비교표 - 27/67 사후 재검토용 실측 자료.

    패시브 배점 원칙 유지: CAGR로 정책을 고르지 않는다. 회전율·편출입 횟수·
    평균 종목 수·비용 차감 성과를 같이 본다(데이터 스누핑 방지).

    '버퍼발동' 열은 해석 장치다. 이 열이 전부 0이면 네 행이 같은 것이 정상이고
    (표본 안에서 유지 임계값이 판정에 개입할 기회가 없었다는 뜻), 0이 아닌데도
    행이 같으면 그때는 배선을 의심해야 한다. 열 없이 표만 내면 두 경우가
    구분되지 않아 '정책이 코드에 안 붙은 것 아니냐'는 반론을 못 막는다.
    """
    rows = {}
    for name, r in results.items():
        s = r["summary"]
        bb = r.get("buffer_binding")
        added = dropped = 0
        prev: set | None = None
        n_by_date: dict = {}
        for event in sorted(r["events"], key=lambda e: e["effective_date"]):
            cur = set(event["target_weights"].index)
            if prev is not None:
                added += len(cur - prev)
                dropped += len(prev - cur)
            prev = cur
            n_by_date[event["effective_date"]] = len(cur)
        n_daily = pd.Series(n_by_date, dtype=float).reindex(r["bt"].index).ffill()
        cost = r["cost"]
        relevance = r["theme_relevance"]
        rows[name] = {
            "유지선(핵심/위성)": f"{r['cfg'].hold_core:.2f}/{r['cfg'].hold_sat:.2f}",
            "연율화회전율(편도)": s["연율화회전율(편도)"],
            "편입 건수": added, "편출 건수": dropped,
            "버퍼발동 건수": int(bb["버퍼발동 건수"].sum())
            if bb is not None and len(bb) else 0,
            "평균 종목수": float(n_daily.mean()),
            "최저 테마적합도": float(relevance["score"].min())
            if len(relevance) else np.nan,
            "CAGR(0bp)": s["CAGR"],
            "CAGR(30bp)": float(cost.loc["30bp", "CAGR"]),
            "연변동성": s["연변동성"], "MDD": s["MDD"],
        }
    return pd.DataFrame(rows).T


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots", required=True, help="PIT 스냅샷 디렉터리")
    ap.add_argument("--exclusions", default=None, help="수시편출 CSV")
    ap.add_argument("--emergency-snapshots", default=None,
                    help="긴급심사 공표일 snapshot_YYYYMMDD.csv 디렉터리")
    ap.add_argument("--suspensions", default=None,
                    help="거래정지 CSV(ticker,start_date,end_date)")
    ap.add_argument("--out", default="out/backtest")
    ap.add_argument("--policy", default="mid",
                    help="mid | none,narrow,mid,wide | all")
    ap.add_argument("--prices-cache", default=None)
    ap.add_argument("--benchmark-cache", default=None)
    ap.add_argument("--benchmark-keyword", default="반도체",
                    help="benchmark.yaml 미확정 시 이름기반 잠정 선택 키워드")
    ap.add_argument("--benchmark-config", default=None,
                    help="벤치마크 지정 파일(기본 data/benchmark.yaml). "
                         "status=CONFIRMED·코드 기입 시 코드로 고정")
    ap.add_argument("--no-benchmark", action="store_true")
    ap.add_argument("--require-lineage", action="store_true",
                    help="데이터 계약 v2 계보 컬럼 5개를 강제")
    ap.add_argument("--coverage-only", action="store_true",
                    help="가격 커버리지 진단만 하고 종료")
    ap.add_argument("--mode", default="pr", choices=("pr", "gross_tr", "both"),
                    help="pr(기본) | gross_tr | both - both는 PR·TR 병기")
    ap.add_argument("--dividends", default=None,
                    help="배당 CSV(ex_date,ticker,dps). gross_tr/both에 필수")
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    # 같은 폴더를 다른 모드로 재실행할 때 이전 실행의 선택 산출물이 남으면
    # 새 매니페스트가 서로 다른 실행을 한 묶음으로 동결한다.
    for name in ("coverage_report.csv", "benchmark_level.csv",
                 "benchmark_inference.csv", "index_level_pr_tr.csv",
                 "policy_comparison.csv", "buffer_binding.csv"):
        path = os.path.join(a.out, name)
        if os.path.exists(path):
            os.remove(path)
    snaps = load_snapshots(a.snapshots, a.require_lineage)
    emergency = load_snapshots(a.emergency_snapshots, a.require_lineage) \
        if a.emergency_snapshots else {}
    adhoc = load_exclusions(a.exclusions)
    suspensions = load_suspensions(a.suspensions)
    tickers = sorted({t for df in list(snaps.values()) + list(emergency.values())
                      for t in df["ticker"]})
    start = min(snaps).strftime("%Y%m%d")
    asof = as_of_today()
    if asof < min(snaps):
        sys.exit(f"[FAIL] INDEX_ASOF({asof.date()})가 최초 시행일보다 이릅니다")
    end = asof.strftime("%Y%m%d")
    print(f"스냅샷 {len(snaps)}회 | 후보 {len(tickers)}종목 | {start}~{end}")
    print("시행일:", ", ".join(d.strftime("%Y-%m-%d") for d in sorted(snaps)))

    px = fetch_prices(tickers, start, end, a.prices_cache)
    if not px.index.is_monotonic_increasing or px.index.has_duplicates:
        sys.exit("[FAIL] 가격 인덱스는 날짜 오름차순·중복 없음이어야 합니다")
    missing_event_dates = sorted((set(snaps) | set(emergency) | set(adhoc)) -
                                 set(px.index))
    if missing_event_dates:
        sys.exit("[FAIL] 가격 거래일 인덱스에 없는 시행/공표일: "
                 f"{[d.date() for d in missing_event_dates]}")

    # 조문 대조: 스냅샷 시행일이 캘린더 조문과 맞는가 (계보 자체 검증)
    cal = set(rebalance_dates(px.index))
    off = [d.date() for d in sorted(snaps) if d not in cal]
    if off:
        print(f"[주의] 캘린더 조문(만기 익주 첫 거래일)과 다른 시행일: {off} - "
              "의도한 것이면 무시, 아니면 스냅샷 파일명을 정정할 것")

    cov = coverage_report(px, snaps)
    if len(cov):
        print("\n[가격 커버리지 경고]")
        print(cov.to_string(index=False))
        cov.to_csv(os.path.join(a.out, "coverage_report.csv"),
                   index=False, encoding="utf-8-sig")
        print("-> 상장 전이면 해당 시점 스냅샷에서 제외(또는 eligible=False), "
              "수집 실패면 재수집. 엔진은 결측을 임의 보정하지 않는다.")
    else:
        print("\n[가격 커버리지] 전 구간·전 편입후보 결측 없음")
    if a.coverage_only:
        return 0

    benchmarks = {}
    if not a.no_benchmark:
        benchmarks = fetch_benchmarks(
            start, end, a.benchmark_keyword, a.benchmark_cache,
            a.mode, a.benchmark_config,
            require_confirmed=a.require_lineage,
        )

    divs = load_dividends(a.dividends, px)
    if a.mode in ("gross_tr", "both") and divs is None:
        sys.exit("[FAIL] --mode gross_tr/both 에는 --dividends 가 필요합니다(제6조). "
                 "배당 없이 TR을 만들면 PR과 같은 시계열에 TR 이름만 붙는 셈입니다.")

    names = list(BUFFER_POLICIES) if a.policy == "all" else a.policy.split(",")
    results = {}
    for nm in names:
        if nm not in BUFFER_POLICIES:
            sys.exit(f"[FAIL] 알 수 없는 정책: {nm} (가능: {list(BUFFER_POLICIES)})")
        print(f"\n=== 정책 '{nm}' 실행 ===")
        base_mode = "pr" if a.mode in ("pr", "both") else "gross_tr"
        results[nm] = run_one(px, snaps, adhoc, ConfigV2.with_policy(nm),
                              benchmarks.get(base_mode),
                              mode=base_mode,
                              divs=None if base_mode == "pr" else divs,
                              emergency_reviews=emergency,
                              suspensions=suspensions)

    main_name = "mid" if "mid" in results else names[0]
    r = results[main_name]
    r["bt"].to_csv(os.path.join(a.out, "index_level.csv"), encoding="utf-8-sig")
    benchmark = benchmarks.get(base_mode)
    if benchmark is not None:
        benchmark.rename("level").to_frame().to_csv(
            os.path.join(a.out, "benchmark_level.csv"), encoding="utf-8-sig")
    r["hist"].to_csv(os.path.join(a.out, "change_history.csv"),
                     index=False, encoding="utf-8-sig")
    r["bt"].attrs["event_log"].to_csv(os.path.join(a.out, "event_log.csv"),
                                      index=False, encoding="utf-8-sig")
    r["theme_relevance"].to_csv(os.path.join(a.out, "theme_relevance.csv"),
                                index=False, encoding="utf-8-sig")

    if a.mode == "both":                       # PR·TR 병기 (제6조)
        cfg_m = ConfigV2.with_policy(main_name)
        r_tr = run_one(px, snaps, adhoc, cfg_m,
                       benchmarks.get("gross_tr"), mode="gross_tr",
                       divs=divs, emergency_reviews=emergency,
                       suspensions=suspensions)
        pr_lv, tr_lv = r["bt"]["level"], r_tr["bt"]["level"]
        comp = pd.DataFrame({"PR": r["summary"], "TR": r_tr["summary"]})
        print(f"\n[PR vs TR 병기 - 정책 {main_name}]\n{comp.to_string()}")
        yrs = (pr_lv.index[-1] - pr_lv.index[0]).days / 365.25
        gap = float((tr_lv.iloc[-1] / pr_lv.iloc[-1]) ** (1 / yrs) - 1)
        print(f"\n  연환산 배당 기여도(TR - PR) = {gap:.4%}")
        print("  PR의 배당락 하락은 오류가 아니라 가격지수의 정의입니다. "
              "두 계열을 병기하고,\n  어느 쪽을 공식 지수로 삼을지는 위원회가 정합니다.")
        pd.DataFrame({"PR": pr_lv, "TR": tr_lv}).to_csv(
            os.path.join(a.out, "index_level_pr_tr.csv"), encoding="utf-8-sig")

    print(f"\n[성과 요약 - 정책 {main_name} · {r['mode'].upper()}]"
          f"\n{r['summary'].to_string()}")
    print(f"\n[회전율 분해]\n{r['by_reason'].round(4).to_string()}")
    print(f"\n[연도별 회전율]\n{r['annual_turnover'].round(4).to_string()}")
    print(f"\n[거래비용 민감도]\n{r['cost'].round(4).to_string()}")
    # round(4)를 프레임 전체에 걸면 effective_date(datetime)에서 UserWarning이
    # 나 확정 실행 로그를 오염시킨다 - 숫자 열만 골라 반올림한다.
    print(f"\n[비앵커 테마 적합도 모니터링]\n"
          f"{r['theme_relevance'].round({'score': 4, 'alert_line': 4}).to_string(index=False)}")
    if "inference" in r:
        print(f"\n[벤치마크 대비 추론]\n{r['inference'].to_string()}")
        r["inference"].to_frame("value").to_csv(
            os.path.join(a.out, "benchmark_inference.csv"), encoding="utf-8-sig")

    bb_all = []
    for nm, res_nm in results.items():
        b = res_nm.get("buffer_binding")
        if b is not None and len(b):
            b = b.copy()
            b.insert(0, "정책", nm)
            b.insert(1, "유지선", f"{res_nm['cfg'].hold_core:.2f}/"
                                 f"{res_nm['cfg'].hold_sat:.2f}")
            bb_all.append(b)
    if bb_all:
        bb_tbl = pd.concat(bb_all, ignore_index=True)
        bb_tbl.to_csv(os.path.join(a.out, "buffer_binding.csv"),
                      index=False, encoding="utf-8-sig")
        tot = int(bb_tbl["버퍼발동 건수"].sum())
        print(f"\n[버퍼 발동 진단] 전 정책·전 시점 합계 발동 {tot}건")
        if tot == 0:
            print("  -> 표본 안에서 유지 임계값이 판정에 개입한 적이 없습니다. "
                  "정책 4안이 같은 수치를 내는 것은 배선 오류가 아니라 이 사실의"
                  "\n     결과입니다. 버퍼의 근거는 실측이 아니라 합성 민감도"
                  " (analysis/sensitivity_v2.py)이며, 발표에서 그렇게 말해야 합니다.")
        else:
            print(bb_tbl[bb_tbl["버퍼발동 건수"] > 0].to_string(index=False))

    if len(results) > 1:
        tbl = policy_table(results)
        print(f"\n[버퍼 정책 비교 - 27/67 사후 점검]\n{tbl.round(4).to_string()}")
        tbl.to_csv(os.path.join(a.out, "policy_comparison.csv"),
                   encoding="utf-8-sig")
        print("\n선택 기준(패시브 배점): 회전율·편출입 횟수·평균 종목수·비용 차감"
              " 성과를 함께 본다. CAGR 단독으로 고르지 않는다(데이터 스누핑).")
        if "버퍼발동 건수" in tbl.columns and int(tbl["버퍼발동 건수"].sum()) == 0:
            print("주의: 버퍼발동 0건 - 이 표의 정책 간 '차이 없음'은 표본의 성질이지"
                  "\n      정책이 무의미하다는 뜻도, 코드가 정책을 무시한다는 뜻도 아니다.")

    print(f"\n저장: {a.out}/  (index_level.csv · change_history.csv · "
          "event_log.csv · theme_relevance.csv · coverage_report.csv · "
          "policy_comparison.csv · buffer_binding.csv · benchmark_level.csv)")
    if a.mode == "pr":
        print("주의: 가격은 PR 계약(배당 미반영 수정주가)이다. TR 병기는 "
              "--mode both --dividends <배당CSV> (제6조).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
