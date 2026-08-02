# -*- coding: utf-8 -*-
"""스냅샷 생성기의 fail-closed 회귀 테스트 (가짜 pykrx, 네트워크 불요).

왜 필요한가 ― 2026-07-31 실제 사고
  확정 실행 중 KRX 가 JSON 대신 HTML(로그인·차단 안내)을 돌려주기 시작했다.
  pykrx 는 예외를 삼키고 빈 결과를 주는데, `market_facts` 가 그것을
  `listed=False`(미상장)로 기록했다. 기초 필터는 미상장을 정상 탈락으로
  처리하므로 **경고 하나 없이** 자격 8/33 짜리 스냅샷이 생성됐다.
  33종목 중 22종목의 `float_mcap` 이 0 이었다.

  로그를 눈으로 훑지 않았다면 그 스냅샷으로 백테스트가 돌아가고 결과가
  FINAL 매니페스트에 실렸을 것이다. `simulate_index` 는 가격 결측에
  예외를 던지는데 스냅샷 생성기만 조용히 통과시키고 있었다.

핵심 판별
  개별 시계열 조회가 비었을 때 그것이 미상장인지 실패인지는, **그 시점
  전체 시장 조회에 종목이 있는가**로 가른다. 있으면 상장 종목이므로
  시계열이 빌 수 없다 ― 조회 실패다.
"""
from __future__ import annotations

import os
import hashlib
import sys
import types

import numpy as np
import pandas as pd
import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "analysis"))

ASOF = pd.Timestamp("2025-05-30")
TICKERS = ["000660", "005930", "100001"]


def _cap_all(n_bad: int = 0) -> pd.DataFrame:
    idx = ["000660", "005930", "100001"] + [f"9{i:05d}" for i in range(97)]
    df = pd.DataFrame({"종가": 10000.0, "시가총액": 1e12}, index=idx)
    if n_bad:
        # 조회 대상(TICKERS) 이 아닌 채움 종목에만 0 을 넣는다. 대상에 넣으면
        # 관문 1(전체 오염)이 아니라 관문 4(대상 종목 시총 0)가 먼저 걸려
        # 무엇을 시험하는지 흐려진다.
        df.loc[df.index[-n_bad:], "시가총액"] = 0.0
    return df


def _install(cap_all, rng=None, ohlcv=None, cap_error=None):
    """가짜 pykrx 주입. rng/ohlcv 를 None 으로 두면 '빈 결과'를 흉내낸다."""
    def get_market_cap(*args, **kwargs):
        if len(args) >= 3:                      # 기간 + 종목 = 시계열 조회
            return rng if rng is not None else pd.DataFrame()
        if cap_error is not None:
            raise cap_error
        return cap_all

    def get_market_ohlcv(*args, **kwargs):
        if ohlcv is not None:
            return ohlcv
        return pd.DataFrame({"종가": [1.0]},
                            index=[pd.Timestamp("2010-01-04")])

    mod = types.ModuleType("pykrx")
    mod.stock = types.SimpleNamespace(get_market_cap=get_market_cap,
                                      get_market_ohlcv=get_market_ohlcv)
    sys.modules["pykrx"] = mod
    sys.modules["pykrx.stock"] = mod.stock


def _series_ok() -> pd.DataFrame:
    idx = pd.bdate_range("2025-01-02", periods=80)
    return pd.DataFrame({"시가총액": 1e12, "거래대금": 5e9}, index=idx)


def _facts(**kwargs):
    import build_pit_snapshots as b
    return b.market_facts(TICKERS, ASOF, **kwargs)


# ------------------------------------------------ 정상 경로
def test_healthy_fetch_returns_facts():
    _install(_cap_all(), rng=_series_ok())
    f = _facts()
    assert len(f) == 3
    assert bool(f["listed"].all())
    assert float(f["market_cap"].min()) > 0


def test_cached_adv_and_listing_dates_avoid_per_ticker_krx_calls():
    _install(_cap_all(), rng=None, ohlcv=pd.DataFrame())
    dates = pd.bdate_range(end=ASOF, periods=80)
    adv = pd.DataFrame(5e9, index=dates, columns=TICKERS)
    listing = pd.Series(pd.Timestamp("2010-01-04"), index=TICKERS)
    facts = _facts(adv_panel=adv, listing_dates=listing)
    assert len(facts) == 3
    assert (facts["adv60"] == 5e9).all()
    assert (facts["listed_days"] > 0).all()


def test_cached_adv_counts_halt_day_as_zero():
    _install(_cap_all(), rng=None, ohlcv=pd.DataFrame())
    dates = pd.bdate_range(end=ASOF, periods=80)
    adv = pd.DataFrame(5e9, index=dates, columns=TICKERS)
    adv.loc[dates[-1], "000660"] = np.nan
    listing = pd.Series(pd.Timestamp("2010-01-04"), index=TICKERS)
    facts = _facts(adv_panel=adv, listing_dates=listing)
    expected = 5e9 * 59 / 60
    assert facts.loc["000660", "adv60"] == pytest.approx(expected)


def test_cached_adv_allows_short_history_for_new_listing():
    _install(_cap_all(), rng=None, ohlcv=pd.DataFrame())
    dates = pd.bdate_range(end=ASOF, periods=20)
    adv = pd.DataFrame(5e9, index=dates, columns=TICKERS)
    listing = pd.Series(ASOF - pd.Timedelta(days=30), index=TICKERS)
    facts = _facts(adv_panel=adv, listing_dates=listing)
    assert (facts["adv60"] == 5e9).all()


