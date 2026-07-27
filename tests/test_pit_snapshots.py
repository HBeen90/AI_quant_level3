# -*- coding: utf-8 -*-
"""PIT 스냅샷 빌더 단위 테스트 - pykrx 없이 규율 부분만 검증.

가장 중요한 건 as_of_ledger 다. 이게 새면 백테스트 전체가 look-ahead로
오염되고, 그 사실이 성과표 어디에도 안 드러난다. 그래서 여기서 못 박는다.
"""
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from analysis.build_pit_snapshots import (_strict_bool, as_of_ledger,  # noqa: E402
                                          review_pairs, screen, to_snapshot)
from src.rebalance import ConfigV2, select_v2, validate_snapshot  # noqa: E402


def _ledger() -> pd.DataFrame:
    """같은 종목의 판정이 사업연도마다 갱신되고, 공시는 3개월 뒤에 난다."""
    rows = []
    for fy, disc, expo in [(2019, "2020-03-30", 0.20),
                           (2020, "2021-03-30", 0.35),
                           (2021, "2022-03-30", 0.55)]:
        rows.append({"ticker": "000100", "name": "코어테스트",
                     "disclosed_at": disc, "fiscal_year": fy,
                     "sector": "장비", "hbm_massproduction": False,
                     "hbm_exposure": expo, "mem_ratio": 0.5,
                     "process_confirmed": True, "committee_ok": True,
                     "free_float": 0.6, "source": "DART", "admin_issue": False})
    rows.append({"ticker": "000200", "name": "앵커테스트",
                 "disclosed_at": "2020-03-30", "fiscal_year": 2019,
                 "sector": "메모리제조", "hbm_massproduction": True,
                 "hbm_exposure": np.nan, "mem_ratio": np.nan,
                 "process_confirmed": True, "committee_ok": True,
                 "free_float": 0.75, "source": "DART", "admin_issue": False})
    rows.append({"ticker": "000300", "name": "위성테스트",
                 "disclosed_at": "2020-03-30", "fiscal_year": 2019,
                 "sector": "소재", "hbm_massproduction": False,
                 "hbm_exposure": 0.1, "mem_ratio": 0.82,
                 "process_confirmed": False, "committee_ok": True,
                 "free_float": 0.5, "source": "DART", "admin_issue": False})
    d = pd.DataFrame(rows)
    d["disclosed_at"] = pd.to_datetime(d["disclosed_at"])
    return d


def test_as_of_ledger_blocks_lookahead():
    """2020-05-29 심사에 2021-03 공시(FY2020) 판정이 새어 들어오면 안 된다."""
    led = _ledger()
    pit = as_of_ledger(led, pd.Timestamp("2020-05-29"))
    row = pit[pit["ticker"] == "000100"].iloc[0]
    assert row["fiscal_year"] == 2019, "미래 공시가 사용됨 - look-ahead 누수"
    assert abs(row["hbm_exposure"] - 0.20) < 1e-12

    pit2 = as_of_ledger(led, pd.Timestamp("2021-05-31"))
    assert pit2[pit2["ticker"] == "000100"].iloc[0]["fiscal_year"] == 2020
    pit3 = as_of_ledger(led, pd.Timestamp("2020-01-02"))
    assert pit3.empty, "공시 전 시점인데 판정이 나옴"
    print("[OK] as_of_ledger: 공시일 기준 PIT - 미래 사업보고서 차단")


def test_pit_changes_selection_outcome():
    """PIT 여부가 실제 편입 결과를 바꾸는지 - 규율이 형식이 아님을 보인다."""
    led = _ledger()
    facts = pd.DataFrame(
        {"listed": [True] * 3, "close": [1e4] * 3,
         "market_cap": [5e12, 3e14, 8e11], "mcap_rank": [30.0, 2.0, 300.0],
         "adv60": [5e9, 5e11, 3e9], "listed_days": [3000, 9000, 2000]},
        index=pd.Index(["000100", "000200", "000300"], name="ticker"))
    cfg = ConfigV2()

    snap_2020 = to_snapshot(screen(facts, as_of_ledger(led, pd.Timestamp("2020-05-29"))))
    validate_snapshot(snap_2020)
    m20 = set(select_v2(snap_2020, prev_members=set(), cfg=cfg)["members"]["ticker"])

    snap_2022 = to_snapshot(screen(facts, as_of_ledger(led, pd.Timestamp("2022-05-31"))))
    m22 = set(select_v2(snap_2022, prev_members=set(), cfg=cfg)["members"]["ticker"])

    assert "000100" not in m20, "FY2019 노출도 20%인데 2020년에 편입됨"
    assert "000100" in m22, "FY2021 노출도 55%인데 2022년에 미편입"
    assert "000200" in m20 and "000200" in m22, "앵커는 상시 편입"
    assert "000300" not in m20, "공정 미확인 위성이 편입됨(하드요건 완화)"
    print("[OK] PIT가 편입 결과를 실제로 바꿈 (2020 미편입 -> 2022 편입)")


