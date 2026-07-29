# 판정 원장 작성 가이드

## 목적

`data/verdict_ledger_scaffold.csv`는 33개 후보의 2019~2025 사업연도 골격
223행입니다. 이 파일은 백테스트 결과가 아니라, 당시 공개된 자료만으로 편입
판정을 재생하기 위한 PIT(point-in-time) 입력 골격입니다.

작성 편의를 위한 Excel 파일은 `data/판정원장_작성시트_223행.xlsx`입니다.
`판정원장_작성시트`와 `작성가이드` 시트를 제공하며, 엔진에 입력할 때는
CSV(`data/verdict_ledger_scaffold.csv`)를 기준 형식으로 사용합니다.

현재 골격은 **확정 원장이 아닙니다.**

- 223행 모두 HBM 판정 5개 필드가 비어 있습니다.
- `disclosed_at`은 실제 DART 접수일이 아니라 익년 3월 31일 가정값입니다.
- `source`에는 모두 `TODO`가 남아 있습니다.
- `free_float`이 없는 행이 8개 있습니다:
  `112290/2021`, `322310/2019`, `348210/2019`, `353200/2019`,
  `357780/2019`, `394280/2021`, `403870/2021`, `425040/2021`.
- `_핸도버2026bucket`은 2026년 결과를 확인하기 위한 헬퍼입니다.
  과거 연도 판정값을 채우는 근거로 사용하면 look-ahead가 발생합니다.

## 2026-07-23 확정 단면

파트2의 확정 PDF 2종은 다음 파일로 별도 보존합니다.

- `evidence/judgment_snapshot_20260723.csv`: 후보 33종목 판정값
- `evidence/judgment_snapshot_20260723.meta.json`: 범위·해시·계보
- `evidence/source_docs/hbm_judgment_values_33_20260723.pdf`
- `evidence/source_docs/hbm_judgment_result_20260723.pdf`
- `data/constituents/constituents_handoff_20260723.csv`: 7종목 유동시총·비중

이 자료는 `2026-07-23`부터 유효한 **FINAL 횡단면**이다. 선정 및 비중
교차검증에는 사용할 수 있지만, 사업연도별 공개일을 가진 역사적 원장은
아니므로 `data/verdict_ledger.csv`에 소급 병합하지 않는다.

```powershell
python analysis\verify_judgment_snapshot.py --report docs\JUDGMENT_SNAPSHOT_20260723_REPORT.md
```

현재 검증 결과는 33종목에서 7종목(앵커 2·핵심 4·위성 1)을 전량
재현하고, 인계 유동시총 기반 비중을 최대 0.001302%p 오차로 재현한다.

## 역할 분담

| 항목 | 확정 책임 |
|---|---|
| 실제 DART 접수일·접수 URL·감사의견·당시 관리종목·유동비율 | 데이터 담당 |
| HBM 양산, HBM 매출 노출도, 메모리향 비중, HBM 공정 확인 | 파트2 판정 담당 |
| `committee_ok` | 지수위원회 |
| 원장 소비, 히스테리시스·수시변경, 백테스트 | 파트3(소연) |

파트3는 판정값을 추정하거나 대신 확정하지 않습니다. 비어 있는 수치값은
`UNKNOWN`으로 유지되며 임계값을 통과하지 못한 것으로 보수 처리됩니다.

## 확정 원장 필수 필드

| 필드 | 규칙 |
|---|---|
| `ticker` | 6자리 문자열 |
| `fiscal_year` | 근거 자료의 사업연도 |
| `disclosed_at` | 실제 공개일, `YYYY-MM-DD` |
| `source` | DART 접수 URL 등 재확인 가능한 출처 |
| `reviewer` | 판정자 |
| `judgment_status` | 확정 행은 `FINAL` |
| `audit_opinion` | 해당 시점 감사의견 |
| `admin_issue` | 해당 시점 관리종목 여부 |
| `hbm_massproduction` | HBM 실제 양산·공급 여부 |
| `hbm_exposure` | HBM 밸류체인 매출 / 전사 매출, 0~1 |
| `mem_ratio` | 메모리 반도체향 매출 / 전사 매출, 0~1 |
| `process_confirmed` | HBM 고유공정 귀속 문서 확인 |
| `committee_ok` | 위원회 확인 |
| `free_float` | 해당 시점 유동비율, 0~1 |

## 작성 순서

1. 사업연도별 근거 카드를 생성합니다. 현재 보고서 자동 선택을 피하려면
   `--fiscal-year`를 반드시 지정합니다.

```powershell
python hbm_evidence.py --input universe_code.csv --fiscal-year 2025
```

`hbm_evidence.py`의 사업연도 조회는 `final=False`로 원본·정정공시를 모두
조회한 뒤 최초 접수본을 선택해야 합니다. 이 인자가 없는 구판 수집기는
룩어헤드를 재발시킬 수 있으므로 사용하지 않습니다.

2. 생성된 `evidence/판정입력_템플릿.csv`에 판정값·판정자·위원회 확인을
   입력합니다. `committee_ok`는 위원회 확인 전에 미리 `True`로 두지 않습니다.

3. 검토 중에는 잠정 원장으로만 생성합니다.

```powershell
python build_ledger_from_evidence.py --scaffold data\verdict_ledger_scaffold.csv --evidence evidence\judgment_input_draft.csv --fiscal-year 2025 --allow-provisional --out data\verdict_ledger_provisional.csv
```

4. 실제 공개일·출처·판정자와 `FINAL` 상태가 모두 채워진 뒤 확정 원장을
   생성합니다. 기본 모드는 fail-closed이므로 하나라도 빠지면 중단됩니다.

```powershell
python build_ledger_from_evidence.py --scaffold data\verdict_ledger_scaffold.csv --evidence evidence\judgment_input_final.csv --out data\verdict_ledger.csv
```

5. 확정 원장만 정식 PIT 스냅샷으로 변환합니다.

```powershell
python analysis\build_pit_snapshots.py --ledger data\verdict_ledger.csv --out data\snapshots
python analysis\run_backtest.py --snapshots data\snapshots --policy all --require-lineage
python analysis\verify_claims.py --factsheet
```

탐색 목적으로 DRAFT 원장을 스냅샷에 넣으려면
`build_pit_snapshots.py --allow-provisional`을 명시해야 합니다. 그 결과는 성과
발표나 확정 지수 산출에 사용할 수 없습니다.

## 금지 사항

- 2026년 확정 bucket을 과거 사업연도 판정에 복사
- 2026-07-23 확정 노출도·메모리향 값을 이전 심사일에 소급
- 법정기한 가정일을 실제 DART 접수일처럼 표시
- 공란 Boolean을 `False`로 자동 변환
- 출처 없는 노출도·메모리향 비중을 임의 추정
- 위원회 확인 전 `committee_ok=True` 입력
- DRAFT 원장으로 산출한 CAGR·MDD·회전율을 실측 성과로 발표
