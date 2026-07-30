# 판정원장 계보 확정 및 재구축 검증

**기준일**: 2026-07-29
**대상**: 판정원장 223행, 유효 판정 215행, 재구성 `evidence_pit/` 카드 215개

## 0. 결론

1. 현재 제공된 제출물·백업 ZIP·연결 작업공간을 검색했으나, 작업메모가 기술한
   원본 수집 산출물 세트는 확보되지 않았다. 작성자 확인 기록을 함께 고려해
   프로젝트 계보상 **원본 유실·미확보**로 처리한다. 이는 모든 저장장치에 사본이
   존재하지 않았음을 기술적으로 증명한다는 뜻은 아니다.
2. 현재 `evidence_pit/` 215개는 판정원장에서 역생성한 PROVISIONAL 카드이므로
   판정원장과 독립된 감사 증거가 아니다.
3. 구판 수집기에는 `annual_report()` 호출의 `final=False`가 없어서 정정공시가
   존재하는 경우 최초 공시 시점이 정정일로 이동할 수 있다. 구판 두 패키지는
   실행 금지본으로 지정한다.
4. 메타데이터 재수집 215/215 대조가 통과하더라도 L09는 자동 종결하지 않는다.
   DART 원문과 판단 필드의 사람 검토까지 끝나야 FINAL 근거가 완성된다.

## 1. 계보 판단 범위

다음 항목을 현재 제출물·백업 ZIP·연결 작업공간에서 검색했다.

- 당시 실행된 원본 `collect_ledger.py`
- 상단 `EXPOSURE` 판정표가 포함된 원본 `fill_ledger.py`
- 원문 인용이 포함된 진본 `evidence_pit/`
- `ledger/*.csv`
- 당시 후보리스트
- `tests_scan.py`
- 실행 로그와 원본 해시 매니페스트

검색 결과 동일한 원본 세트는 확보되지 않았다. 따라서 계보상 상태는
`MISSING_OR_LOST`로 기록한다. 이후 진본이 발견되면 해시·작성시점·DART 원문을
대조한 뒤 별도 개정 기록으로 편입한다.

## 2. 수집기 버전 판정

| 구분 | `collect_ledger.py` SHA-256 | `annual_report()` | 판정 |
|---|---|---|---|
| 구 `collect_fill_evidence_pit_20260729` 재구성본 | `26E50A5FF7C7D8667F8C3CE863200CB39A065B3DEBE44B49FB1759181C050858` | `final=False` 없음 | 실행 금지 |
| 구 `재구축검증킷_20260729.zip` | 위와 동일 | `final=False` 없음 | 실행 금지 |
| `rebuild_verification_kit_fixed_20260729.zip` | `0572E3ECAD7A0A5C2E1AB2B57A50D6D013D70E4297BB0F2B5E3CE289F3B90A77` | `final=False`, 접수일·접수번호 순 최초본 선택 | 사용 |
| `collect_fill_evidence_pit_fixed_20260729.zip` | 위와 동일 | 위와 동일 | 사용 |

고정본은 OpenDartReader의 `final=True` 기본값을 명시적으로 우회한다.

```python
lst = dart.list(code, start=start, end=end, kind="A", final=False)
r = chosen.sort_values(
    ["rcept_dt", "rcept_no"], ascending=[True, True]
).iloc[0]
```

`fill_ledger.py`의 SHA-256은
`CF40F07739ACD03148F18C45A1850FCCCB9D7673392CC177A5B12983095F376F`이며,
r11 `build_ledger_from_evidence.py`와 동일본으로 확인됐다.

## 3. 검증 상태

### 완료

- 재구성 카드 215개와 원장 유효 215행의 필드 대조 불일치 0
- 재구성 근거표와 제출 XLSX DRAFT 215행의
  `hbm_exposure`, `mem_ratio`, `disclosed_at` 대조 불일치 0
- 한미반도체 FY2021 접수일 `2022-03-10` 반영 확인
- 감사의견 유효 215행 채움 확인
- 고정 수집기의 `final=False` 호출과 최초접수 정렬 확인
- 고정 검증킷의 정상·오염·중복·날짜예외·재실행 거부 경로 검증

### 아직 미완료

- DART API 환경에서 33종목×FY2019~FY2025 전수 재수집
- `rcpNo`, `rcept_dt`, HBM 언급횟수 215/215 독립 대조
- 각 카드의 원문 근거문구 검독
- `hbm_exposure`, `mem_ratio`, `process_confirmed`, `committee_ok`의 위원회 승인
- 관리종목 이력과 두 FY2025 불일치 값의 정본 확정

따라서 L09 상태는 현재 **PARTIAL**이다. 215/215 메타데이터 대조 통과 시
`METADATA_VERIFIED`로 올릴 수 있지만, 판단 필드 검토 전에는 FINAL로 종결하지 않는다.

## 4. 실행 게이트

1. 구 `collect_fill_evidence_pit_20260729.zip`과 구
   `재구축검증킷_20260729.zip`의 수집기는 실행하지 않는다.
2. DART 재수집은 `rebuild_verification_kit_fixed_20260729.zip`으로만 수행한다.
3. strict 대조는 예외 허용 없이 `MATCH 215/215`,
   `MATCH_RCPNO_DATE 0`, 누락·중복 0이어야 한다.
4. `MATCH`는 메타데이터 동등성만 뜻한다. 사람 판단값의 타당성까지 증명하지 않는다.
5. 불일치가 있으면 기존 원장이나 재수집본을 자동 덮어쓰지 않고 건별 검토표를 만든다.

## 5. 작성가이드 산식 정정

이론 격자는 `33종목 × 7개 사업연도 = 231개`다.

- 상장 전이라 행 자체가 없는 조합 8개를 제외하면 실제 원장은 223행이다.
- 그 223행 중 사업보고서가 없어 `NO_DATA`인 행이 별도로 8개다.
- 두 8개 집합은 서로 다르다.

정정본 XLSX의 `작성가이드!B1`에 이 산식을 반영했다.

## 6. 인용 규칙

본 문서를 계보 판단의 실재 근거로 사용한다. 기존
`판정원장_재검증_및_인계결론_20260729.md`는 독립 재검증 결과로 병기하되,
그 문서의 L09 `PARTIAL` 판정을 유지한다.

원본 세트에 대해 "파일로 존재한 적이 없음이 확정"이라고 단정하지 않는다.
문서에는 다음 표현을 사용한다.

> 현재 제공된 제출물·백업 ZIP·연결 작업공간에서 원본 세트를 확보하지 못했으며,
> 작성자 확인 기록에 따라 프로젝트 계보상 원본 유실·미확보로 처리한다.
