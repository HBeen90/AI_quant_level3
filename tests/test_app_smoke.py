# -*- coding: utf-8 -*-
"""
app.py 헤드리스 스모크 - streamlit 없이 대시보드 로직을 전부 실행한다.

왜 필요한가: 대시보드 버그의 대부분은 그리기가 아니라 **데이터 정형**에서
난다(컬럼명 불일치, 인덱스 파싱, 없는 함수 호출). 그건 브라우저를 안 띄워도
잡을 수 있고, 잡아야 한다 - 발표 자리에서 처음 발견하면 늦다.

streamlit·altair를 허용적 스텁으로 대체하고 app.py 를 페이지별로 실행해,
예외 없이 끝나는지와 화면에 올라간 표의 모양이 맞는지 확인한다.
스텁이므로 '보기 좋은가'는 검증하지 않는다 - 그건 눈으로 봐야 한다.
"""
import os
import runpy
import sys
import types

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)


# ── 허용적 스텁 ────────────────────────────────────────────────────────────
class _Any:
    """무엇을 호출해도 자기 자신을 돌려주는 체이닝 더미(altair 대역)."""

    def __init__(self, *a, **k):
        pass

    def __getattr__(self, _):
        return _Any()

    def __call__(self, *a, **k):
        return _Any()

    def __add__(self, other):
        return _Any()

    def __or__(self, other):
        return _Any()


class _Stop(Exception):
    """st.stop() 대역."""


class _Recorder:
    """streamlit 대역. 화면에 올라간 것을 기록해 검증에 쓴다."""

    def __init__(self, page: str, outdir: str):
        self.page, self.outdir = page, outdir
        self.frames, self.metrics, self.charts = [], [], 0
        self.sidebar = self
        self.column_config = _Any()

    # 표시 계열 - 전부 무시하되 표/지표만 기록
    def __getattr__(self, name):
        def _noop(*a, **k):
            return None
        return _noop

    def dataframe(self, data=None, *a, **k):
        self.frames.append(data)

    def metric(self, label=None, value=None, *a, **k):
        self.metrics.append((label, value))

    def altair_chart(self, *a, **k):
        self.charts += 1

    def columns(self, spec, *a, **k):
        n = spec if isinstance(spec, int) else len(spec)
        return [self for _ in range(n)]

    def radio(self, label, options, *a, **k):
        return self.page

    def text_input(self, label, value="", *a, **k):
        return self.outdir

    def slider(self, label, *a, **k):
        return a[2] if len(a) > 2 else a[0]

    def number_input(self, label, *a, **k):
        return a[2] if len(a) > 2 else a[0]

    def spinner(self, *a, **k):
        class _Ctx:
            def __enter__(self_): return None
            def __exit__(self_, *e): return False
        return _Ctx()

    def __enter__(self):            # `with col:` 블록 지원
        return self

    def __exit__(self, *exc):
        return False

    def stop(self):
        raise _Stop()

    def set_page_config(self, *a, **k):
        return None


class _AltModule:
    """altair 대역 모듈. 어떤 속성을 찾아도 체이닝 더미를 준다."""

    __name__ = "altair"

    def __getattr__(self, _):
        return _Any()


def _run_page(page: str, outdir: str) -> _Recorder:
    # 모듈 객체 대신 인스턴스를 sys.modules 에 직접 넣는다 - __getattr__ 이
    # 살아 있어야 st.<무엇이든> 이 전부 받아진다(types.ModuleType 은 안 됨).
    rec = _Recorder(page, outdir)
    saved = {k: sys.modules.get(k) for k in ("streamlit", "altair")}
    sys.modules["streamlit"] = rec
    sys.modules["altair"] = _AltModule()
    try:
        runpy.run_path(os.path.join(HERE, "app.py"), run_name="__main__")
    except _Stop:
        pass                                   # 데이터 없음 -> 정상 조기 종료
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return rec


# ── 테스트 ────────────────────────────────────────────────────────────────
def test_pages_run_without_data():
    """데이터 없이 켜져야 하는 화면(①~③)이 실제로 켜지는가."""
    missing = os.path.join(HERE, "__no_such_out__")
    for page in ("① 파이프라인 상태", "② 용량 역산기", "③ PIT vs FROZEN"):
        rec = _run_page(page, missing)
        assert rec.frames, f"{page}: 표가 하나도 안 올라감"
        assert rec.charts or page.startswith("①"), f"{page}: 차트 없음"
    print("[OK] ①~③ 데이터 없이 동작 (용량 역산기·PIT 데모 포함)")


