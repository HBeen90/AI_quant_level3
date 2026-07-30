# D3 KIND 관리종목 이력 조사

- 조사범위: 2020-01-01~2026-07-30
- 후보 유니버스: 33종목
- 공식 조회 분류: KIND 시장조치 `0350`(관리종목)
- 질의 수: 231건(종목별·연도별 정확 종목코드 질의)
- 양성 대조군: `003620` KG모빌리티 2건 검출
- 관리종목 이력 발견 종목: 0종목
- 자동 조사 상태: 완료 (`human_signoff` 미기입)
- 원응답 보관: `evidence/kind_admin_history_20260730/raw_kind_responses.zip`

## 판정

33종목 모두 공식 분류 0350 조회 결과가 0건이므로, 해당 기간의 관리종목 지정·해제 이력은 **미발견**입니다.

따라서 이 조사로 백테스트에 추가할 관리종목 수시편출 이벤트는 **0건**입니다.

## 조사 규율

- 기존 `admin_history_normalized.csv`의 Y/N 값은 판정 입력으로 사용하지 않았다.
- 현재 소속부나 사업보고서 문구로 과거 이력을 대체하지 않았다.
- KIND의 1년 검색 제한에 맞춰 연도별로 질의했다.
- 동일 수집기로 기지의 관리종목 지정 종목을 조회해 양성 검출을 확인했다.
- 각 응답 원문은 ZIP에 보존하고 SHA-256을 질의 로그에 기록했다.
- `미발견`은 조회 범위와 공식 분류코드에 한정된 결론이다.

## 산출물

- `data/admin_history_kind_2020_2026.csv`: 33종목 요약
- `evidence/kind_admin_history_20260730/events.csv`: 발견 이벤트 전량
- `evidence/kind_admin_history_20260730/query_log.csv`: 231개 질의와 응답 해시
- `evidence/kind_admin_history_20260730/control_log.csv`: 양성 대조군 결과
- `evidence/kind_admin_history_20260730/raw_kind_responses.zip`: 원응답
- `evidence/kind_admin_history_20260730/run_manifest.json`: 실행 계보

## 공식 조회 화면

- https://kind.krx.co.kr/disclosure/details.do?method=searchDetailsMain
