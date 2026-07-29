# 백테스트 완성 실행 순서서 (Runbook)

작성: 2026-07-29 · 파트3 기안 · 대상: 전 구간(2020-06-15~ASOF) 실데이터 백테스트
전제: FINAL 원장 215행(`verdict_ledger.csv`, sha256 `E89BDCE9…247F`) 완성,
엔진·테스트 92/92, PIT 템플릿 13시점 커버 확인 완료.
실행 환경: **로컬 PC** (pykrx 네트워크 조회 필요 — 클라우드 조사 환경에서는
패키지 설치가 차단되어 실행 불가 확인).

---

## 0. 무엇이 남았나 (한 눈)

| 구분 | 상태 |
|---|---|
| 판정원장 (엔진 입력) | ✅ FINAL 215행, strict 13/13 |
| PIT 스냅샷 생성기·백테스트 러너·검증 체계 | ✅ 배선 완료 (실행만 남음) |
| 실데이터 스냅샷 13회분 | ❌ 미생성 (pykrx) |
| 가격 캐시 `out/px.csv` | ❌ 미수집 (최초 실행 시 자동 수집·캐시) |
| INDEX_ASOF (백테스트 종료일) | ❌ 위원회 미확정 — **선행 결정 D1** |
| 벤치마크 CONFIRMED | ❌ PROVISIONAL — **선행 결정 D2** |
| 수시변경 이벤트 조사 (상폐·합병·거래정지) | ❌ 미조사 — **선행 결정 D3** |
| 배당 CSV (TR용) | ❌ 선택 항목 (PR 헤드라인은 불필요) |

## 1. 선행 결정 3건 (실행 전 위원회/팀 확정)

**D1. `INDEX_ASOF` 확정.** 백테스트 종료일. 재현 기준일을 한 곳에서
통제하는 값이며(`index_calendar`), 최초 시행일(2020-06-15)보다 이르면
러너가 중단한다. 권고: 최근 완결 월말 또는 2026-07-23(인계 기준일)과
정합한 날짜. 회의록에 값·사유 기록.

**D2. 벤치마크.** `python analysis\resolve_benchmark_code.py` (INDEX_ASOF
설정 후) → PR/TR 코드·정확 표기명·최초 관측일을 회의록에 기록 →
`data/benchmark.yaml`에 코드·`effective_date`·`resolved_by` 기입 →
`status: CONFIRMED`. **1차 실행은 `--no-benchmark`로 병행 가능** —
벤치마크 확정을 기다리느라 본 실행을 막을 필요는 없다(추적오차·상관만
후속 재실행으로 붙는다).

**D3. 수시변경 이벤트 조사.** 2020-06-15~ASOF 구간에서 후보 33종목의
상장폐지·구성종목 간 합병·거래정지·관리종목 지정 이력을 조사한다.
`admin_history_normalized.csv`(33종목 전부 N)는 **현재 시점 무지정 확인**
이므로, 기간 중 지정→해제 이력은 KIND에서 별도 확인해야 한다. 결과가
0건이어도 "조사했고 0건"을 기록하고(조용한 생략 금지), 있으면:

- 편출: `exclusions.csv` — `공지일, ticker, 사유` (엔진이 공지일 D+2 집행)
- 거래정지: `suspensions.csv` — `ticker, start_date, end_date`

## 2. 환경 준비 (로컬 PowerShell, 레포 **내부** 루트에서)

```powershell
# 0) 먼저 현재 미커밋 변경분을 커밋한다 - 스냅샷 --code-commit과
#    실행 매니페스트가 가리킬 코드 상태를 고정하기 위함
git add -A ; git commit -m "docs+data: FINAL ledger v2, governance draft, discovery contract"
git rev-parse HEAD   # <COMMIT>으로 아래에서 사용

pip install -r requirements.txt
python -c "import pykrx; print('pykrx ok')"
mkdir out -Force
pip freeze > out\env_freeze_20260729.txt   # requirements 주석의 '재현 릴리스 고정' 약속 이행
$env:INDEX_ASOF = "<D1에서 확정한 YYYY-MM-DD>"
```

## 3. 실행 시퀀스

```powershell
# S1. 필요 시점 재확인 (네트워크 불필요, 13시점 표)
python analysis\build_pit_snapshots.py --ledger data\verdict_ledger.csv --template-only

# S2. PIT 스냅샷 13회분 생성 (pykrx: 시총·ADV60·상장경과 / 원장: 판정·유동비율)
python analysis\build_pit_snapshots.py --ledger data\verdict_ledger.csv --out data\snapshots --code-commit <COMMIT>

# S3. 커버리지 진단 (본 실행 전 필수 게이트)
python analysis\run_backtest.py --snapshots data\snapshots --prices-cache out\px.csv --coverage-only --out out\backtest

# S4. 본 실행 - PR 헤드라인, 버퍼 4안 동일 조건, 계보 강제
python analysis\run_backtest.py --snapshots data\snapshots --prices-cache out\px.csv `
  --policy all --require-lineage --mode pr --no-benchmark --out out\backtest
