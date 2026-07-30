# 후보발굴 원장 데이터 계약 (candidate_discovery) 【v2】

작성: 2026-07-29 · 상태: 계약 확정안 (팀 승인 대기)
목적: 고정 33종목 후보 유니버스의 선택편향을 해소하는 **전시장 후보발굴
계층**의 입출력·시점 규율·역할 경계를 코드 구현 전에 고정한다.

> v2 변경 (팀 검토 반영): ① `listed_asof`를 기계 검증 가능한
> `listing_date`(날짜) + `listed_asof`(불리언) 2열로 분리 — 12열 구조,
> ② 상태 정의 중복 제거(NEW/PART2_PENDING)와 전이 규칙 고정,
> ③ v1은 종목당 정기보고서 1건만 사용(복수 공시는 long-format 확장으로만),
> ④ 인코딩을 `utf-8-sig · LF`로 계약·템플릿 일치화(템플릿 재생성).

## 1. 계층 분리 원칙

| 계층 | 담당 | 하는 일 | 하지 않는 일 |
|---|---|---|---|
| 후보발굴 (`analysis/candidate_discovery.py`, 신설 예정) | 유니버스·데이터 담당 | 전 상장종목 열거 → 공시 키워드 점수화 → 후보 CSV **동결** | 편입 판정, 노출도 추정 |
| 판정 | 파트2 | 발굴된 후보의 매출 노출도·메모리향 실측 → 판정원장 행 추가 | 후보 누락 책임(발굴 계층 소관) |
| 소비 (파트3) | 김소연 | 동결 스냅샷 소비 · PIT 상태 보고 · 리밸런싱 실행 | 후보 발굴·판정 수정 |

`hbm_evidence.py`는 **확장하지 않는다** — 동 스크립트는 `--input`/`--codes`로
받은 종목만 조사하는 카드 생성기다(main, line 441~). 전시장 열거·점수화는
별도 모듈 소관이며, 종목별 근거 카드 생성은 기존처럼 `hbm_evidence.py`를
후속 단계에서 호출한다.

## 2. 출력 스키마 (심사시점별 동결 CSV · 12열)

파일: `data/candidates/candidate_discovery_<selection_date>.csv`
형식: **utf-8-sig · LF** (해시는 파일 바이트 기준 — `verdict_ledger.csv`
출력 규약과 동일). 동결 후 수정 금지, sha256을 매니페스트에 기록.

```
selection_date, ticker, name, listing_date, listed_asof,
source_rcp_no, disclosed_at, keyword_version,
hbm_hits, process_hits, discovery_reason, review_status
```

| 컬럼 | 정의 |
|---|---|
| `selection_date` | 심사시점(종목 선정일). 3장 일정 조문의 산출값 |
| `ticker` | 6자리 문자열 (zfill, dtype=str 강제) |
| `name` | 종목명 (조회 시점 표기) |
| `listing_date` | 상장일 `YYYY-MM-DD` (KRX 원천) |
| `listed_asof` | `true`/`false` — `selection_date` 현재 상장 여부. **기계 검증**: `listing_date <= selection_date`이고 동 시점 상폐 아님과 일치해야 함 |
| `source_rcp_no` | 점수 산출에 사용한 공시 접수번호 — **§4의 1건 원칙** (최초 접수분, `final=False` — 판정원장과 동일 규칙) |
| `disclosed_at` | 위 공시의 DART `rcept_dt` |
| `keyword_version` | 키워드 사전 버전 (예: `kw_v1`). 사전 변경도 버전 증가로만 |
| `hbm_hits` | HBM 키워드 언급 횟수 (정수) |
| `process_hits` | 고유공정(TC본딩·TSV 등) 키워드 언급 횟수 (정수) |
| `discovery_reason` | 임계 통과/탈락 사유 문자열 (예: `hbm_hits>=5`, `임계 미달`, `선정일 현재 미상장`) |
| `review_status` | §3의 상태값 중 하나 |

## 3. 상태 정의와 전이 (중복 제거 확정)

```
NEW            발굴 직후, 기초 스크린 판단 전 (실행 중 임시 상태)
SCREENED_OUT   기초 스크린 탈락 (사유를 discovery_reason에 기재)
PART2_PENDING  기초 스크린 통과, 파트2 판정 대기
LEDGER_ADDED   파트2 판정 완료, 판정원장에 행 추가됨
REJECTED       파트2 판정 결과 테마 무관 확정 (근거 카드 보존)
```

전이 규칙 (이 외의 전이 금지):

