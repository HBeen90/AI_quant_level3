# PR: src/rebalance.py · backtest/backtest.py 구현 (김소연 파트, v2)

## 채운 것
- src/rebalance.py : 히스테리시스 버퍼(신규 30/70 · 유지 27/67 잠정) ·
  무대체 수시변경 · 5종목 하한 · CSV 로더(6자리 문자열 강제)
  + selection.py 접합(select_from_selection: 한글 스냅샷 -> 히스테리시스 -> 군 확정)
  + **비중은 weighting.allocate 위임**(잠정 자체 구현 삭제 - 귀속 원칙)
- backtest/backtest.py : v2 이벤트 스케줄러(편출 공지 D+2 · 월간캡 D+2 ·
  예약 무효화) + 지수 재생 · 지표(수익률/변동성/MDD/회전율/상관/추종오차/비용)
- tests/ : 명세 9 + 스케줄러 16(리뷰 3 + 안건3 4 + 안건1·2 4 + r5 회귀 2) +
  통합 8 = **33/33**
- 재현 기준: develop@0a2ca32 트리에 본 패키지 오버레이. 의존 모듈
  (src/selection.py · src/weighting.py, 민수님 파트 원본)을 참조용으로
  동봉했으므로 zip 단독으로도 33/33 재현 가능
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

## r5 리뷰 반영 (r5.1)
- [P1-3] 편출 집행 시 예약 긴급편입 전량 무효화(취소 마커) - 편출 종목
  부활 차단, 재공표 필요. [P1-4] 회복 시 term_logged 리셋 - 반복 미달마다
  이관 마커 재발생. 각 회귀 테스트 추가
- [P1-5] 지원 범위 명시: 합병은 현금·단순 편출만 백테스트 지원. 주식교부
  합병의 존속회사 승계는 index_calc 목표비중 이벤트 소비 경로로 반영
- [P1-2] README.md·docs/methodology.md 에 v2 확정 조문 전체 반영(본 PR 포함)
- [P2] 군 이동 테스트 68%로 유지 기준 실검증, 종료 테스트 or True 제거·
  정확 시점 단언, 합병 pro-rata 수치 검증, 잔여 '산출 중단' 문구 정정

## r5.1 최종 마감 (r5.2)
- [P1] 편출가 최종 폴백 = 최소가격 0.0001원으로 확정 기입(v8 승계) -
  README·methodology 반영. 팀이 0원 채택 시 숫자 1곳만 교체
- [P2] 소스 헤더 현행화: assign_weights_v2 = weighting.allocate 위임 어댑터,
  MethodologyReviewRequired = 전 종목 편출 등 진짜 산출 불가에만 사용

## index_calc(인서님) 인계 과제
- 주식교부 합병: 존속회사 주식수 증가를 반영한 '최종 목표비중 이벤트' 생성
  기능이 index_calc에 아직 없음. 생성되면 backtest.simulate_index 가 그대로
  소비하는 인터페이스(이벤트 규격 동일)로 백테스트 반영 완성

## 미결(변동 없음)
- 유지 임계값 27/67은 실측 데이터 전 잠정 - hbm_evidence 카드 산정 후 재검토
- 심텍(222800)/SFA넥셀(222080) DART 대조 확정 대기
- .gitignore data/raw/*.csv 로 universe*.csv 미푸시 상태 - 규칙 수정 필요
