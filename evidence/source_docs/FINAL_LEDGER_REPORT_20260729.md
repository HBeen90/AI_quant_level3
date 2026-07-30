# HBM 테마지수 FINAL 판정원장 완료 보고

## 1. 완료 상태

- 입력계약 상태: `FINAL_INPUT_CONTRACT`
- 계보 등급: `L09 PARTIAL`
- 엔진 입력 행: 215행
- 유니버스: 33종목
- 사업연도: FY2019~FY2025
- 별도 보존 `NO_DATA`: 8행
- 중복 `(ticker, fiscal_year)`: 0건
- 수치 `UNKNOWN`: 0건
- strict 검증: 13/13 PASS

`NO_DATA` 8행은 상장 전·사업보고서 미공시 조합으로, 보간하거나 미래 값을 복사하지
않고 엔진 입력에서 제외했다. 감사용 Excel과 `no_data_rows.csv`에는 그대로 보존했다.

## 2. 결합한 원천

1. `판정원장_작성완료_노민수.xlsx`
   - 파트2 판정값과 DART 접수 URL
   - 원본 reviewer `노민수` 유지
2. `관리종목이력_확인.csv`
   - 33종목 관리종목 이력 확인
   - 전 종목 `N`, 확인자·확인일·KIND 근거 URL 존재
3. `data/verdict_ledger_scaffold.csv`
   - 유니버스·섹터·유동비율 기준 골격

이 결과의 `FINAL`은 제공 자료를 결합해 코드의 strict 입력계약을 충족했다는 뜻이다.
파트3가 파트2 판정값을 새로 판단했다는 뜻은 아니며, DART 원문 215건을 제3자가
독립 재수집했다는 주장도 하지 않는다.

## 3. 자동 교정

DART `rcpNo`의 접수일과 `disclosed_at`이 하루 어긋난 2건을 접수번호 기준으로 정렬했다.

| 종목/FY | 기존 | 교정 | 근거 |
|---|---:|---:|---|
| 014680/FY2020 | 2021-03-17 | 2021-03-16 | `rcpNo=20210316...` |
| 067310/FY2022 | 2023-03-28 | 2023-03-27 | `rcpNo=20230327...` |

교정은 공개 시점을 뒤로 당기는 임의 조정이 아니라 DART 접수번호에 포함된 실제
접수일로 맞춘 것이다.

## 4. strict 검증 결과

- canonical `build_ledger_from_evidence.py`: `FINAL 215행`
- canonical `analysis.build_pit_snapshots.load_ledger`: PASS
- 모든 `judgment_status`: `FINAL`
- DART 원천 URL·감사의견·판정자: 215/215 완비
- DART 접수번호 날짜와 공개일: 215/215 일치
- 관리종목 확인 종목: 33/33
- Boolean 필드 엄격 파싱: PASS
- 노출도·메모리향·유동비율 범위 0~1: PASS
- Excel 수식 오류: 0건
- 감사용 Excel의 Checks 시트: 13/13 PASS

## 5. 산출물

- `verdict_ledger.csv`: 백테스트/PIT 스냅샷용 최종 기계 입력
- `judgment_input_final.csv`: strict 빌더 재현용 최종 판정 입력
- `HBM_verdict_ledger_FINAL_20260729.xlsx`: 사람이 검토하는 감사용 원장
- `no_data_rows.csv`: 제외된 8개 조합
- `admin_history_normalized.csv`: 관리종목 확인 33종목
- `final_ledger_manifest.json`: 검증 결과·원천 해시·교정 이력
- `verdict_ledger_final_decision_record_20260729.md`: 확인 요청 5항 종결과 남은 계보 범위
- `source_ledger_final_20260729.xlsx`: 파트2 제출 원본의 무수정 보관본
- `SHA256SUMS.txt`: 배포 파일 무결성 확인

## 6. 다음 실행

레포 루트에서 최종 CSV를 `data/verdict_ledger.csv`로 배치한 뒤 실행한다.

```powershell
python .\analysis\build_pit_snapshots.py --ledger .\data\verdict_ledger.csv --out .\data\snapshots
python .\analysis\run_backtest.py --snapshots .\data\snapshots --prices-cache .\out\px.csv --policy all --require-lineage --out .\out\backtest
python -m streamlit run .\app.py
```

재현 기준일은 실행 전에 `INDEX_ASOF`로 고정한다.

```powershell
$env:INDEX_ASOF = "위원회가 승인한 백테스트 종료일"
```

## 7. 남는 범위

독립 DART 215건 전수 대조는 `METADATA_VERIFIED` 승격의 필수 조건이며,
현재 `FINAL_INPUT_CONTRACT`의 선행조건은 아니다. 재수집 결과를 동결·해시한 뒤
215행 대조표를 추가하기 전까지 L09는 `PARTIAL`로 유지한다.
