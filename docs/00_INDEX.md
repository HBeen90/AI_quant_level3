# 문서 안내 — 어디부터 읽나

목적별 진입점입니다.

## 상황별

| 지금 하려는 일 | 읽을 문서 | 소요 |
|---|---|---|
| **내일 발표한다** | **`FACTSHEET.md`** | 5분 |
| 대시보드에 숫자를 넣으려 한다 | `DASHBOARD_NUMBER_AUDIT.md` | 10분 |
| 다음에 뭘 할지 정한다 | `DEVELOP_ROADMAP.md` (1절 → 7절) | 15분 |
| "PIT 스냅샷"이 뭔지 모르겠다 | `WHAT_IS_PIT_SNAPSHOT.md` | 15분 |
| 판정 원장을 실제로 채운다 | `VERDICT_LEDGER_GUIDE.md` | 10분 |
| 판정 원장 원본·재구성 계보를 확인한다 | `ledger_lineage_correction_20260729.md` | 5분 |
| FINAL 전 확인 요청과 종결 결과를 본다 | `verdict_ledger_team_request_v2_20260729.md` → `verdict_ledger_final_decision_record_20260729.md` | 5분 |
| 2026-07-23 확정 판정 재현 결과를 본다 | `JUDGMENT_SNAPSHOT_20260723_REPORT.md` | 3분 |
| 확정 단면과 역사적 초안의 경계를 본다 | `HBM_JUDGMENT_DRAFT.md` | 5분 |
| 상용지수 비교의 채택·참고 경계를 본다 | `commercial_index_gap_review.md` | 5분 |
| 공식 벤치마크의 확정 조건을 본다 | `benchmark_confirmation_memo.md` | 5분 |
| 외부 리뷰를 받았다 | `REVIEW_AUDIT.md` | 15분 |
| 대시보드를 켠다 | `../RUN_APP.md` | 2분 |

## 발표 전 체크리스트

```powershell
python tests/run_all.py                              # 13/13 파일 · 113 케이스
python analysis/verify_claims.py --fast              # 발표 문장 재현(테스트 재실행 생략)
python analysis/verify_claims.py --scan 발표자료.md  # 금지 수치 유출 점검
```

뒤의 둘이 이 프로젝트의 **가장 중요한 안전장치**입니다.
`verify_claims.py` 에 재현 함수가 등록된 수치만 인용합니다. 등록되지 않은
수치는 — 합성이든 가정이든 추정이든 — 발표·문서·화면 어디에도 쓰지 않습니다.

같은 사고(출처 없는 숫자가 실림)가 세 번 반복돼서 넣은 규칙입니다.
자세한 배경은 `FACTSHEET.md` 서두에 있습니다.

## 검증 스크립트

합성·단위 검증은 외부 데이터가 필요 없지만 전체 E2E는 PC에 따라 수 분 걸린다.

| 명령 | 무엇을 보여주나 |
|---|---|
| `python analysis/verify_claims.py` | 발표 문장을 지금 재현 (+ 감사 결과 분리 표시) |
| `python analysis/verify_claims.py --scan <파일…>` | 문서에서 인용 금지 수치 유출 검출 |
| `python analysis/verify_claims.py --factsheet-out docs\FACTSHEET.md` | `FACTSHEET.md` UTF-8 재생성 |
| `python analysis/demo_why_pit.py` | 왜 판정 원장이 필요한가 (FROZEN 0회 vs PIT) |
| `python analysis/audit_review_claims.py` | 월말 캡 실증 + 버킷 드리프트 발견 |
| `python analysis/capacity_v2.py --aum 3000` | 고정 % 상한의 역산 한계 |
| `python analysis/audit_dashboard_numbers.py` | 대시보드 표시 수치 검산 |
| `python analysis/index_calendar.py` | 정기변경 일정이 조문에서 재생 |
| `python analysis/verify_judgment_snapshot.py` | 2026-07-23 확정 33종목 판정·7종목 비중 재현 |
| `python analysis/resolve_benchmark_code.py` | KRX 반도체 PR/TR 코드·이름·시계열 확인 |

## 한 줄 요약

**2026-07-23 확정 단면과 역사적 PIT 판정 원장은 재현 완료됐습니다.**
성과 수치는 시장 데이터 커버리지와 벤치마크 계보를 확정한 뒤 전구간
백테스트를 실행할 때까지 잠근다.

## 재현성 메모

날짜 의존 산출물(정기변경 횟수·백테스트 종료일 등)은 `INDEX_ASOF`로
고정할 수 있습니다. 가격 캐시의 이후 관측치도 자동으로 제외됩니다.

```powershell
$env:INDEX_ASOF = "2026-07-26"
python analysis/demo_why_pit.py
```

지정하지 않으면 오늘 날짜를 씁니다. 스크립트마다 날짜를 박아 두면 다음 달에
조용히 낡기 때문에 `index_calendar.as_of_today()` 한 곳으로 모았습니다.
