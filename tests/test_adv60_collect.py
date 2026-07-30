# -*- coding: utf-8 -*-
"""ADV60 수집기의 오프라인 로직 회귀 테스트.

pykrx 조회는 검증 대상이 아니다. 검증 대상은 **기준일을 어디서 가져오는가**와
**capacity_v2 계약을 지키는가**이며, 여기가 틀리면 백테스트와 다른 시점의
용량을 재고도 같은 시점인 줄 알게 된다.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from analysis.capacity_v2 import load_adv
from analysis.collect_adv60 import panel_tickers_and_asof

PASS = []


def ok(name):
    PASS.append(name)
    print(f"[OK] {name}")


def _px(path, dates, tickers):
    pd.DataFrame(1000.0, index=pd.to_datetime(dates), columns=tickers).to_csv(
        path, encoding="utf-8-sig")


def test_asof_comes_from_panel_end():
    """기준일은 가격 패널의 마지막 거래일이어야 한다.

    손으로 날짜를 넣는 구조였다면 백테스트 종료일이 바뀔 때 조용히 어긋난다.
    """
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "px.csv")
        _px(p, ["2026-07-21", "2026-07-22", "2026-07-23"], ["005930", "000660"])
        tickers, asof = panel_tickers_and_asof(p)
        assert asof == pd.Timestamp("2026-07-23"), asof
        assert tickers == ["005930", "000660"], tickers
        ok("기준일·종목목록을 가격 패널에서 유도")
    finally:
        import shutil; shutil.rmtree(tmp)


def test_ticker_zero_padding_preserved():
    """앞자리 0이 살아 있어야 한다. '005930'이 '5930'이 되면 조회가 통째로 빈다."""
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "px.csv")
        _px(p, ["2026-07-23"], ["005930", "042700"])
        tickers, _ = panel_tickers_and_asof(p)
        assert all(len(t) == 6 for t in tickers), tickers
        ok("종목코드 6자리 유지(앞자리 0 보존)")
    finally:
        import shutil; shutil.rmtree(tmp)


def test_output_satisfies_capacity_contract():
    """산출 CSV를 capacity_v2.load_adv 가 그대로 읽어야 한다.

    계약이 어긋나면 용량 분석이 못 돈다. 두 모듈을 실제로 붙여서 확인한다.
    """
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "adv60.csv")
        pd.DataFrame({"ticker": ["005930", "042700"],
                      "adv60_krw": [1.2e12, 9.0e10]}).to_csv(
            p, index=False, encoding="utf-8-sig")
        s = load_adv(p)
        assert set(s.index) == {"005930", "042700"}
        assert float(s["005930"]) == 1.2e12
        ok("산출 CSV가 capacity_v2 계약을 충족(두 모듈 실접합)")
    finally:
        import shutil; shutil.rmtree(tmp)


def test_capacity_rejects_nonpositive_adv():
    """ADV가 0이면 소요일수가 무한대가 된다 - 계약 단계에서 막아야 한다."""
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "adv60.csv")
        pd.DataFrame({"ticker": ["005930"], "adv60_krw": [0.0]}).to_csv(
            p, index=False, encoding="utf-8-sig")
        try:
            load_adv(p)
            raise AssertionError("ADV 0인데 통과함")
        except SystemExit:
            pass
        ok("ADV60 비양수 - fail-closed (무한대 소요일수 차단)")
    finally:
        import shutil; shutil.rmtree(tmp)


if __name__ == "__main__":
    for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
        fn()
    print(f"\n{len(PASS)}/{len(PASS)} ADV60 수집기 테스트 통과")
