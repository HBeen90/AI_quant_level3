# FINAL 판정원장 정정 공표 — 322310 오로스테크놀로지 FY2024·FY2025

정정일: 2026-07-30 · 근거 절차: 판정 추인 원문 검사(파트3 세션)
원칙: 산출 오류 정정은 정정 사실·범위·전후 값을 공표한다(거버넌스 초안 7.3(1)).

## 1. 정정 사실과 범위

| 필드 | 정정 전 | 정정 후 |
|---|---|---|
| 322310 FY2024 `process_confirmed` / `committee_ok` | True / True | **False / False** |
| 322310 FY2025 `process_confirmed` / `committee_ok` | True / True | **False / False** |

다른 213행·전 수치 필드는 변경 없음. 215행·33종목·NO_DATA 8행 구조 불변.

## 2. 근거 — DART 원문 검사 (2026-07-30)

`analysis/check_judgment_ratification.py`로 해당 사업연도 사업보고서 원문을
직접 검사한 결과:

- FY2024(rcpNo 20250312000665, 본문 208,418자): **HBM 계열 언급 0회**
- FY2025(rcpNo 20260313000952, 본문 216,609자): **HBM 계열 언급 0회**
- TSV 서술은 존재하나("Overlay & CD 계측장비는 TSV라는 후공정에서 사용")
  **HBM 귀속 없는 일반 후공정 서술**이며, 두 해 발췌가 사실상 동일
  보일러플레이트로 FY2023(False)→FY2024(True) 전환을 지지할 문서적 차이가
  없다.
- 판정 규칙("HBM 고유공정 귀속을 **그 해 문서로** 확인한 경우만 TRUE")에
  따라 False가 정본이다. 2026-07-23 확정 단면의 C2 판정이 옳았고, 원장의
  True/True는 문서 외 정황(뉴스·국가전략기술 지정 등)이 유입된 오기로
  판단한다. 발췌·해시: `evidence/judgment_ratification_20260730/`

## 3. 전후 해시 (정정 계보)

| 파일 | 정정 전 SHA-256 | 정정 후 SHA-256 |
|---|---|---|
| `evidence/judgment_input_final.csv` | `3A217442…AF9F4E6` | `62844723…3711BAC` |
| `data/verdict_ledger.csv` | `E89BDCE9…682247F` | `EAB08F09…25936EC` |

정정 후 원장은 canonical 빌더(`build_ledger_from_evidence.py`) 재실행 산출물이며
strict 계약(215행·FINAL·중복 0)을 그대로 통과한다. 2026-07-29 배포 패키지의
매니페스트·SHA256SUMS·감사용 XLSX는 **당시 상태의 역사 기록으로 보존**하고
수정하지 않는다 — 본 문서가 정정의 정본이다. (감사용 XLSX 재생성은 후속
정비 항목)

## 4. 파급 반영 (전부 완료)

1. PIT 스냅샷 `snapshot_20251215.csv`·`snapshot_20260615.csv`: 322310
   `eligible=False`(사유: 공정/위원회 미확인) — 빌더의 위성 하드요건 접기와
   동일한 산출(시장 데이터 컬럼 불변).
2. 잠정 백테스트 재실행(정책 4안): 오로스 편입 소멸 →
   **최종 구성 7종목으로 2026-07-23 확정 단면과 완전 일치**. 시계열 차이는
   2025-12-16부터 미세(±0.1% 미만, 수치는 FINAL 게이트 전 인용 금지).
   새 잠정 매니페스트: `out/backtest/backtest_run_manifest_PROVISIONAL.json`
3. 교차대조 예외 해소: `verify_judgment_snapshot.ALIGNMENT_GROUP_EXCEPTIONS`
   를 빈 집합으로 — 단면·원장이 33종목 전부 예외 없이 정합(테스트 봉인).
4. 단면 meta의 정정 항목 E1: `RESOLVED_BY_LEDGER_CORRECTION_20260730`로 종결.
5. 판정 기록(`judgment_record_322310_atsemicon_20260730.md`)에 번복 기록 추가.
6. 게이트 양식 `judgment_322310` 문구를 B안 추인으로 갱신.

## 5. 남는 확인 1건 (도구 검증)

원문 검사기의 본문 수집이 원 수집기와 동일 말뭉치인지의 양성 대조 —
한미반도체 FY2025(rcpNo 20260312001230, 원장 비고 기준 언급 63회 내외
기대)를 같은 도구로 조회해 다수 언급이 검출되면 도구 검증 완결. 로컬 실행:

```powershell
python -c "import sys; sys.path.insert(0,'.'); from src.hbm_evidence import _clean,_make_dart,HBM_KW; import os; from dotenv import load_dotenv; load_dotenv(); d=_make_dart(os.environ['DART_API_KEY']); t=_clean(d.document('20260312001230')); print('한미 FY2025 HBM 언급:', sum(t.count(k) for k in HBM_KW))"
```

*본 문서는 방법론 검증용이며 특정 종목 투자 권유가 아니다.*
