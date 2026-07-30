# 대시보드 실행

```powershell
pip install streamlit altair
streamlit run app.py
```

브라우저가 열리면 사이드바에서 화면을 고릅니다.

## 데이터 없이 지금 바로 되는 화면

| 화면 | 내용 |
|---|---|
| **① 파이프라인 상태** | 모듈별 진행·미결 항목·병목 한눈에 |
| **② 용량 역산기** | AUM·참여율·허용일수 슬라이더 → 비중 상한 역산. "위성 5% 상한이 며칠인가"를 즉석에서 답합니다 |
| **③ PIT vs FROZEN** | 13회 판정을 즉석 재생. FROZEN 편출입 0회 vs PIT 30~61회 |

## 백테스트 실행 후 켜지는 화면

```powershell
python analysis/run_backtest.py --snapshots data/snapshots --prices-cache out/px.csv --policy all
# 배당까지 있으면
python analysis/run_backtest.py --snapshots data/snapshots --prices-cache out/px.csv --policy all --mode both --dividends data/dividends.csv
```

| 화면 | 필요한 파일 |
|---|---|
| **④ 백테스트 결과** | `index_level.csv` · `event_log.csv` · `change_history.csv` |
| **⑤ 버퍼 정책 비교** | `policy_comparison.csv` (`--policy all`) |
| **⑥ PR vs TR** | `index_level_pr_tr.csv` (`--mode both --dividends`) |

`INDEX_ASOF`를 설정하면 백테스트 종료일도 그 날짜로 고정되며, 가격 캐시에
그 이후 관측치가 있어도 분석에서 제외됩니다. D+2 집행일이 분석기간 밖인
예약은 `change_history.csv`의 `deferred_beyond_panel` 행으로 확인합니다.

사이드바의 "백테스트 산출 폴더"에 경로를 넣으면 다른 위치도 읽습니다.

## 설계

계산은 전부 기존 모듈(`src/` · `backtest/` · `analysis/`)이 합니다. `app.py`는
위젯과 표시만 담당합니다 — 대시보드가 자체 계산 경로를 가지면 "엔진과
대시보드가 다른 답을 내는" 사고가 시작됩니다.

streamlit 없이도 `python tests/test_app_smoke.py`로 대시보드 로직 전체를
검증할 수 있습니다(스텁 주입). 대시보드 버그는 대부분 그리기가 아니라
데이터 정형에서 나므로, 브라우저를 띄우기 전에 잡습니다.
