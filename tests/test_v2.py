# -*- coding: utf-8 -*-
"""v2 브랜치 테스트 - 명세 7항목."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from src.rebalance import (ANCHOR, CORE, SAT, AdhocManagerV2, BUFFER_POLICIES,
                          ConfigV2, MethodologyReviewRequired,
                          regular_rebalance_v2, select_v2)

PASS = []


def ok(name):
    PASS.append(name)
    print(f"[OK] {name}")


def snap(rows):
    df = pd.DataFrame(rows, columns=["ticker", "name", "group", "exposure",
                                     "mem_ratio", "float_mcap", "eligible"])
    df["eligible"] = df["eligible"].astype(bool)
    return df


BASE = [
    ("000001", "앵커1", ANCHOR, np.nan, np.nan, 300e12, True),
    ("000002", "앵커2", ANCHOR, np.nan, np.nan, 250e12, True),
    ("100001", "핵심1", CORE, 0.60, np.nan, 30e12, True),
    ("100002", "핵심2", CORE, 0.45, np.nan, 20e12, True),
    ("200001", "위성1", SAT, 0.12, 0.90, 7e12, True),
    ("200002", "위성2", SAT, 0.08, 0.80, 5e12, True),
]


def test_entry_boundary():
    """신규 29.9%는 미편입, 30.0%는 편입."""
    rows = BASE + [("100009", "경계", CORE, 0.299, np.nan, 10e12, True)]
    out = select_v2(snap(rows), prev_members=set())
    assert "100009" not in set(out["members"]["ticker"])
    rows[-1] = ("100009", "경계", CORE, 0.300, np.nan, 10e12, True)
    out = select_v2(snap(rows), prev_members=set())
    assert "100009" in set(out["members"]["ticker"])
    ok("entry: 29.9% 미편입 / 30.0% 편입")


def test_hold_boundary_wide():
    """넓은 버퍼(hold 25%): 기존 25.0% 유지, 24.9% 편출."""
    cfg = ConfigV2.with_policy("wide")
    prev = {"100009"}
    rows = BASE + [("100009", "경계", CORE, 0.250, np.nan, 10e12, True)]
    out = select_v2(snap(rows), prev, cfg)
    assert "100009" in set(out["members"]["ticker"])
    rows[-1] = ("100009", "경계", CORE, 0.249, np.nan, 10e12, True)
    out = select_v2(snap(rows), prev, cfg)
    assert "100009" not in set(out["members"]["ticker"])
    # 대칭: 버퍼 없음 정책이면 25.0%도 편출
    out = select_v2(snap([("100009", "경계", CORE, 0.250, np.nan, 10e12, True)]
                         + BASE), prev, ConfigV2.with_policy("none"))
    assert "100009" not in set(out["members"]["ticker"])
    ok("hold(wide): 25.0% 유지 / 24.9% 편출 / none 정책은 25.0%도 편출")


def test_hard_screen_overrides_buffer():
    """하드 스크린 탈락(eligible=False)은 버퍼와 무관하게 편출."""
    prev = {"100001"}
    rows = [r for r in BASE]
    rows[2] = ("100001", "핵심1", CORE, 0.60, np.nan, 30e12, False)  # 관리종목 등
    out = select_v2(snap(rows), prev, ConfigV2.with_policy("wide"))
    assert "100001" not in set(out["members"]["ticker"])
    ok("하드 탈락이 버퍼에 우선")


def test_variable_count_and_determinism():
    """종목 수가 8/11/15로 달라도 배분 성립 + 입력 순서 무관 결정론."""
    for n_core, n_sat in [(4, 2), (6, 3), (9, 4)]:
        rows = BASE[:2]
        rows += [(f"1{i:05d}", f"핵심{i}", CORE, 0.30 + 0.05 * i, np.nan,
                  (30 - i) * 1e12, True) for i in range(n_core)]
        rows += [(f"2{i:05d}", f"위성{i}", SAT, 0.05 + 0.02 * i, 0.75 + 0.03 * i,
                  (8 - i) * 1e12, True) for i in range(n_sat)]
        df = snap(rows)
        out1 = regular_rebalance_v2(df, set())
        out2 = regular_rebalance_v2(df.sample(frac=1, random_state=7), set())
        w1, w2 = out1["weights"].sort_index(), out2["weights"].sort_index()
        assert out1["n"] == 2 + n_core + n_sat
        assert abs(w1.sum() - 1) < 1e-9
        assert (w1 - w2).abs().max() < 1e-12          # 순서 무관
        anchors = w1[["000001", "000002"]].sum()
        assert abs(anchors - 0.40) < 1e-9             # 40/60 불변식
        assert (w1.drop(["000001", "000002"]) <= 0.18 + 1e-9).all()
    ok("가변 종목 수(8/11/15) 배분 + 순서 무관 결정론 + 40/60·캡 준수")


def test_adhoc_no_replacement():
    """수시편출 후 대체 편입 없음 - 종목 수 감소·정규화만."""
    out = regular_rebalance_v2(snap(BASE), set())
    vm = AdhocManagerV2(out["weights"].copy(),
                        out["members"].set_index("ticker")["group"].copy())
    n0 = len(vm.weights)
    vm.apply_exclusions([("200001", "상장폐지")])
    assert len(vm.weights) == n0 - 1
    assert abs(vm.weights.sum() - 1) < 1e-12
    assert not any(e[0] == "fill" for e in vm.log)    # 충원 이벤트 자체가 없음
    ok("수시편출 무대체 · 정규화(제수 동치)")


def test_under_min_continuity():
    """안건 3(확정): 5종목 미만 -> 중단이 아니라 산출 지속 + 플래그.
    수시편출로 6->4가 되어도 예외 없이 정규화되고 긴급심사 플래그가 선다.
    정기변경도 자격자 4종목이면 경고와 함께 비중을 산출한다(under_min=True).
    전 종목 편출만이 유일한 산출 불가 사유다."""
    import warnings as _w
    out = regular_rebalance_v2(snap(BASE), set())     # 6종목
    vm = AdhocManagerV2(out["weights"].copy(),
                        out["members"].set_index("ticker")["group"].copy())
    with _w.catch_warnings():
        _w.simplefilter("ignore")
        vm.apply_exclusions([("200001", "합병"), ("200002", "관리종목")])  # 6->4
    assert len(vm.weights) == 4
    assert abs(vm.weights.sum() - 1) < 1e-12
    assert "under_min_emergency_review" in vm.flags
    few = snap(BASE[:2] + [("100001", "핵심1", CORE, 0.6, np.nan, 30e12, True),
                           ("200001", "위성1", SAT, 0.1, 0.9, 7e12, True)])
    with _w.catch_warnings():
        _w.simplefilter("ignore")
        res = regular_rebalance_v2(few, set())        # 자격자 4종목
    assert res["under_min"] and abs(res["weights"].sum() - 1) < 1e-9
    try:                                              # 전 종목 편출은 불가
        vm2 = AdhocManagerV2(out["weights"].copy(),
                             out["members"].set_index("ticker")["group"].copy())
        with _w.catch_warnings():
            _w.simplefilter("ignore")
            vm2.apply_exclusions([(tk, "x") for tk in out["weights"].index])
        raise AssertionError("전 종목 편출이 허용됨")
    except MethodologyReviewRequired:
        pass
    ok("5 미만 산출 지속·플래그(수시·정기) / 전 종목 편출만 불가")


def test_policy_turnover_ordering():
    """버퍼 정책별 교체 억제: 모든 정책에 '동일한' 노출도 경로를 적용해
    wide <= mid <= narrow <= none 단조성을 검사한다(리뷰 반영 - 정책별로
    난수 경로가 달라지는 교란 제거). 이 테스트는 경로 통제된 교체 '건수'만
    검사하며, 드리프트 반영 실회전율의 단조성은 analysis/sensitivity_v2.py
    (simulate_index 경유)가 검증한다."""
    rng = np.random.default_rng(42)
    paths = {(t, j): 0.28 + 0.04 * ((t + j) % 2) + rng.normal(0, 0.002)
             for t in range(12) for j in range(4)}     # 사전 생성 -> 전 정책 공유
    churn = {}
    for pol in ("none", "narrow", "mid", "wide"):
        cfg = ConfigV2.with_policy(pol)
        prev, swaps = set(), 0
        for t in range(12):
            rows = list(BASE)
            for j in range(4):
                rows.append((f"3{j:05d}", f"진동{j}", CORE, paths[(t, j)],
                             np.nan, 10e12, True))
            out = select_v2(snap(rows), prev, cfg)
            swaps += len(out["added"] - ({"000001", "000002"} if t == 0 else set())) \
                + len(out["dropped"])
            prev = set(out["members"]["ticker"])
        churn[pol] = swaps
    assert churn["wide"] <= churn["mid"] <= churn["narrow"] <= churn["none"]
    assert churn["wide"] < churn["none"]
    ok(f"정책별 교체 건수 단조성(동일 경로) {churn}")


def test_satellite_admitted_without_exposure():
    """위성 편입 요건은 mem_ratio + eligible 뿐 - exposure 결측/0이어도 편입
    (문서에 없는 exposure>0 조건 제거 확인)."""
    rows = BASE + [("200009", "위성X", SAT, np.nan, 0.85, 3e12, True),
                   ("200010", "위성Y", SAT, 0.0, 0.75, 2e12, True)]
    out = select_v2(snap(rows), set())
    got = set(out["members"]["ticker"])
    assert {"200009", "200010"} <= got
    ok("위성 exposure 결측/0 편입 허용(요건 외 조건 제거)")


def test_drift_missing_return_fails_closed():
    """활성 종목 수익률 결측 -> 0% 대체 금지, 예외 발생."""
    out = regular_rebalance_v2(snap(BASE), set())
    vm = AdhocManagerV2(out["weights"].copy(),
                        out["members"].set_index("ticker")["group"].copy())
    r = pd.Series(0.01, index=out["weights"].index).drop("100001")
    try:
        vm.drift(r)
        raise AssertionError("예외가 발생해야 함")
    except ValueError as e:
        assert "100001" in str(e)
    ok("drift 수익률 결측 fail-closed")


def test_drift_infinite_return_fails_closed():
    """활성 종목 수익률 inf(0원 가격 복구 등) -> 조용한 NaN 오염 대신 예외.

    과거 가드는 NaN 만 막아, inf 를 곱하면 weights 가 inf -> NaN 으로 오염된 채
    캡·회전율 판정이 이어졌다(실패한 티도 안 남). +inf 와 -inf 둘 다 막는지 본다.
    """
    out = regular_rebalance_v2(snap(BASE), set())
    for bad_val in (np.inf, -np.inf):
        vm = AdhocManagerV2(out["weights"].copy(),
                            out["members"].set_index("ticker")["group"].copy())
        r = pd.Series(0.01, index=out["weights"].index)
        r.loc["100001"] = bad_val
        try:
            vm.drift(r)
            raise AssertionError(f"{bad_val} 이 통과됨 - inf 미차단")
        except ValueError as e:
            assert "100001" in str(e)
        # 오염 여부 직접 확인: 예외로 막혔으므로 weights 는 유한·정규화 유지
        assert vm.weights.notna().all() and abs(vm.weights.sum() - 1) < 1e-12
    ok("drift 수익률 inf(±) fail-closed - 조용한 NaN 오염 차단")


if __name__ == "__main__":
    for fn in [test_entry_boundary, test_hold_boundary_wide,
               test_hard_screen_overrides_buffer,
               test_variable_count_and_determinism,
               test_adhoc_no_replacement, test_under_min_continuity,
               test_policy_turnover_ordering,
               test_satellite_admitted_without_exposure,
               test_drift_missing_return_fails_closed,
               test_drift_infinite_return_fails_closed]:
        fn()
    print(f"\n{len(PASS)}/10 테스트 통과 - v2 명세 검증 완료")