def test_cached_adv_requires_full_market_window_for_established_ticker():
    _install(_cap_all(), rng=None, ohlcv=pd.DataFrame())
    dates = pd.bdate_range(end=ASOF, periods=20)
    adv = pd.DataFrame(5e9, index=dates, columns=TICKERS)
    listing = pd.Series(pd.Timestamp("2010-01-04"), index=TICKERS)
    with pytest.raises(SystemExit, match="fewer than 60 market days"):
        _facts(adv_panel=adv, listing_dates=listing)


def test_market_cap_login_failure_is_fail_closed_without_traceback():
    _install(_cap_all(), cap_error=KeyError("KRX login response"))
    with pytest.raises(SystemExit) as e:
        _facts()
    msg = str(e.value)
    assert "시가총액 조회 실패" in msg
    assert "KRX_ID/KRX_PW" in msg


# ------------------------------------------------ 관문 1: 전체 시장 오염
def test_widespread_zero_market_cap_aborts():
    """전체 조회의 상당수가 0 이면 KRX 응답이 깨진 것이다."""
    _install(_cap_all(n_bad=40), rng=_series_ok())
    with pytest.raises(SystemExit) as e:
        _facts()
    assert "시가총액이 비정상" in str(e.value)


def test_small_number_of_zeros_is_tolerated():
    """정상 시장에도 거래정지 등으로 0 이 소수 있을 수 있다."""
    _install(_cap_all(n_bad=2), rng=_series_ok())
    f = _facts()
    assert len(f) == 3


# ------------------------------------------------ 관문 2: 실패 vs 미상장
def test_empty_series_for_listed_ticker_aborts():
    """전체 조회에 있는 종목의 시계열이 비면 미상장이 아니라 실패다."""
    _install(_cap_all(), rng=None)              # 시계열만 빈 결과
    with pytest.raises(SystemExit) as e:
        _facts()
    msg = str(e.value)
    assert "시계열 조회 실패" in msg
    assert "미상장이 아닙니다" in msg


def test_ticker_absent_from_market_is_unlisted_not_failure():
    """전체 조회에 없는 종목은 진짜 미상장 - 중단하지 않는다."""
    cap = _cap_all().drop(index="100001")
    _install(cap, rng=_series_ok())
    f = _facts()
    assert not bool(f.loc["100001", "listed"])
    assert bool(f.loc["000660", "listed"])


# ------------------------------------------------ 관문 3: 상장 이력
def test_missing_listing_history_aborts():
    """상장경과일을 NaN 으로 두면 3개월 요건이 조용히 통과된다."""
    _install(_cap_all(), rng=_series_ok(), ohlcv=pd.DataFrame())
    with pytest.raises(SystemExit) as e:
        _facts()
    assert "상장 이력 조회 실패" in str(e.value)


# ------------------------------------------------ 관문 4: 최종 관문
def test_zero_market_cap_on_listed_ticker_aborts():
    """앞 관문을 통과해도 상장 종목의 시총이 0 이면 산출하지 않는다."""
    cap = _cap_all()
    cap.loc["100001", "시가총액"] = 0.0         # 전체 3% 미만이라 관문1 통과
    _install(cap, rng=_series_ok())
    with pytest.raises(SystemExit) as e:
        _facts()
    assert "시총 결측·비양수" in str(e.value)


# ------------------------------------------------ 사고 재현
def test_reproduces_the_20260731_incident_shape():
    """사고 당시 형태 - 시계열 조회가 전부 실패했다. 이제는 중단해야 한다."""
    _install(_cap_all(), rng=pd.DataFrame())
    with pytest.raises(SystemExit):
        _facts()


def test_frozen_market_facts_verify_raw_source_hash(tmp_path):
    import build_pit_snapshots as b

    root = tmp_path / "facts"
    raw = root / "raw"
    raw.mkdir(parents=True)
    source = raw / "krx_all_20250530.csv"
    source.write_bytes(b"official KRX source")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    frame = pd.DataFrame({
        "ticker": TICKERS,
        "listed": [True, True, True],
        "close": [1.0, 1.0, 1.0],
        "market_cap": [1e12, 1e12, 1e12],
        "mcap_rank": [1.0, 2.0, 3.0],
        "adv60": [1e9, 1e9, 1e9],
        "listed_days": [1000.0, 1000.0, 1000.0],
        "asof": [ASOF.date()] * 3,
        "market_cap_source": ["KRX all-stock screen download"] * 3,
        "source_file": [source.name] * 3,
        "source_sha256": [source_hash] * 3,
    })
    frame.to_csv(root / "facts_20250530.csv", index=False)
    mapping = pd.DataFrame({
        "selection_date": [ASOF.date()],
        "source_sha256": [source_hash],
        "copied_file": [source.name],
        "price_reference_file": ["px_kis_raw.csv"],
        "price_reference_sha256": ["1" * 64],
    })
    mapping.to_csv(root / "source_mapping.csv", index=False)
    facts = b.load_market_facts(str(root), ASOF, TICKERS)
    assert facts.attrs["source_sha256"] == source_hash
    assert len(facts.attrs["mapping_sha256"]) == 64
    assert facts.attrs["price_reference_sha256"] == "1" * 64

    source.write_bytes(b"tampered")
    with pytest.raises(SystemExit, match="source hash mismatch"):
        b.load_market_facts(str(root), ASOF, TICKERS)

    source.write_bytes(b"official KRX source")
    mapping.loc[0, "source_sha256"] = "0" * 64
    mapping.to_csv(root / "source_mapping.csv", index=False)
    with pytest.raises(SystemExit, match="source mapping does not match"):
        b.load_market_facts(str(root), ASOF, TICKERS)