def test_screen_applies_universe_filters():
    """기초 유니버스 필터가 탈락사유로 남는가 - 판정 재생 검증의 재료."""
    led = as_of_ledger(_ledger(), pd.Timestamp("2020-05-29"))
    facts = pd.DataFrame(
        {"listed": [True, True, True],
         "close": [1e4] * 3,
         "market_cap": [100e8, 3e14, 8e11],      # 첫 종목 시총 100억 -> 탈락
         "mcap_rank": [900.0, 2.0, 300.0],
         "adv60": [5e8, 5e11, 3e9],              # 첫 종목 ADV 5억 -> 탈락
         "listed_days": [3000, 9000, 30]},       # 셋째 상장 30일 -> 탈락
        index=pd.Index(["000100", "000200", "000300"], name="ticker"))
    j = screen(facts, led)
    r1 = j.set_index("ticker").loc["000100"]
    assert not r1["eligible"] and "시총<350억" in r1["탈락사유"] \
        and "ADV60<10억" in r1["탈락사유"]
    r3 = j.set_index("ticker").loc["000300"]
    assert "상장<3개월" in r3["탈락사유"]
    assert j.set_index("ticker").loc["000200", "eligible"]
    print("[OK] 기초 유니버스 필터 + 탈락사유 기록")


def test_snapshot_satisfies_engine_contract():
    """산출 스냅샷이 rebalance.validate_snapshot 계약을 그대로 만족하는가."""
    led = as_of_ledger(_ledger(), pd.Timestamp("2022-05-31"))
    facts = pd.DataFrame(
        {"listed": [True] * 3, "close": [1e4] * 3,
         "market_cap": [5e12, 3e14, 8e11], "mcap_rank": [30.0, 2.0, 300.0],
         "adv60": [5e9, 5e11, 3e9], "listed_days": [3000, 9000, 2000]},
        index=pd.Index(["000100", "000200", "000300"], name="ticker"))
    snap = to_snapshot(screen(facts, led))
    validate_snapshot(snap)
    assert snap["ticker"].is_monotonic_increasing, "결정론적 정렬 위반"
    assert snap["eligible"].dtype == bool
    assert (snap.loc[snap["eligible"], "float_mcap"] > 0).all()
    print("[OK] 스냅샷이 엔진 데이터 계약 충족 (validate_snapshot 통과)")


def test_rule_priority_and_strict_boolean():
    """규칙 0>A>C 우선순위와 Boolean fail-closed를 고정한다."""
    dual = pd.DataFrame([{
        "ticker": "000400", "name": "이중충족", "sector": "장비",
        "hbm_massproduction": False, "hbm_exposure": 0.45, "mem_ratio": 0.80,
        "market_cap": 5e12, "free_float": 0.60, "eligible": True,
        "탈락사유": "", "process_confirmed": True, "committee_ok": True,
    }])
    out = to_snapshot(dual)
    assert out.loc[0, "group"] == "core", \
        "핵심·위성 동시 충족 종목은 규칙 A가 C보다 먼저여야 함"
    try:
        _strict_bool(pd.Series(["maybe"]), "eligible")
        raise AssertionError("알 수 없는 Boolean 값이 False로 조용히 변환됨")
    except SystemExit:
        pass
    print("[OK] 규칙 0>A>C 우선순위 · Boolean fail-closed")


def test_audit_opinion_is_hard_exclusion():
    """감사의견 비적정은 노출도와 무관하게 하드 탈락한다."""
    led = as_of_ledger(_ledger(), pd.Timestamp("2022-05-31")).copy()
    led["audit_opinion"] = "적정"
    led.loc[led["ticker"].eq("000100"), "audit_opinion"] = "의견거절"
    facts = pd.DataFrame(
        {"listed": [True] * 3, "close": [1e4] * 3,
         "market_cap": [5e12, 3e14, 8e11], "mcap_rank": [30.0, 2.0, 300.0],
         "adv60": [5e9, 5e11, 3e9], "listed_days": [3000, 9000, 2000]},
        index=pd.Index(["000100", "000200", "000300"], name="ticker"))
    judged = screen(facts, led).set_index("ticker")
    assert not judged.loc["000100", "eligible"]
    assert "감사의견 비적정" in judged.loc["000100", "탈락사유"]
    print("[OK] 감사의견 비적정 하드 탈락")


def test_review_pairs_excludes_future_rebalances():
    """연도 끝까지 만든 근사 달력에서도 INDEX_ASOF 이후 회차는 생성하지 않는다."""
    td = pd.bdate_range("2020-01-01", "2026-12-31")
    pairs = review_pairs(td, 2020, 2026, pd.Timestamp("2026-07-27"))
    assert len(pairs) == 13
    assert pairs[-1][1] == pd.Timestamp("2026-06-15")
    assert all(r <= pd.Timestamp("2026-07-27") for _, r in pairs)
    print("[OK] PIT 스냅샷 미래 정기변경 조회 차단(INDEX_ASOF)")


if __name__ == "__main__":
    test_as_of_ledger_blocks_lookahead()
    test_pit_changes_selection_outcome()
    test_screen_applies_universe_filters()
    test_snapshot_satisfies_engine_contract()
    test_rule_priority_and_strict_boolean()
    test_audit_opinion_is_hard_exclusion()
    test_review_pairs_excludes_future_rebalances()
    print("\n7/7 PIT 규율 테스트 통과")