def test_capacity_page_numbers():
    """② 용량 역산기가 실제로 맞는 숫자를 내는가 (기본값 AUM 3000·5%·10%)."""
    rec = _run_page("② 용량 역산기", os.path.join(HERE, "__no_such_out__"))
    frames = [f.data if hasattr(f, "data") else f for f in rec.frames]
    ev = next(f for f in frames
              if isinstance(f, pd.DataFrame) and "소요일수" in f.columns)
    row = ev[ev["ADV60(억)"] == 15.0].iloc[0]
    # 0.05 * 3000 / (15 * 0.10) = 100 거래일
    assert abs(row["소요일수"] - 100.0) < 1e-9, f"역산 오류: {row['소요일수']}"
    row45 = ev[ev["ADV60(억)"] == 45.0].iloc[0]
    assert abs(row45["소요일수"] - 33.3333333) < 1e-4
    print(f"[OK] ② 역산 검증: ADV 15억 -> {row['소요일수']:.0f}일, "
          f"45억 -> {row45['소요일수']:.1f}일 (AUM 3000억·상한 5%·참여율 10%)")


def test_pit_page_shows_zero_churn_for_frozen():
    """③ 이 FROZEN=0회를 실제로 보여주는가 - 이 화면의 존재 이유."""
    rec = _run_page("③ PIT vs FROZEN", os.path.join(HERE, "__no_such_out__"))
    res = next(f for f in rec.frames
               if isinstance(f, pd.DataFrame) and "편출입 합" in f.columns)
    fz = res[res["입력"] == "FROZEN"]["편출입 합"]
    pit = res[res["입력"] == "PIT"]["편출입 합"]
    assert (fz == 0).all(), f"FROZEN 편출입이 0이 아님: {fz.tolist()}"
    assert pit.min() > 0 and pit.nunique() > 1, "PIT가 정책별로 안 갈림"
    assert pit.iloc[-1] < pit.iloc[0], "넓은 버퍼가 회전을 못 줄임"
    print(f"[OK] ③ FROZEN 0회 vs PIT {int(pit.min())}~{int(pit.max())}회")


def _make_out(root: str) -> str:
    """④~⑥용 산출물 생성 - run_backtest 를 --mode both 로 실제 실행."""
    import subprocess
    from tests.test_run_backtest_smoke import _make_fixture
    snap_dir, cache, _ = _make_fixture(root)
    px = pd.read_csv(cache, index_col=0, parse_dates=True)
    px.columns = [str(c).zfill(6) for c in px.columns]
    rows = [{"ex_date": d.date(), "ticker": t,
             "dps": round(float(px.loc[d, t]) * 0.015)}
            for d in px.index[[100, 400, 800, 1200]] for t in px.columns[:6]]
    div = os.path.join(root, "div.csv")
    pd.DataFrame(rows).to_csv(div, index=False)
    out = os.path.join(root, "out")
    p = subprocess.run(
        [sys.executable, os.path.join(HERE, "analysis", "run_backtest.py"),
         "--snapshots", snap_dir, "--prices-cache", cache, "--no-benchmark",
         "--policy", "all", "--mode", "both", "--dividends", div, "--out", out],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", cwd=HERE,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert p.returncode == 0, f"산출물 생성 실패:\n{p.stdout}\n{p.stderr}"
    return out


def test_pages_with_data():
    """④~⑥ 이 실제 산출물을 읽어 표·차트를 만드는가."""
    import tempfile
    with tempfile.TemporaryDirectory() as root:
        out = _make_out(root)
        for page in ("④ 백테스트 결과", "⑤ 버퍼 정책 비교", "⑥ PR vs TR"):
            rec = _run_page(page, out)
            assert rec.charts > 0, f"{page}: 차트가 안 그려짐"
            assert rec.metrics, f"{page}: 지표가 없음"

        rec4 = _run_page("④ 백테스트 결과", out)
        labels = dict(rec4.metrics)
        assert "MDD" in labels and "CAGR" in labels
        rec6 = _run_page("⑥ PR vs TR", out)
        gap = dict(rec6.metrics)["연환산 배당 기여도"]
        assert gap.endswith("%") and float(gap[:-1]) > 0, \
            f"배당 기여도가 양수가 아님: {gap}"
        print(f"[OK] ④~⑥ 산출물 연동 (MDD {labels['MDD']}, "
              f"CAGR {labels['CAGR']}, 배당 기여도 {gap})")


def test_policy_page_warns_without_data():
    """⑤ 가 데이터 없이 '수치 쓰지 말라' 경고를 띄우고 멈추는가."""
    rec = _run_page("⑤ 버퍼 정책 비교", os.path.join(HERE, "__no_such_out__"))
    assert rec.charts == 0, "데이터 없이 차트를 그림"
    print("[OK] ⑤ 데이터 없을 때 조기 종료 (합성 수치 오용 방지 경고)")


if __name__ == "__main__":
    test_pages_run_without_data()
    test_capacity_page_numbers()
    test_pit_page_shows_zero_churn_for_frozen()
    test_policy_page_warns_without_data()
    test_pages_with_data()
    print("\n5/5 대시보드 헤드리스 스모크 통과")
