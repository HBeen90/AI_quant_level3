# -*- coding: utf-8 -*-
"""run_backtest 드라이버 스모크 - pykrx 없이 전 배선을 검증한다.

가격 캐시(--prices-cache)와 --no-benchmark 경로를 쓰면 네트워크 없이도
스냅샷 로드 -> 캘린더 대조 -> 커버리지 진단 -> 이벤트 스케줄 -> 지수 재생 ->
지표·정책비교까지 전부 실행된다. CI에 그대로 넣을 수 있다.
"""
import os
import subprocess
import sys
import tempfile
from unittest.mock import patch

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from analysis.index_calendar import (announce_date, rebalance_dates,  # noqa: E402
                                     second_thursday, selection_dates)
import contextlib  # noqa: E402
import io  # noqa: E402

from analysis.run_backtest import (choose_benchmark, coverage_report,  # noqa: E402
                                   fetch_benchmark, fetch_benchmarks, fetch_prices,
                                   load_benchmark_config, load_snapshots,
                                   resolve_benchmark_spec, theme_relevance_history)
from analysis.resolve_benchmark_code import classify_target_name  # noqa: E402
from analysis import capacity_v2  # noqa: E402
from backtest.backtest import correlation  # noqa: E402


def test_calendar_matches_methodology():
    """조문 대조: 만기일=둘째 목요일, 시행일=익주 첫 거래일, 개편일=D-2."""
    assert second_thursday(2020, 6) == pd.Timestamp("2020-06-11")
    assert second_thursday(2023, 12) == pd.Timestamp("2023-12-14")
    td = pd.bdate_range("2020-01-01", "2026-12-31")
    rebs = rebalance_dates(td)
    # 방법론 기준일 2020-06-15 가 캘린더 조문에서 그대로 재생되는가
    assert pd.Timestamp("2020-06-15") in rebs, "기준일이 조문에서 재생되지 않음"
    assert all(d.dayofweek == 0 for d in rebs), "시행일이 월요일(익주 첫 영업일)이 아님"
    assert len(rebs) == 14                      # 2020~2026 x 연 2회
    assert announce_date(td, pd.Timestamp("2020-06-15")) == pd.Timestamp("2020-06-11")
    sels = selection_dates(td)
    assert pd.Timestamp("2020-05-29") in sels   # 5월 마지막 영업일
    print("[OK] 캘린더 조문 대조 (기준일 2020-06-15 재생 · 개편일 D-2)")


def test_holiday_shift_is_deterministic():
    """익주 월요일이 휴장이면 그 다음 거래일로 밀린다(거래일 인덱스가 캘린더)."""
    td = pd.bdate_range("2020-01-01", "2026-12-31")
    td = td[td != pd.Timestamp("2020-06-15")]        # 월요일 휴장 가정
    assert pd.Timestamp("2020-06-16") in rebalance_dates(td)
    print("[OK] 휴장 시 익영업일 이월 결정론")


def _make_fixture(root: str, n_days: int = 1600, seed: int = 3) -> tuple:
    """합성 스냅샷 6회 + 가격 캐시. 실제 파이프라인과 동일한 파일 계약."""
    rng = np.random.default_rng(seed)
    codes = [f"{i:06d}" for i in range(1, 15)]
    days = pd.bdate_range("2020-06-15", periods=n_days)
    px = pd.DataFrame(
        50000 * np.exp(np.cumsum(rng.normal(3e-4, 0.02, (n_days, len(codes))), axis=0)),
        index=days, columns=codes)
    cache = os.path.join(root, "px.csv")
    px.to_csv(cache, encoding="utf-8-sig")

    snap_dir = os.path.join(root, "snapshots")
    os.makedirs(snap_dir, exist_ok=True)
    rebs = [d for d in rebalance_dates(days) if d in days.tolist()][:6]
    for d in rebs:
        expo = np.concatenate([[np.nan, np.nan],
                               rng.uniform(0.20, 0.90, 8), [np.nan] * 4])
        mem = np.concatenate([[np.nan] * 10, rng.uniform(0.55, 0.95, 4)])
        pd.DataFrame({
            "ticker": codes, "name": [f"N{c}" for c in codes],
            "group": ["anchor"] * 2 + ["core"] * 8 + ["satellite"] * 4,
            "exposure": expo, "mem_ratio": mem,
            "float_mcap": rng.uniform(1e12, 3e14, len(codes)),
            "eligible": True,
            "selection_date": (d - pd.Timedelta(days=16)).date(),
            "ff_market_cap_asof": (d - pd.Timedelta(days=16)).date(),
            "ff_market_cap_source": "synthetic-fixture",
            "free_float_asof": (d - pd.Timedelta(days=16)).date(),
            "code_commit": "0000000",
        }).to_csv(os.path.join(snap_dir, f"snapshot_{d.strftime('%Y%m%d')}.csv"),
                  index=False, encoding="utf-8-sig")
    return snap_dir, cache, rebs


