# PR: src/rebalance.py · backtest/backtest.py 구현 (김소연 파트, v2)

## 채운 것
- src/rebalance.py : 히스테리시스 버퍼(신규 30/70 · 유지 27/67 잠정) ·
  무대체 수시변경 · 5종목 하한 · CSV 로더(6자리 문자열 강제)
  + selection.py 접합(select_from_selection: 한글 스냅샷 -> 히스테리시스 -> 군 확정)
  + **비중은 weighting.allocate 위임**(잠정 자체 구현 삭제 - 귀속 원칙)
- backtest/backtest.py : v2 이벤트 스케줄러(편출 공지 D+2 · 월간캡 D+2 ·
  예약 무효화) + 지수 재생 · 지표(수익률/변동성/MDD/회전율/상관/추종오차/비용)
- tests/ : 명세 9 + 스케줄러 14(리뷰 3 + 안건3 4 + 안건1·2 4) + 통합 8 = **31/31**
  (CP949 콘솔 검증. 실행은 한 줄씩 - PowerShell 5.1은 && 미지원:
  `python tests/test_v2.py` / `python tests/test_schedule_v2.py` /
  `python tests/test_develop_integration.py`)
- analysis/sensitivity_v2.py : 버퍼 정책 민감도(다중 seed, --seeds/--out)

## 접합 확인 사항
- 유지 판정식은 selection.classify_row 를 임계값만 치환해 복제 -
  hold=entry 일치성 테스트로 규칙 동일성 보장
- assign_weights_v2 == weighting.compute_weights (1e-12), verify() 무위반
- 희소 조항(수용량<60% -> 앵커 흡수·합계 100% 우선)은 weighting 동작을
  그대로 따름 - **팀 확인 4번(앵커 1종목)도 동일 원리로 처리됨을 확인**
- IIF 산출까지 스모크 통과(index_calc 인계 규격)

## 리뷰 반영(r3)
- [P1] 정기변경 지연 계산: prev_members = 시행일 현재 vm.weights.index -
  기중 편출 종목은 신규 기준(30%/70%) 적용, 재편입 회귀 테스트 추가
- [P1] 정기변경일=편출 D+2 원자 병합: 하드 편출을 스냅샷에 선반영해
  이벤트 1건으로 산출(회전율 이중 계상 방지), 회귀 테스트 추가
- [P2] 월간 캡 '정확히 D+2 거래일' 검증 강화, exposure>0 잔여 docstring 삭제,
  비용 주석을 '왕복 30bp x 편도 회전율'로 통일

## 안건 3 확정 반영 (v2.1)
- 하한 미달 -> 산출 지속 + under_min 플래그(수시·정기 공통), 전 종목 편출만 산출 불가
- 긴급심사: 공표일 A 기준 A+2 편입 · PIT 스냅샷 · 후보 없으면 폴백 ·
  누적 하드 편출 부활 금지 · window 초과 시 termination_review_due 마커
- rulebook_version v2.1+continuity. 60영업일은 팀 운영안(파라미터)

## 안건 1·2 확정 반영 (v2.2)
- apply_suspensions: 거래소 확인 정지 기간만 최종 체결가 carry(재개 시 복귀),
  미등록 결측 fail-closed 유지. 편출가 워터폴은 데이터 계약으로 명문화
- 합병: 소멸 종목 무대체 편출 + 동일자 원자 병합(기구현) + 거래조건가는
  워터폴 계약. 주식수 승계·제수는 index_calc 경계
- rulebook_version v2.2+suspension-merger

## 미결(변동 없음)
- 유지 임계값 27/67은 실측 데이터 전 잠정 - hbm_evidence 카드 산정 후 재검토
- 심텍(222800)/SFA넥셀(222080) DART 대조 확정 대기
- .gitignore data/raw/*.csv 로 universe*.csv 미푸시 상태 - 규칙 수정 필요
