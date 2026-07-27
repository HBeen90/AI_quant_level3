# -*- coding: utf-8 -*-
"""develop 통합 테스트 - selection.py(민수님) / weighting.py(민수님) /
rebalance.py(소연) 3모듈 접합 검증."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd
from src import selection, weighting
from src.rebalance import (ConfigV2, MethodologyReviewRequired,
                           assign_weights_v2, select_from_selection,
                           selection_hold_group)

PASS = []


def ok(name):
    PASS.append(name)
    print(f"[OK] {name}")


def kr_snap(rows):
    cols = ["종목명", "코드", "유형", "유동시총", "HBM양산", "HBM노출도",
            "메모리향비중", "HBM공정확인", "위원회확인"]
    return pd.DataFrame(rows, columns=cols)


BASE = [
    ("삼성전자", "005930", "메모리제조", 300e12, True, 0.0, 0.0, False, False),
    ("SK하이닉스", "000660", "메모리제조", 250e12, True, 0.0, 0.0, False, False),
    ("핵심1", "100001", "장비", 30e12, False, 0.60, 0.0, False, False),
    ("핵심2", "100002", "소재", 20e12, False, 0.45, 0.0, False, False),
    ("위성1", "200001", "전공정", 7e12, False, 0.12, 0.90, True, True),
    ("위성2", "200002", "전공정", 5e12, False, 0.08, 0.80, True, True),
]


def test_hold_equals_entry_matches_classify():
    """hold=entry 로 두면 selection.classify_row 와 판정이 완전히 일치해야 한다
    - 유지 판정식이 민수님 규칙(0>A>C·하드 요건)을 정확히 복제했는지 검증."""
    rng = np.random.default_rng(3)
    rows = list(BASE)
    for i in range(30):                      # 무작위 경계 후보 30종
        rows.append((f"R{i}", f"9{i:05d}", "장비", 1e12,
                     False, float(rng.uniform(0, 0.6)),
                     float(rng.uniform(0.4, 1.0)),
                     bool(rng.integers(2)), bool(rng.integers(2))))
    df = kr_snap(rows)
    mine = df.apply(lambda r: selection_hold_group(r, selection.CORE_TH,
                                                   selection.SAT_MEM_TH), axis=1)
    theirs = df.apply(selection.classify_row, axis=1)
    assert (mine == theirs).all(), (mine != theirs).sum()
    ok("hold=entry 판정 == selection.classify_row (규칙 복제 정확성)")


def test_kr_hysteresis():
    """한글 스냅샷 히스테리시스: 기존 핵심 28%는 mid(27%)에서 유지·신규 28%는
    미편입, 하드 요건 상실(공정확인 False)은 유지 임계값과 무관하게 편출."""
    cfg = ConfigV2.with_policy("mid")
    rows = BASE + [("경계기존", "100009", "장비", 10e12, False, 0.28, 0.0, False, False),
                   ("경계신규", "100010", "장비", 10e12, False, 0.28, 0.0, False, False),
                   ("공정상실", "200009", "전공정", 3e12, False, 0.10, 0.85, False, True)]
    prev = {"100009", "200009"}              # 기존: 경계기존·공정상실
    out = select_from_selection(kr_snap(rows), prev, cfg)
    got = set(out["코드"])
    assert "100009" in got                   # 28% >= hold 27% -> 유지
    assert "100010" not in got               # 신규는 30% 필요
    assert "200009" not in got               # 하드 요건 상실 -> 편출
    ok("한글 스냅샷 히스테리시스·하드 탈락 우선")


RICH = BASE + [("핵심3", "100003", "장비", 15e12, False, 0.40, 0.0, False, False),
               ("핵심4", "100004", "테스트", 12e12, False, 0.35, 0.0, False, False)]
# RICH: 핵심 4·위성 2 -> 수용량 = 18%p x 4 + 18% = 90% >= 60% (희소 미발동)


def test_adapter_delegates_to_weighting():
    """assign_weights_v2 == weighting.compute_weights (동일 입력·동일 결과)
    + weighting.verify() 무위반 - 위임이 정확하고 자체 검증도 통과."""
    sel = select_from_selection(kr_snap(RICH), set())
    # 어댑터 경로 (EN 계약)
    members = pd.DataFrame({
        "ticker": sel["코드"],
        "group": sel["군"].map({"앵커": "anchor", "핵심": "core", "위성": "satellite"}),
        "float_mcap": sel["유동시총"]})
    w_mine = assign_weights_v2(members).sort_index()
    # 민수님 공개 진입점 직접 호출
    res = weighting.compute_weights(sel)
    w_theirs = res.set_index("코드")["편입비중"].sort_index()
    assert (w_mine - w_theirs).abs().max() < 1e-12
    issues = weighting.verify(res)
    assert not issues, issues
    anchors = w_mine[["005930", "000660"]].sum()
    assert abs(anchors - 0.40) < 1e-9
    ok("어댑터 위임 일치(1e-12) + weighting.verify 무위반 + 앵커 40%")


def test_scarce_clause_anchor_absorbs():
    """희소 조항: 핵심 2·위성 2 -> 수용량 54% < 60% -> 잔여 6%p 앵커 흡수
    (앵커 합계 46%, 합계 100% 최우선). 위임 결과와 verify() 허용을 확인."""
    sel = select_from_selection(kr_snap(BASE), set())
    members = pd.DataFrame({
        "ticker": sel["코드"],
        "group": sel["군"].map({"앵커": "anchor", "핵심": "core", "위성": "satellite"}),
        "float_mcap": sel["유동시총"]})
    w = assign_weights_v2(members)
    anchors = float(w[["005930", "000660"]].sum())
    assert abs(anchors - 0.46) < 1e-9          # 40% + 흡수 6%p
    assert abs(float(w.sum()) - 1.0) < 1e-12   # 합계 100% 최우선
    issues = weighting.verify(weighting.compute_weights(sel))
    assert not issues, issues                  # 희소 조항은 위반이 아님
    ok("희소 조항: 앵커 46% 흡수·합계 100%·verify 허용")


def test_min_constituents_kr_path():
    """안건 3(확정): 한글 경로도 5종목 미만이면 중단이 아니라
    경고 + 산출 지속(긴급 재심사 개시)."""
    import warnings as _w
    with _w.catch_warnings(record=True) as caught:
        _w.simplefilter("always")
        out = select_from_selection(kr_snap(BASE[:4]), set())
    assert len(out) == 4                       # 편입은 그대로 확정
    assert any("긴급 재심사" in str(c.message) for c in caught)
    ok("한글 경로 5 미만: 경고 + 산출 지속(안건 3)")


def test_iif_pipeline_smoke():
    """IIF 산출까지 무결 통과(인서님 index_calc 인계 규격)."""
    res = weighting.compute_weights(select_from_selection(kr_snap(BASE), set()))
    iif = res.set_index("코드")["IIF"]
    assert (iif > 0).all() and abs(float(iif.max()) - 1.0) < 1e-9
    ok("IIF 산출 스모크 (최대 1.0 정규화)")


def test_group_migration_core_to_sat():
    """군 이동 히스테리시스(조문 4안 - 종목 단위 지위).
    전제: 군 재분류는 파트2 심사(판정 규칙 0>A>C)의 산출이다 - 이 스냅샷의
    수치(노출도 25%·메모리향 80%·공정·위원회)는 파트2 심사가 해당 종목을
    핵심이 아닌 위성으로 재분류하는 입력이며, 엔진은 기존 구성종목 지위를
    인정해 '이동한 군(위성)'의 유지 임계값(67%)을 적용할 뿐이다.
    결과: 편출이 아니라 위성으로 이동하며 잔류."""
    cfg = ConfigV2.with_policy("mid")
    # 메모리향 68%: 위성 '유지'(>=67%)는 충족하지만 '신규'(>=70%)는 미달 -
    # 유지 임계값이 실제로 판정을 가른다(리뷰 P2: 80%면 신규도 편입돼 미검증).
    rows = BASE + [("이동후보", "100020", "장비", 8e12, False, 0.25, 0.68, True, True)]
    prev = {"100020"}                        # 직전까지 핵심으로 편입돼 있던 종목
    out = select_from_selection(kr_snap(rows), prev, cfg)
    row = out[out["코드"] == "100020"]
    assert len(row) == 1, "군 이동 대상이 편출됨"
    assert row["군"].iloc[0] == "위성", f"핵심->위성 이동 실패: {row['군'].iloc[0]}"
    out_new = select_from_selection(kr_snap(rows), set(), cfg)
    assert "100020" not in set(out_new["코드"]), "신규 68%가 편입됨 - 유지 기준 미검증"
    ok("군 이동 핵심->위성(68%): 기존만 유지·신규 미편입 - 유지 기준 실검증")


def test_group_migration_sat_to_core():
    """군 이동 위성->핵심. 전제: 파트2 심사 재분류 - 노출도 28% 상승은
    판정 규칙 순서(0>A>C)상 핵심 재분류의 입력이고, 엔진은 기존 구성종목
    지위를 인정해 핵심 '유지' 임계값(27%)을 적용한다.
    대조: 같은 28%라도 신규는 핵심 신규 기준(30%) 미달 -> 위성 편입."""
    cfg = ConfigV2.with_policy("mid")
    rows = BASE + [("역이동", "200020", "전공정", 4e12, False, 0.28, 0.85, True, True)]
    out_inc = select_from_selection(kr_snap(rows), {"200020"}, cfg)
    assert out_inc[out_inc["코드"] == "200020"]["군"].iloc[0] == "핵심"
    out_new = select_from_selection(kr_snap(rows), set(), cfg)
    assert out_new[out_new["코드"] == "200020"]["군"].iloc[0] == "위성"
    ok("군 이동 위성->핵심: 기존 28%는 핵심 승격·신규 28%는 위성 편입")


def test_selection_numeric_boolean_keeps_anchor():
    """HBM양산 이 float 1.0(pandas 추론)으로 와도 앵커가 탈락하지 않는다.

    과거 _coerce 는 str(1.0)='1.0' 이 참 집합에 없어 True 를 False 로 뒤집어,
    삼성·SK하이닉스가 규칙0에서 조용히 탈락했다(앵커 0개 퇴화)."""
    df = pd.DataFrame([
        {"종목명": "삼성전자", "코드": "005930", "유형": "메모리제조",
         "유동시총": 3e14, "HBM양산": 1.0, "HBM노출도": 0.10, "메모리향비중": 0.55,
         "HBM공정확인": True, "위원회확인": True},
        {"종목명": "핵심A", "코드": "100001", "유형": "장비",
         "유동시총": 3e13, "HBM양산": 0.0, "HBM노출도": 0.50, "메모리향비중": 0.20,
         "HBM공정확인": False, "위원회확인": False},
    ])
    out = selection.select_constituents(df)
    got = out[out["코드"] == "005930"]
    assert len(got) == 1 and got["군"].iloc[0] == "앵커", "float 1.0 앵커가 탈락"
    assert selection._truthy("1.0") is True and selection._truthy(0.0) is False
    ok("selection: 숫자형 Boolean(1.0/'1.0') 앵커 유지 - 조용한 탈락 차단")


def test_weighting_verify_single_anchor_ok_but_normal_cap_enforced():
    """앵커 1종목이면 allocate 는 40%를 배정하고 verify 는 이를 통과시킨다
    (40% > 개별 25%는 앵커 수가 강제한 불가피 - 오탐 아님). 단 앵커 2종목 이상
    정상 상황의 개별 상한 위반은 여전히 잡아야 한다(과잉 면제 방지)."""
    g = np.array(["앵커", "핵심", "핵심", "핵심", "핵심", "핵심"])
    fmc = np.array([3e6, 50e3, 40e3, 30e3, 20e3, 10e3])
    w = weighting.allocate(g, fmc)
    d = pd.DataFrame({"군": g, "유동시총": fmc, "편입비중": w})
    d["IIF"] = weighting.to_iif(w, fmc)
    assert abs(w[g == "앵커"][0] - 0.40) < 1e-9
    assert weighting.verify(d) == [], f"앵커1 오탐: {weighting.verify(d)}"
    g2 = np.array(["앵커", "앵커", "핵심", "핵심", "핵심", "핵심"])
    w2 = np.array([0.30, 0.10, 0.18, 0.18, 0.16, 0.08])
    bad = weighting.verify(pd.DataFrame({"군": g2, "편입비중": w2}))
    assert any("앵커 개별" in m for m in bad), f"정상 상황 앵커 상한 미검출: {bad}"
    ok("weighting.verify: 앵커1 면제·앵커2 개별상한 유지(과잉면제 없음)")


if __name__ == "__main__":
    for fn in [test_hold_equals_entry_matches_classify, test_kr_hysteresis,
               test_adapter_delegates_to_weighting,
               test_scarce_clause_anchor_absorbs,
               test_min_constituents_kr_path, test_iif_pipeline_smoke,
               test_group_migration_core_to_sat, test_group_migration_sat_to_core,
               test_selection_numeric_boolean_keeps_anchor,
               test_weighting_verify_single_anchor_ok_but_normal_cap_enforced]:
        fn()
    print(f"\n{len(PASS)}/10 통합 테스트 통과")