def test_driver_end_to_end():
    """드라이버를 실제 프로세스로 실행 - 정책 4안 비교까지 산출되는가."""
    with tempfile.TemporaryDirectory() as root:
        snap_dir, cache, rebs = _make_fixture(root)
        out = os.path.join(root, "out")
        cmd = [sys.executable, os.path.join(HERE, "analysis", "run_backtest.py"),
               "--snapshots", snap_dir, "--prices-cache", cache,
               "--no-benchmark", "--policy", "all", "--require-lineage",
               "--out", out]
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           env={**os.environ, "PYTHONIOENCODING": "utf-8",
                                "INDEX_ASOF": "2026-07-26"})
        assert p.returncode == 0, f"드라이버 실패\nSTDOUT:{p.stdout}\nSTDERR:{p.stderr}"
        for f in ("index_level.csv", "change_history.csv", "event_log.csv",
                  "theme_relevance.csv", "policy_comparison.csv"):
            assert os.path.exists(os.path.join(out, f)), f"산출물 누락: {f}"
        lv = pd.read_csv(os.path.join(out, "index_level.csv"),
                         index_col=0, parse_dates=True)
        assert lv["level"].notna().all() and (lv["level"] > 0).all()
        assert abs(lv["level"].iloc[0] - 1000.0) < 1e-9, "기준지수 1000 아님"
        assert lv.index.max() <= pd.Timestamp("2026-07-26"), \
            "가격 캐시가 INDEX_ASOF 이후까지 재생됨"
        tbl = pd.read_csv(os.path.join(out, "policy_comparison.csv"), index_col=0)
        assert set(tbl.index) == {"none", "narrow", "mid", "wide"}
        rel = pd.read_csv(os.path.join(out, "theme_relevance.csv"))
        assert {"score", "alert_line", "below_alert"} <= set(rel.columns)
        assert np.allclose(rel["alert_line"], 0.35)
        # 버퍼가 넓을수록 회전율이 낮아야 한다(히스테리시스의 존재 이유)
        assert tbl.loc["wide", "연율화회전율(편도)"] <= tbl.loc["none", "연율화회전율(편도)"] + 1e-9, \
            "넓은 버퍼가 회전율을 못 낮춤 - 정책 배선 확인"
        assert (tbl["편입 건수"] == tbl["편출 건수"]).all(), \
            "고정 종목 수 시나리오에서 최초 구성을 편입 건수로 세면 안 됨"
        print("[OK] 드라이버 end-to-end (지수 시계열 · 정책 4안 비교 · 계보 강제)")
        print(tbl[["연율화회전율(편도)", "편입 건수", "편출 건수"]].round(4).to_string())


