# 동기화 사고 기록 — 보강 세션의 부분 회귀와 복구 (2026-07-30)

## 무슨 일이 있었나

FINAL 잠금 보강 세션(별도 작업 사본에서 수행 후 정본으로 동기화)이
의도한 보강과 함께 **정본의 최신 상태 일부를 구판으로 되돌렸다.**

- 유지된 보강(유효): `make_backtest_manifest.py` 해시·날짜·commit 검증 강화,
  `run_backtest_final.ps1` 깨끗한 작업트리 요구·생성물만 커밋,
  `test_claims.py` 변조 재잠금 회귀 테스트, 게이트 양식 날짜 검증.
- 회귀 1 (기능 소실): `analysis/verify_claims.py` 가 게이트 해제 장치
  (백테스트 클레임·동적 인용금지·잠정수치 스캐너) 이전 구판으로 교체됨 —
  새 회귀 테스트가 참조하는 API(`_BT_DIR` 등)가 모듈에서 사라져 상호 불일치.
- 회귀 2 (데이터 되돌림): **322310 정정(B안)이 적용된 원장이 정정 전
  해시(`E89BDCE9…`)로 회귀**, `verify_judgment_snapshot.py`(판정군 교차대조)·
  `test_judgment_snapshot.py`·단면 meta 도 구판으로 교체.

## 어떻게 발견·복구했나

부분 회귀 세트는 **자기일관적**이라 구판 테스트끼리는 전부 통과했고,
run_all(파일 직접 실행) 검증만으로는 잡히지 않았다. 유일한 비일관 지점
(새 test_claims ↔ 구판 verify_claims)이 pytest 수집에서 실패하며 드러났다.

복구(2026-07-30, pytest 99개·run_all 14/14 통과 확인):

1. `verify_claims.py` 게이트 판 복원 + 보강 의도를 실제 구현 —
   `_final_manifest_valid()` 가 FINAL 매니페스트의 산출물·스냅샷·원장
   해시를 현재 파일과 전수 대조하고, 하나라도 불일치하면 FINAL 을 무효화해
   수치를 다시 잠근다(새 회귀 테스트가 이 경로를 봉인).
2. B안 정정 데이터 일체 재반영: 원장(`EAB08F09…`)·judgment_input·스냅샷
   2개·백테스트 산출물·잠정 매니페스트·단면 meta.
3. 판정군 교차대조 판 복원: `verify_judgment_snapshot.py`(예외 0 상태)·
   `test_judgment_snapshot.py`.
4. FACTSHEET 재생성(클레임 14건·FAIL 0·동적 인용금지).

## 재발 방지

1. **정본 레포 단일 작업 원칙** — 별도 사본에서 작업했으면 파일 복사가
   아니라 diff 검토를 거친 선별 반영만 한다. (세 번째 반복된 사본 분기)
2. 커밋 전 검증은 `pytest` 와 `tests/run_all.py` **둘 다** — run_all 은
   `__main__` 목록 기반이라 수집 불일치를 못 본다.
3. 데이터 파일(원장·스냅샷·meta)은 해시를 정정 공표 문서와 대조 후 커밋
   (`verdict_ledger_correction_20260730.md` §3의 전후 해시가 기준).

*본 기록은 방법론 검증용이다.*
