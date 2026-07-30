# -*- coding: utf-8 -*-
"""생존편향 조사의 오프라인 로직 회귀 테스트.

네트워크 수집(1단계)은 여기서 검증하지 않는다. 검증 대상은 **캐시된 명단으로
소멸 종목을 뽑고 후보를 압축하는 규칙**이며, 이게 틀리면 "후보 0건"이라는
발표 문장이 조사 부실을 정상으로 위장하게 된다. 그래서 0건이 나와야 하는
경우와 나오면 안 되는 경우를 양쪽으로 고정한다.
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from analysis.survivorship_check import (build_candidates, find_disappeared,
                                         ledger_tickers, review_dates)

PASS = []


def ok(name):
    PASS.append(name)
    print(f"[OK] {name}")


def _write(path, tickers):
    pd.DataFrame({"ticker": list(tickers)}).to_csv(
        path, index=False, encoding="utf-8-sig")


def make_fixture(tmp):
    """심사 2시점. 원장은 2종목(A·B), 시장에는 C·D 가 더 있다가 사라진다.

      시점1 시장: A B C D      (반도체지수: A C)
      시점2 시장: A B          -> C·D 소멸, 그중 C 만 반도체
    """
    snap = os.path.join(tmp, "snapshots")
    ev = os.path.join(tmp, "evidence")
    os.makedirs(snap), os.makedirs(ev)
    cols = ["ticker", "name", "group", "exposure", "mem_ratio",
            "float_mcap", "eligible"]
    for ymd in ("20200615", "20201214"):
        pd.DataFrame([["000001", "원장A", "anchor", 0.5, 0.9, 1e12, True],
                      ["000002", "원장B", "core", 0.4, 0.8, 1e12, True]],
                     columns=cols).to_csv(
            os.path.join(snap, f"snapshot_{ymd}.csv"),
            index=False, encoding="utf-8-sig")
    _write(os.path.join(ev, "listed_20200615.csv"),
           ["000001", "000002", "000003", "000004"])
    _write(os.path.join(ev, "listed_20201214.csv"), ["000001", "000002"])
    _write(os.path.join(ev, "semi_20200615.csv"), ["000001", "000003"])
    _write(os.path.join(ev, "semi_20201214.csv"), ["000001"])
    return snap, ev


def test_review_dates_and_ledger():
    tmp = tempfile.mkdtemp()
    try:
        snap, _ = make_fixture(tmp)
        assert review_dates(snap) == ["20200615", "20201214"]
        assert ledger_tickers(snap) == {"000001", "000002"}
        ok("심사일 추출 · 원장 종목 수집")
    finally:
        shutil.rmtree(tmp)


def test_disappearance_detected():
    """앞 시점에 있고 뒤 시점에 없으면 소멸로 잡혀야 한다."""
    tmp = tempfile.mkdtemp()
    try:
        snap, ev = make_fixture(tmp)
        gone = find_disappeared(review_dates(snap), ev)
        assert set(gone["ticker"]) == {"000003", "000004"}, gone
        semi = gone.set_index("ticker")["소멸직전_반도체지수"]
        assert bool(semi["000003"]) and not bool(semi["000004"])
        ok("소멸 종목 검출 · 반도체지수 여부 표시")
    finally:
        shutil.rmtree(tmp)


def test_candidate_compression():
    """후보 = 소멸 ∩ 반도체지수 − 원장. 세 조건이 모두 걸려야 한다."""
    tmp = tempfile.mkdtemp()
    try:
        snap, ev = make_fixture(tmp)
        cand = build_candidates(snap, ev)
        assert list(cand["ticker"]) == ["000003"], cand
        assert "편입자격" in cand.columns and (cand["편입자격"] == "").all()
        ok("후보 압축(반도체지수 교집합·원장 제외) + 판정 기입칸 생성")
    finally:
        shutil.rmtree(tmp)


def test_ledger_member_never_becomes_candidate():
    """원장 종목이 시장 명단에서 빠져도 후보가 되면 안 된다.

    원장 종목은 이미 조사 대상이라 여기 끼면 이중 계상이 되고, '후보 N건'
    이라는 발표 숫자가 부풀려진다.
    """
    tmp = tempfile.mkdtemp()
    try:
        snap, ev = make_fixture(tmp)
        _write(os.path.join(ev, "listed_20201214.csv"), ["000001"])  # 원장B 소멸
        _write(os.path.join(ev, "semi_20200615.csv"),
               ["000001", "000002", "000003"])                       # B도 반도체
        gone = find_disappeared(review_dates(snap), ev)
        assert "000002" in set(gone["ticker"])
        cand = build_candidates(snap, ev)
        assert "000002" not in set(cand["ticker"]), cand
        ok("원장 종목은 소멸해도 후보 제외(이중 계상 차단)")
    finally:
        shutil.rmtree(tmp)


def test_index_history_catches_dropout_then_delist():
    """지수에서 먼저 빠지고 나중에 상폐된 종목을 놓치면 안 된다.

    이 스크립트의 첫 판(2026-07-31)은 '소멸 **직전** 시점의 지수 구성종목'만
    후보로 봤다. 그러면 2021년 반도체지수에 있다가 2022년 지수에서 빠지고
    (상장은 유지) 2023년 상폐된 회사가 통째로 빠진다 - 소멸 직전엔 이미 지수
    밖이기 때문이다. 놓친 후보는 '조사했는데 없었다'로 둔갑하므로, 그 시나리오를
    여기 고정한다.
    """
    tmp = tempfile.mkdtemp()
    try:
        snap = os.path.join(tmp, "snapshots")
        ev = os.path.join(tmp, "evidence")
        os.makedirs(snap), os.makedirs(ev)
        cols = ["ticker", "name", "group", "exposure", "mem_ratio",
                "float_mcap", "eligible"]
        for ymd in ("20200615", "20201214", "20210614"):
            pd.DataFrame([["000001", "원장A", "anchor", 0.5, 0.9, 1e12, True]],
                         columns=cols).to_csv(
                os.path.join(snap, f"snapshot_{ymd}.csv"),
                index=False, encoding="utf-8-sig")
        # 000009: 1시점 반도체지수 -> 2시점 지수에서 빠짐(상장 유지) -> 3시점 소멸
        _write(os.path.join(ev, "listed_20200615.csv"), ["000001", "000009"])
        _write(os.path.join(ev, "listed_20201214.csv"), ["000001", "000009"])
        _write(os.path.join(ev, "listed_20210614.csv"), ["000001"])
        _write(os.path.join(ev, "semi_20200615.csv"), ["000001", "000009"])
        _write(os.path.join(ev, "semi_20201214.csv"), ["000001"])
        _write(os.path.join(ev, "semi_20210614.csv"), ["000001"])

        gone = find_disappeared(review_dates(snap), ev)
        row = gone[gone["ticker"] == "000009"].iloc[0]
        assert not bool(row["소멸직전_반도체지수"]), "직전 기준은 못 잡는 게 맞다"
        assert bool(row["반도체지수_이력"]), "이력 기준은 반드시 잡아야 한다"

        cand = build_candidates(snap, ev)
        assert "000009" in set(cand["ticker"]), cand
        ok("지수 이탈 후 상폐 - 이력 기준이 후보로 포착(직전 기준은 놓침)")
    finally:
        shutil.rmtree(tmp)


def test_missing_cache_fails_closed():
    """캐시가 없으면 조용히 0건이 아니라 중단해야 한다.

    이게 가장 위험한 오답이다 - 수집을 안 돌린 상태에서 '소멸 0건'이 나오면
    조사를 한 것처럼 보이면서 실제로는 아무것도 안 본 것이 된다.
    """
    tmp = tempfile.mkdtemp()
    try:
        snap, ev = make_fixture(tmp)
        os.remove(os.path.join(ev, "listed_20201214.csv"))
        try:
            find_disappeared(review_dates(snap), ev)
            raise AssertionError("캐시 누락인데 통과함")
        except SystemExit:
            pass
        ok("상장 명단 캐시 누락 - fail-closed (빈 결과로 위장 금지)")
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
    print(f"\n{len(PASS)}/{len(PASS)} 생존편향 조사 테스트 통과")
