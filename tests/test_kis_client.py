# -*- coding: utf-8 -*-
"""KIS 클라이언트 회귀 테스트 (가짜 세션, 네트워크 불요).

무엇을 고정하는가

  1. 페이지네이션이 100행 경계를 넘어 start 까지 거슬러 가고, 중복 없이
     오름차순으로 합쳐진다 ― 여기가 틀리면 **구간이 조용히 잘린다**
     (가격 패널 조기 종료 사고와 같은 유형, 작업내역 3-3).
  2. rt_cd != "0" 이면 예외로 죽는다(fail-closed) ― 조용한 빈 값 금지.
  3. 토큰이 디스크에 캐시되고, 유효하면 재발급하지 않는다 ― KIS 는 발급
     빈도를 제한하므로 매 실행 재발급이면 운영에서 걸린다.
  4. 수정주가 플래그가 기본 "0"(수정주가)으로 나간다 ― 원주가로 수집하면
     액면분할 종목에서 수익률이 통째로 틀어진다.
  5. 0원 종가는 NaN ― run_backtest.fetch_prices 와 같은 규약.
  6. 교차검증이 허용오차 초과에서 실제로 FAIL 한다.
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import pandas as pd
import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from analysis.kis_client import KISClient, KISError, PAGE_ROWS  # noqa: E402


# ------------------------------------------------ 가짜 HTTP
class FakeResp:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


class FakeSession:
    """토큰 1회 + 시세 N회를 흉내낸다. 호출 파라미터를 기록한다."""

    def __init__(self, pages):
        self.pages = list(pages)      # 시세 호출마다 하나씩 소비
        self.calls = []               # (path, params)
        self.token_posts = 0

    def post(self, url, json=None, timeout=None):
        self.token_posts += 1
        return FakeResp({"access_token": "T" * 8, "expires_in": 86400})

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        if not self.pages:
            return FakeResp({"rt_cd": "0", "output2": []})
        return FakeResp(self.pages.pop(0))


def _rows(dates, close=1000.0):
    return [{"stck_bsop_date": d, "stck_clpr": str(close),
             "acml_tr_pbmn": "1", "acml_vol": "1"} for d in dates]


def _client(pages, tmp_path):
    sess = FakeSession(pages)
    c = KISClient("K" * 10, "S" * 10, session=sess,
                  token_cache=str(tmp_path / "tok.json"), rate_per_sec=1e9)
    return c, sess


# ------------------------------------------------ 1. 페이지네이션
def test_pagination_walks_back_to_start(tmp_path):
    """100행 페이지 두 장 + 꼬리 한 장이 빠짐없이 합쳐지는가."""
    days = pd.bdate_range("2024-01-02", periods=230).strftime("%Y%m%d")
    p1 = {"rt_cd": "0", "output2": _rows(days[-PAGE_ROWS:])}       # 최신 100
    p2 = {"rt_cd": "0", "output2": _rows(days[-2 * PAGE_ROWS:-PAGE_ROWS])}
    p3 = {"rt_cd": "0", "output2": _rows(days[:-2 * PAGE_ROWS])}   # 나머지 30
    c, sess = _client([p1, p2, p3], tmp_path)
    df = c.daily_prices("005930", days[0], days[-1])
    assert len(df) == 230
    assert df.index.is_monotonic_increasing and not df.index.has_duplicates
    # 두 번째 호출의 종료일 = 첫 페이지 최고(古)일 전일이어야 한다
    d2 = sess.calls[1][1]["FID_INPUT_DATE_2"]
    oldest_p1 = min(days[-PAGE_ROWS:])
    assert d2 < oldest_p1


def test_stops_on_short_page_without_extra_call(tmp_path):
    """100행 미만 페이지가 오면 거기서 멈춘다(상장 초기 구간)."""
    days = pd.bdate_range("2024-06-03", periods=40).strftime("%Y%m%d")
    c, sess = _client([{"rt_cd": "0", "output2": _rows(days)}], tmp_path)
    df = c.daily_prices("305090", "20240101", days[-1])
    assert len(df) == 40
    assert len(sess.calls) == 1


# ------------------------------------------------ 2. fail-closed
def test_explicit_rejection_raises(tmp_path):
    c, _ = _client([{"rt_cd": "1", "msg_cd": "EGW00201",
                     "output2": []}], tmp_path)
    with pytest.raises(KISError):
        c.daily_prices("005930", "20240102", "20240131")


def test_non_json_raises_not_retries_forever(tmp_path):
    c, sess = _client([None], tmp_path)   # JSON 아님 -> 즉시 예외
    with pytest.raises(KISError):
        c.daily_prices("005930", "20240102", "20240131")
    assert len(sess.calls) == 1


# ------------------------------------------------ 3. 토큰 캐시
def test_token_is_cached_to_disk_and_reused(tmp_path):
    c1, s1 = _client([], tmp_path)
    c1.token()
    assert s1.token_posts == 1
    # 같은 캐시 파일을 쓰는 새 클라이언트는 재발급하지 않아야 한다
    c2, s2 = _client([], tmp_path)
    c2.token()
    assert s2.token_posts == 0, "유효 토큰이 있는데 재발급했다"
    d = json.load(open(tmp_path / "tok.json", encoding="utf-8"))
    assert d["appkey_tail"] == "K" * 4


# ------------------------------------------------ 4. 수정주가 플래그
def test_adjusted_flag_defaults_to_adjusted(tmp_path):
    days = ["20240102", "20240103"]
    c, sess = _client([{"rt_cd": "0", "output2": _rows(days)}], tmp_path)
    c.daily_prices("005930", days[0], days[-1])
    assert sess.calls[0][1]["FID_ORG_ADJ_PRC"] == "0"
    assert sess.calls[0][1]["FID_PERIOD_DIV_CODE"] == "D"


# ------------------------------------------------ 5. 규약
def test_zero_close_becomes_nan(tmp_path):
    rows = _rows(["20240102"], close=0)
    c, _ = _client([{"rt_cd": "0", "output2": rows}], tmp_path)
    df = c.daily_prices("005930", "20240102", "20240102")
    assert np.isnan(df["close"].iloc[0])


def test_empty_listing_returns_empty_frame(tmp_path):
    c, _ = _client([{"rt_cd": "0", "output2": []}], tmp_path)
    df = c.daily_prices("999999", "20200102", "20200131")
    assert df.empty


# ------------------------------------------------ 6. 교차검증
def test_cross_check_fails_beyond_tolerance(tmp_path, capsys):
    import analysis.fetch_prices_kis as fp
    idx = pd.bdate_range("2024-01-02", periods=5)
    old = pd.DataFrame({"005930": [100., 101, 102, 103, 104]}, index=idx)
    old_p = tmp_path / "old.csv"
    old.to_csv(old_p)
    new = old.copy()
    new.iloc[2, 0] = 102 * 1.01          # 1% 어긋난 셀 하나
    with pytest.raises(SystemExit):
        fp.cross_check(new, str(old_p), tol=1e-6,
                       report=str(tmp_path / "rep.csv"))
    # 통과 케이스 - 동일 데이터
    fp.cross_check(old.copy(), str(old_p), tol=1e-6,
                   report=str(tmp_path / "rep2.csv"))
