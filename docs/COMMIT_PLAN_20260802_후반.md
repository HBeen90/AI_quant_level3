# 2026-08-02 후반 커밋 계획

커밋 수를 늘리지 않고 세 묶음으로 반영한다. 실험 분석과 실패한 대체 산식은 제외한다.

## 1. 시장 데이터 정본과 계보

포함:

- `data/raw/HBM_value_33_2020Q1_2026Q1.csv`
- `data/raw/krx_basic_info_20260802.csv`
- `data/listing_dates.csv`
- `data/market_facts/source_mapping.csv`
- `data/market_facts/facts_*.csv`
- `data/market_facts/raw/krx_all_*.csv`
- `analysis/match_krx_screens.py`
- `analysis/krx_facts_from_screen.py`
- 관련 테스트

커밋 메시지:

```text
feat(part3): freeze official KRX market facts for 13 PIT reviews
```

## 2. FINAL 스냅샷 배선과 감사 게이트

포함:

- `analysis/build_pit_snapshots.py`
- `analysis/verify_claims.py`
- `analysis/make_backtest_manifest.py`
- `data/price_cache_manifest.json`
- `out/px.csv`
- `run_backtest_final.ps1`
- `tests/test_backtest_manifest.py`
- `tests/test_claims.py`
- `tests/test_snapshot_fail_closed.py`
- `.gitignore`
- `evidence/recollect_20260731/`의 compact 4파일

커밋 메시지:

```text
fix(part3): verify KRX raw lineage before FINAL snapshot replay
```

## 3. 작업 기록

2번 커밋 뒤 아래 순서로 FACTSHEET와 회귀 상태를 먼저 동기화한다.

```powershell
python analysis\verify_claims.py --factsheet-out docs\FACTSHEET.md --fast
python -m pytest tests -q
python tests\run_all.py
```

포함:

- `docs/FACTSHEET.md` - 위 명령으로 생성한 파일만 포함, 손편집 금지
- `docs/파트3_공식KRX_FINAL입력_20260802.md`
- `docs/COMMIT_PLAN_20260802_후반.md`

커밋 메시지:

```text
docs(part3): record official-data replay and rejected fallback paths
```

## 제외

- `analysis/facts_from_shares.py`
- `analysis/fetch_prices_kis.py`의 시가총액용 원주가 변경
- `analysis/fetch_market_facts.py`, `analysis/kfsc_client.py`
- 의결 전 cap 대안·timing lag 실험 모듈
- `data/data_2252_20260802.csv`, `data/krx_basic_info_20260802.csv`,
  `data/krx_screens/` 중복 사본 (`data/raw`·`data/market_facts/raw`만 채택)
- `out/px_kis_raw.csv` 등 수집 캐시와 `out/backtest*` 실행 산출물
- `out/backtest_official*`, `data/snapshots_official*` 검증용 사본

위 파일은 FINAL 경로에 필요하지 않거나, 검증 실패·보류 상태이므로 이번 커밋에 넣지 않는다.