#   (D2 완료 후: --no-benchmark 제거하고 재실행 - benchmark_inference.csv 추가됨)
#   (D3 결과가 있으면: --exclusions ... --suspensions ... 추가)

# S5. (선택) TR 병기 - 배당 CSV(ex_date,ticker,dps · 보통배당만) 준비 후
python analysis\run_backtest.py ... --mode both --dividends data\dividends.csv
```

게이트 (각 단계 통과 조건):

- **S2**: `data/snapshots/snapshot_YYYYMMDD.csv` 13개 생성, strict 로더
  통과(`--require-lineage`가 S3에서 검증). 시행일이 캘린더 조문과 다르면
  러너가 경고한다 — 의도가 아니면 파일명 정정.
- **S3**: `coverage_report.csv`의 결측을 전건 분류한다 — **상장 전**이면
  해당 시점 스냅샷 제외(정상), **수집 실패**면 재수집. 엔진은 결측을 임의
  보정하지 않으므로 미분류 결측이 남으면 본 실행이 중단된다(설계대로).
  이때 **시점별 편입 종목 수도 확인**한다 — 2020~2022 희소 구간에서 5 미만
  구간이 나오면 §5-리스크의 팀 결정이 필요하다.
- **S4**: 성과 요약·회전율 분해·연도별 회전율·비용 민감도·테마 적합도가
  출력되고 `out/backtest/`에 산출물이 쌓인다.

## 4. 산출물 → 등록 → 동결 (백테스트의 "완성" 정의)

실행 산출물 (`out/backtest/`): `index_level.csv`, `change_history.csv`,
`event_log.csv`, `theme_relevance.csv`, `policy_comparison.csv`,
`coverage_report.csv`, (벤치마크 확정 후) `benchmark_inference.csv`,
(TR 실행 시) `index_level_pr_tr.csv`. 가격 캐시는 `out/px.csv`로 저장돼
재실행 시 재사용된다.

완성 처리 4단계:

1. **생존편향 절차**: `run_backtest.listing_check`로 시점별 상장 명단
   대조 + "그 시점엔 자격이었으나 지금 없는 종목"의 사람 조사. 결과를
   "대조했고 N종목 해당, 편향 방향 상향 가능성"으로 문장화(수학적 보장
   표현 금지).
2. **클레임 등록**: 성과(수익률·변동성·MDD)·회전율·적합도·구성 이력
   수치를 `verify_claims.py`에 재현 함수로 추가 → 인용 금지 목록에서
   해당 항목 해제 → `--factsheet-out`으로 FACTSHEET 재생성. **등록 전에는
   어떤 수치도 문서·화면에 싣지 않는다** (기존 규칙 그대로).
3. **대시보드 연결**: `streamlit run app.py` — ④~⑥ 화면이 `out/backtest`
   산출물로 활성화된다. 표시 수치는
   `analysis/audit_dashboard_numbers.py`로 검산 후 공개.
4. **실행 기록 동결**: 백테스트 실행 매니페스트를 남긴다 —
   `INDEX_ASOF`, `<COMMIT>`, `env_freeze` 해시, 입력 해시(원장
   `E89BDCE9…`, 스냅샷 13개, px 캐시), 산출물 해시, 실행 일시.
   `final_ledger_manifest.json` 패턴 준용. 이 매니페스트가 있어야
   "같은 입력→같은 지수"를 제3자가 검증할 수 있다.

## 5. 리스크와 대응

- **pykrx 조회 실패·중단**: 가격은 `out/px.csv` 캐시로 이어달리기 가능.
  스냅샷 생성 중 실패 시 해당 시점만 재실행.
- **2020~2022 희소 구간 — 구성 5 미만 가능성**: 방법론은 "잔여 종목으로
  산출 지속 + 긴급 재심사 개시·공표"다. 역사적 백테스트에서 긴급심사를
  소급 재현할지(`--emergency-snapshots`), "충족 종목 없음 — 하한 미달
  지속"으로 기록만 할지는 **커버리지 결과 확인 후 팀 결정**(원장 밖 정보로
  소급 심사하면 look-ahead이므로, 원장 기반 재현이 불가하면 후자가 원칙에
  부합).
- **TR 이중반영**: 배당 CSV에는 보통배당만 — 특별배당·자본환급은 제수
  조정 경로에서 반영되므로 넣으면 이중반영이다(러너 주석 명시).
- **유동비율 원천**: DART 기준 유지(한계점 고지 유지). KRX 공식 확보 시
  숫자 교체 후 재실행.
- **완성 후 열리는 후속** (이 순서서 범위 밖): 27/67 사후검토
  (`policy_comparison.csv` 2회차 축적 후 7.3 절차), 리밸 주기 민감도
  (`frequency_sensitivity.py` 신설 — 계약된 P2), capacity 실측 전환,
  상용상품 집중도 대비표(P5, 재현 함수 등록 포함).

---

*수치는 방법론 검증용이며 특정 종목 투자 권유가 아니다.*