def test_coverage_report_flags_gaps():
    """상장 전 구간(가격 NaN)을 예외 전에 사람이 읽을 표로 먼저 낸다."""
    with tempfile.TemporaryDirectory() as root:
        snap_dir, cache, rebs = _make_fixture(root)
        px = pd.read_csv(cache, index_col=0, parse_dates=True)
        px.columns = [str(c).zfill(6) for c in px.columns]
        px.loc[:px.index[400], "000014"] = np.nan       # 늦은 상장 모사
        px.to_csv(cache, encoding="utf-8-sig")
        out = os.path.join(root, "out")
        p = subprocess.run(
            [sys.executable, os.path.join(HERE, "analysis", "run_backtest.py"),
             "--snapshots", snap_dir, "--prices-cache", cache, "--no-benchmark",
             "--coverage-only", "--out", out],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        assert p.returncode == 0, p.stderr
        cov = pd.read_csv(os.path.join(out, "coverage_report.csv"))
        assert (cov["ticker"].astype(str).str.zfill(6) == "000014").any(), \
            "결측 종목이 커버리지 리포트에 안 잡힘"

        # 다음 시행일은 앞 구간에서 제외되어 경계 결측이 이중 계상되지 않는다.
        d = pd.bdate_range("2026-01-02", periods=4)
        q = pd.DataFrame({"000001": [1.0, 1.0, np.nan, 1.0]}, index=d)
        s = pd.DataFrame({"ticker": ["000001"], "eligible": [True]})
        boundary = coverage_report(q, {d[0]: s, d[2]: s})
        assert len(boundary) == 1 and boundary.iloc[0]["시행일"] == d[2].date(), \
            "다음 시행일 결측이 인접 두 구간에 이중 계상됨"
        print("[OK] 커버리지 진단이 상장 전 결측을 사전 표면화")


def test_snapshot_loader_fails_closed():
    """잘못된 Boolean과 미래 시점 계보를 조용히 통과시키지 않는다."""
    with tempfile.TemporaryDirectory() as root:
        snap_dir, _, _ = _make_fixture(root)
        f = sorted(os.path.join(snap_dir, x) for x in os.listdir(snap_dir))[0]
        original = pd.read_csv(f, dtype={"ticker": str})

        bad_bool = original.copy()
        bad_bool["eligible"] = bad_bool["eligible"].astype(object)
        bad_bool.loc[0, "eligible"] = "maybe"
        bad_bool.to_csv(f, index=False, encoding="utf-8-sig")
        try:
            load_snapshots(snap_dir, require_lineage=True)
            raise AssertionError("알 수 없는 eligible 값이 False로 변환됨")
        except SystemExit:
            pass

        bad_time = original.copy()
        effective = pd.Timestamp(os.path.basename(f)[9:17])
        bad_time["selection_date"] = (effective + pd.Timedelta(days=1)).date()
        bad_time["ff_market_cap_asof"] = bad_time["selection_date"]
        bad_time.to_csv(f, index=False, encoding="utf-8-sig")
        try:
            load_snapshots(snap_dir, require_lineage=True)
            raise AssertionError("미래 selection_date가 통과됨")
        except SystemExit:
            pass
    print("[OK] 스냅샷 Boolean·PIT 계보 fail-closed")


def test_correlation_does_not_fill_missing_returns():
    """벤치마크 결측을 0% 수익률로 채우지 않고 공통 관측치만 쓴다."""
    d = pd.bdate_range("2024-01-02", periods=50)
    ri = np.sin(np.arange(50) / 4) * 0.01 + 0.001
    rb = np.cos(np.arange(50) / 5) * 0.008 + 0.0005
    index_level = pd.Series(100 * np.cumprod(1 + ri), index=d)
    benchmark = pd.Series(100 * np.cumprod(1 + rb), index=d)
    benchmark.iloc[20] = np.nan
    pair = pd.concat([
        index_level.pct_change(fill_method=None).rename("i"),
        benchmark.pct_change(fill_method=None).rename("b"),
    ], axis=1).dropna()
    expected = float(pair["i"].corr(pair["b"]))
    got = correlation(index_level, benchmark, min_obs=30)
    assert abs(got - expected) < 1e-12
    print("[OK] 상관계수는 결측 0% 보정 없이 일간 수익률 공통구간 사용")


def test_capacity_real_delta_w_path_is_wired():
    """capacity_v2 의 정기·캡 재생 |Δw| 경로가 배선돼 동작한다.

    과거 main() 은 --events 만 받고 쓰지 않아, 광고한 실측 용량 표가 산출되지
    않았다(시나리오 표만 출력). 스냅샷+가격+ADV 로 이벤트를 재생성해 종목별
    소요일수·함의상한이 나오고, 소요일수 공식이 재현되는지 못박는다.
    """
    with tempfile.TemporaryDirectory() as root:
        snap_dir, cache, _ = _make_fixture(root)
        codes = [f"{i:06d}" for i in range(1, 15)]
        adv = os.path.join(root, "adv.csv")
        pd.DataFrame({"ticker": codes,
                      "adv60_krw": np.linspace(15, 500, len(codes)) * 1e8}
                     ).to_csv(adv, index=False)
        td = capacity_v2.real_capacity(snap_dir, cache, adv, aum_eok=3000,
                                       participation=0.10, policy="mid",
                                       max_days=5.0)
        assert not td.empty and {"abs_delta_w", "소요일수", "함의상한"} <= set(td.columns)
        assert (td["소요일수"] > 0).all() and (td["abs_delta_w"] > 0).all()
        # 소요일수 = AUM × |Δw| / (ADV60억 × 참여율) - 한 행 재현
        r = td.iloc[0]
        expect = 3000 * r["abs_delta_w"] / (r["adv60_억"] * 0.10)
        assert abs(expect - r["소요일수"]) < 0.02, f"공식 불일치: {expect} vs {r['소요일수']}"
        # 하나라도 빠지면 실측 경로 대신 시나리오 표(부분 지정 방어는 main에서)
        assert capacity_v2.real_capacity.__doc__ is not None
    print(f"[OK] capacity_v2 정기·캡 재생 |Δw| 경로({len(td)}행·공식 재현)")


def test_benchmark_config_fixes_code_and_matches_series():
    """벤치마크 지정 파일: CONFIRMED 시 코드 고정·PR/TR 계열 매칭·공란 fail-closed.
    미확정(PROVISIONAL) 시 이름기반 잠정으로 떨어진다."""
    # 동봉 템플릿은 PROVISIONAL 이어야 한다(위원회 미확정 상태로 출하)
    cfg_path = os.path.join(HERE, "data", "benchmark.yaml")
    cfg = load_benchmark_config(cfg_path)
    assert cfg is not None and str(cfg["status"]).upper() == "PROVISIONAL", \
        "출하 템플릿은 PROVISIONAL(위원회 미확정)이어야 함"
    assert resolve_benchmark_spec(cfg, "pr")["status"] == "provisional"

    # CONFIRMED: 모드에 따라 PR/TR 코드 자동 선택
    conf = {"status": "CONFIRMED", "fallback_keyword": "반도체",
            "headline_return_type": "PR", "effective_date": "2026-07-28",
            "resolved_by": "지수위원회 2026-07-28",
            "primary": {"pr_name": "KRX 반도체", "tr_name": "KRX 반도체 TR",
                        "pr_code": "1KRX01", "tr_code": "1KRX01T"}}
    assert resolve_benchmark_spec(conf, "pr") == \
        {"status": "confirmed", "code": "1KRX01", "name": "KRX 반도체", "return_type": "PR"}
    assert resolve_benchmark_spec(conf, "gross_tr") == \
        {"status": "confirmed", "code": "1KRX01T", "name": "KRX 반도체 TR",
         "return_type": "TR"}
    assert resolve_benchmark_spec(conf, "both")["return_type"] == "PR"   # both→헤드라인 PR

    # CONFIRMED 인데 해당 계열 코드/이름이 비면 중단
    base = {"status": "CONFIRMED", "headline_return_type": "PR",
            "effective_date": "2026-07-28", "resolved_by": "회의록",
            "primary": {"pr_name": "KRX 반도체", "pr_code": "1KRX01"}}
    for bad in (
        {**base, "primary": {"pr_name": "KRX 반도체", "pr_code": ""}},
        {**base, "primary": {"pr_name": "", "pr_code": "1KRX01"}},
        {**base, "effective_date": ""},
        {**base, "resolved_by": ""},
    ):
        try:
            resolve_benchmark_spec(bad, "pr")
            raise AssertionError("코드/이름 공란 CONFIRMED 가 통과함")
        except SystemExit:
            pass
    # 설정 없음 → 전달 키워드로 잠정
    assert resolve_benchmark_spec(None, "pr", default_keyword="2차전지") == \
        {"status": "provisional", "keyword": "2차전지", "return_type": "PR"}
    print("[OK] 벤치마크 코드 고정·PR/TR 매칭·공란 fail-closed·잠정 폴백")


def test_benchmark_resolver_aliases_and_distinct_series_names():
    """resolver는 한·영 표기를 허용하되 부분일치로 코드를 확정하지 않는다."""
    assert classify_target_name("KRX 반도체") == "PR"
    assert classify_target_name("KRX Semicon") == "PR"
    assert classify_target_name("KRX 반도체 TR") == "TR"
    assert classify_target_name("  KRX   Semicon TR  ") == "TR"
    assert classify_target_name("KRX 반도체 선물") is None
    print("[OK] 벤치마크 resolver 한·영 별칭·PR/TR 표기 분리")


def test_benchmark_choice_is_deterministic_and_prefers_pr():
    """벤치마크 선택이 후보 순서에 무관하고 PR(비 TR) 최단명을 고른다."""
    cands = [("KRX", "1", "KRX 반도체 TR"),          # TR - 배제 우선
             ("KOSPI", "2", "KOSPI 200 반도체"),      # PR 이나 이름 김
             ("KRX", "3", "KRX 반도체"),              # PR·최단 → 채택
             ("테마", "4", "반도체 테마 TR")]
    chosen = choose_benchmark(cands)
    assert chosen == ("KRX", "3", "KRX 반도체"), chosen
    # 입력 순서를 뒤집어도 같은 선택(결정론)
    assert choose_benchmark(list(reversed(cands))) == chosen
    assert choose_benchmark(cands, "TR") == ("테마", "4", "반도체 테마 TR")
    # 전부 TR 뿐이면 그중 결정론적으로 하나(최단명 → KRX)
    only_tr = [("KOSPI", "8", "KOSPI 반도체 TR"), ("KRX", "9", "KRX 반도체 TR")]
    assert choose_benchmark(only_tr) == ("KRX", "9", "KRX 반도체 TR")
    print("[OK] 벤치마크 선택 결정론·PR 우선(후보 순서 무관)")


def test_both_mode_fetches_distinct_pr_tr_benchmarks_and_caches():
    """both 모드는 PR 한 계열을 두 번 쓰지 않고 PR/TR을 각각 조회한다."""
    pr = pd.Series([100.0], index=[pd.Timestamp("2026-01-02")], name="PR")
    tr = pd.Series([101.0], index=[pd.Timestamp("2026-01-02")], name="TR")
    with patch("analysis.run_backtest.fetch_benchmark",
               side_effect=[pr, tr]) as mocked:
        got = fetch_benchmarks(
            "20260101", "20261231", "반도체", "cache.csv", "both", "bm.yaml")
    assert got["pr"] is pr and got["gross_tr"] is tr
    calls = mocked.call_args_list
    assert calls[0].args[3] == "cache_pr.csv" and calls[0].kwargs["mode"] == "pr"
    assert calls[1].args[3] == "cache_tr.csv" and calls[1].kwargs["mode"] == "gross_tr"
    print("[OK] both 모드 PR/TR 별도 조회·캐시 분리")


def test_price_cache_staleness_fail_closed_benchmark_warns():
    """가격 캐시: 낡으면(연휴보다 큰 공백) fail-closed. 벤치마크: 부분겹침은
    정상(지수 출범 늦음)이라 하드페일이 아니라 경고 후 사용한다."""
    with tempfile.TemporaryDirectory() as d:
        cache = os.path.join(d, "px.csv")
        days = pd.bdate_range("2020-06-15", "2025-12-31")     # 캐시는 2025-12 까지만
        px = pd.DataFrame(50000.0, index=days, columns=["000001", "000002"])
        px.to_csv(cache, encoding="utf-8-sig")
        try:                                                   # 요청 끝 2026-07 → 207일 공백 > 16
            fetch_prices(["000001", "000002"], "20200615", "20260726", cache)
            raise AssertionError("낡은 가격 캐시가 통과함")
        except SystemExit as exc:
            assert "충분히 덮지" in str(exc)
        got = fetch_prices(["000001"], "20200615", "20251231", cache)
        assert got.index.max() == pd.Timestamp("2025-12-31")
        # 연휴 규모(<=16일) 끝단 공백은 오발동하지 않아야 한다(추석 뒤 as_of 등)
        import contextlib as _c
        buf0 = io.StringIO()
        with _c.redirect_stdout(buf0):
            fetch_prices(["000001"], "20200615", "20260112", cache)  # 12일 공백
        assert "FAIL" not in buf0.getvalue()

        bench_cache = os.path.join(d, "benchmark.csv")
        pd.Series(100.0, index=days, name="BM").to_csv(
            bench_cache, encoding="utf-8-sig")
        # 벤치마크 부분겹침: 하드페일 금지 → 경고 후 짧은 시리즈 반환
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            bench = fetch_benchmark("20200615", "20260726", cache=bench_cache)
        assert "[주의]" in buf.getvalue() and bench.index.max() == pd.Timestamp("2025-12-31")
        # 완전 커버면 경고 없음(오경보 방지)
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            fetch_benchmark("20200615", "20251231", cache=bench_cache)
        assert "[주의]" not in buf2.getvalue()
    print("[OK] 가격캐시 fail-closed(연휴 오발동 없음) · 벤치마크 부분겹침 경고")


def _relevance_row(weights: dict, metrics: dict, alert: float = 0.35):
    """theme_relevance_history 를 한 정기변경 이벤트로 호출해 1행을 돌려준다.
    weights: {ticker: w}, metrics: {ticker: (group, exposure, mem_ratio)}."""
    d = pd.Timestamp("2024-06-17")
    w = pd.Series(weights, dtype=float)
    snap = pd.DataFrame(
        [{"ticker": t, "group": g, "exposure": ex, "mem_ratio": mr}
         for t, (g, ex, mr) in metrics.items()])
    events = [{"effective_date": d, "reason": "regular", "target_weights": w}]
    out = theme_relevance_history(events, {d: snap}, alert=alert)
    assert len(out) == 1
    return out.iloc[0]


def test_theme_relevance_is_anchor_weight_invariant():
    """비앵커 구성이 같으면 앵커 비중이 달라도 적합도 점수는 같아야 한다.

    과거 구현은 앵커를 metric 0 으로 전체 비중에 곱해, 앵커 비중이 클수록
    (=테마가 순수할수록) 점수가 낮아지는 역설이 있었다. 정규화 수정이 이를
    없앴는지 못박는다. 핵심:위성 비중비를 2:1 로 고정하면 두 시나리오의
    비앵커 가중평균은 동일해야 한다.
    """
    metrics = {"A0": ("anchor", np.nan, np.nan),
               "C0": ("core", 0.50, np.nan),
               "S0": ("satellite", np.nan, 0.80)}
    # 앵커 40% (비앵커 60%: 핵심 40 · 위성 20)
    lo = _relevance_row({"A0": 0.40, "C0": 0.40, "S0": 0.20}, metrics)
    # 앵커 70% (비앵커 30%: 핵심 20 · 위성 10) - 핵심:위성 = 2:1 동일
    hi = _relevance_row({"A0": 0.70, "C0": 0.20, "S0": 0.10}, metrics)
    expected = (0.50 * 2 + 0.80 * 1) / 3          # 비앵커 가중평균 = 0.60
    assert abs(lo["score"] - expected) < 1e-12
    assert abs(hi["score"] - expected) < 1e-12, "앵커 비중이 점수를 바꿈(편향 잔존)"
    assert abs(lo["score"] - hi["score"]) < 1e-12
    print("[OK] 테마 적합도가 앵커 비중에 불변 - 정규화 수정 검증")


def test_theme_relevance_alert_and_degenerate():
    """경보선 판정과 비앵커 부재(퇴화) 처리."""
    m = {"A0": ("anchor", np.nan, np.nan),
         "C1": ("core", 0.30, np.nan), "C2": ("core", 0.30, np.nan)}
    low = _relevance_row({"A0": 0.50, "C1": 0.25, "C2": 0.25}, m)
    assert abs(low["score"] - 0.30) < 1e-12 and bool(low["below_alert"]) is True
    m_hi = {"A0": ("anchor", np.nan, np.nan), "C1": ("core", 0.90, np.nan)}
    high = _relevance_row({"A0": 0.50, "C1": 0.50}, m_hi)
    assert abs(high["score"] - 0.90) < 1e-12 and bool(high["below_alert"]) is False
    # 비앵커가 하나도 없으면 적합도는 정의 불가(NaN) → 경보 아님
    deg = _relevance_row({"A0": 1.0}, {"A0": ("anchor", np.nan, np.nan)})
    assert np.isnan(deg["score"]) and bool(deg["below_alert"]) is False
    print("[OK] 테마 적합도 경보선·비앵커 부재 퇴화 처리")


def test_theme_relevance_launch_calibration():
    """2026-07-23 확정 비중·판정값이 문서의 출범 점수 44.05%를 재현한다."""
    metrics = {
        "005930": ("anchor", np.nan, np.nan),
        "000660": ("anchor", np.nan, np.nan),
        "042700": ("core", 0.60, np.nan),
        "089030": ("core", 0.30, np.nan),
        "003160": ("core", 0.35, np.nan),
        "348210": ("core", 0.50, np.nan),
        "112290": ("satellite", np.nan, 0.75),
    }
    row = _relevance_row(
        {"005930": 0.2157, "000660": 0.1843, "042700": 0.1800,
         "089030": 0.1800, "003160": 0.1281, "348210": 0.1058,
         "112290": 0.0061},
        metrics)
    assert abs(row["score"] - 0.4405166666666667) < 1e-12
    assert bool(row["below_alert"]) is False
    print("[OK] 비앵커 테마 적합도 출범 점수 44.05% 재현")


def test_capacity_adv_duplicate_ticker_fails_closed():
    """ADV CSV 에 중복 ticker 가 있으면 pandas 스택트레이스 대신 클린 실패."""
    with tempfile.TemporaryDirectory() as d:
        adv = os.path.join(d, "adv.csv")
        pd.DataFrame({"ticker": ["000001", "000001"],
                      "adv60_krw": [15e8, 20e8]}).to_csv(adv, index=False)
        try:
            capacity_v2.load_adv(adv)
            raise AssertionError("중복 ticker ADV 가 통과함")
        except SystemExit as exc:
            assert "중복 ticker" in str(exc)
    print("[OK] capacity ADV 중복 ticker fail-closed")


if __name__ == "__main__":
    test_calendar_matches_methodology()
    test_holiday_shift_is_deterministic()
    test_driver_end_to_end()
    test_coverage_report_flags_gaps()
    test_snapshot_loader_fails_closed()
    test_correlation_does_not_fill_missing_returns()
    test_capacity_real_delta_w_path_is_wired()
    test_capacity_adv_duplicate_ticker_fails_closed()
    test_benchmark_config_fixes_code_and_matches_series()
    test_benchmark_resolver_aliases_and_distinct_series_names()
    test_benchmark_choice_is_deterministic_and_prefers_pr()
    test_both_mode_fetches_distinct_pr_tr_benchmarks_and_caches()
    test_price_cache_staleness_fail_closed_benchmark_warns()
    test_theme_relevance_is_anchor_weight_invariant()
    test_theme_relevance_alert_and_degenerate()
    test_theme_relevance_launch_calibration()
    print("\n16/16 드라이버 스모크 통과 - pykrx 없이 전 배선 검증 완료")