```
NEW → SCREENED_OUT | PART2_PENDING
PART2_PENDING → LEDGER_ADDED | REJECTED
```

동결 규칙: `NEW`는 실행 중에만 존재한다. **동결 파일에 `NEW`가 남아 있으면
strict 로더가 중단한다(fail-closed).** 동결 후의 상태 변화(`LEDGER_ADDED`·
`REJECTED`)는 동결 파일을 수정하지 않고 **다음 심사시점 파일**에 반영한다
— 각 파일은 그 시점의 상태 스냅샷이다.

## 4. 근거 공시 — v1은 1건 원칙

v1은 종목당 **"선정기준일까지 공개된 최신 적격 정기보고서 1건"**만
사용한다(사업보고서 우선, 동일 연도 내 최초 접수분). 단순하고 재현성이
높다. 복수 공시(주요공시·계약공시 등)를 쓰는 확장은 `source_rcp_no`에
쉼표로 결합하지 않고, 별도 long-format 근거표
`data/candidates/candidate_evidence_long_<selection_date>.csv`
(`selection_date, ticker, rcp_no, disclosed_at, doc_type, hbm_hits,
process_hits`)를 신설해 처리한다 — v1 범위 밖이며 도입 시 계약 개정.

## 5. 시점 규율 (PIT)

1. **운영/백테스트 분리.** 전시장 발굴 실행(네트워크 조회)은 운영 시에만
   한다. 백테스트·재현 실행은 네트워크를 재조회하지 않고 **동결 CSV만
   소비**한다. 동결 CSV가 없는 심사시점은 "후보발굴 미실시"로 보고한다
   (조용한 생략 금지).
2. **공개근거 시점 필터.** 각 심사시점 파일에는 `disclosed_at <=
   selection_date`인 공개근거만 담는다.
3. **소급 금지.** 새 후보는 처음 공개근거가 생긴 다음 심사부터 들어간다.
   과거 심사시점 파일에 소급 추가하지 않는다. 과거 구간의 후보발굴을
   재구성할 수 없는 경우, 그 구간은 "후보군 고정" 상태임을 백테스트
   보고서에 명시하고 **상향 가능성이 큰 선택·생존편향**의 존재와 방향을
   고지한다(수학적으로 항상 위쪽이 보장되는 것은 아님).
4. **키워드 사전 PIT.** `keyword_version`은 심사시점에 유효했던 사전을
   가리킨다. 사전을 소급 적용해 과거 파일을 다시 만들지 않는다. 사전
   변경은 버전 증가 + 변경 이력 기록으로만 하며, 재실행 시 신구 버전
   결과를 별도 파일로 병기한다.

## 6. 판정원장과의 접합

- 후보발굴 원장은 판정원장의 **입구**다. `LEDGER_ADDED`가 되어도 편입
  여부는 기존 판정 규칙(규칙 0/A/C + 기초 유니버스 필터)이 정한다.
- 판정원장 유니버스 확장 시 `verdict_ledger` 스키마·strict 검증은 변경
  없다 — 행이 늘어날 뿐이다. NO_DATA·상장 전 조합 처리도 기존 규칙 그대로.
- 33종목 기존 후보는 소급 발굴 대상이 아니라 "v0 후보군"으로 간주하고,
  최초 운영 실행부터 신규 발굴분이 증분으로 붙는다.

## 7. 검증 게이트 (구현 시 테스트로 봉인)

1. strict 로더: 12열 스키마·dtype·상태값·중복 `(selection_date, ticker)`
   검사, 위반 시 fail-closed
2. PIT 검사: `disclosed_at > selection_date` 행 존재 시 실패
3. **상장 정합 검사**: `listed_asof`가 `listing_date <= selection_date`
   (및 상폐 여부)와 모순이면 실패
4. **동결 상태 검사**: 동결 파일에 `NEW` 잔존 시 실패
5. 동결 검사: 기존 파일 해시 변경 감지 시 실패 (재실행은 새 파일로만)
6. 커버리지 보고: 심사시점별 후보 수·상태 분포·미실시 시점 표

## 8. 미결(팀 확인 필요)

1. 발굴 임계값(`hbm_hits`/`process_hits` 기준)과 키워드 사전 v1의 확정 —
   유니버스·데이터 담당 기안, 위원회 승인
2. 전시장 열거 원천(KRX 상장 목록 API vs pykrx) 및 상폐 이력 원천
   (`listed_asof` 검증용)
3. 실행 주기 — 매 심사시점 필수 실행으로 방법론 2장에 명문화할지,
   운영 지침으로 둘지
