# 다음 단계 로드맵 — 지금 상태 진단과 우선순위

작성: 2026-07-26 | 대상: `develop_PR_rebalance_backtest_r5_2` + 인계 산출물 전체

> **상태 갱신 (2026-07-27):** 27%/67%는 현행 운영값으로 확정됐으며
> 회차별 재량 변경은 금지된다. 아래의 "27/67 확정" 표현은 최소 2회차
> 실측 축적 후 7.3 절차로 수행할 **사후 재검토**로 읽는다.

---

## 1. 진단 — 파이프라인은 다 있는데, 한 칸이 비었다

```
universe.py    selection.py     weighting.py     rebalance.py      backtest.py      index_calc.py
(임효빈)    →   (노민수)     →   (노민수)     →   (김소연)      →   (김소연)     →   (김인서)
  ⚠️ 빈 파일       ✅ 209줄         ✅ 297줄         ✅ 332줄          ✅ 610줄         ✅ 검증 완료
                                                    33/33 통과       동치성 1e-15
                    ▲
                    └── 여기에 넣을 "시점별 입력"이 1회분(2026-07-23)밖에 없다  ← 진짜 병목
```

엔진 품질은 이미 높습니다. 계층 분리·결정론·fail-closed가 일관되고,
리뷰 5회를 회귀 테스트로 봉인했고, 인서님 산출기와의 동치성을 1e-15 수준으로
실증했습니다. **문제는 코드가 아니라 코드에 넣을 데이터입니다.**

미결 항목을 의존관계로 세우면 하나로 수렴합니다:

| 미결 | 왜 못 닫는가 |
|---|---|
| 방법론 3장 "성과 보고"(수익률·변동성·MDD·회전율·상관계수) | 지수 시계열이 없음 |
| 유지 임계값 27/67 사후 재검토 | 최소 2회차 실측 민감도가 아직 없음 |
| selection 순방향 재생 33→7 | **2026-07-23 확정 단면으로 완료** |
| 회전율 기반 실제 용량(capacity) 산출 | 실측 회전율이 없음 |

현재 단면 선정은 닫혔지만, 성과·회전율·실측 민감도는 여전히 같은 원인으로
막혀 있습니다 — 시점별(PIT) 심사 스냅샷 13회분이 없습니다.

---

## 2. 이번에 채운 것

### 2-1. `analysis/index_calendar.py` — 일정 조문의 결정론적 구현

3장의 "6·12월 만기일 익주 첫 영업일"을 코드로 옮겼습니다. 휴장일 테이블을
따로 관리하지 않고 **실제 거래일 인덱스를 유일한 캘린더로** 삼습니다.

부수 확인 하나가 발표에서 쓸 만합니다:

> **기준일 2020-06-15는 임의 상수가 아니라 3장 조문의 산출값이다.**
> 캘린더 함수가 2020년 6월 만기일(둘째 목요일 6/11) → 익주 첫 영업일(6/15)을
> 독립적으로 재생했고, 그 값이 방법론 기준일과 정확히 일치합니다.
> 테스트로 봉인했습니다(`test_calendar_matches_methodology`).

### 2-2. `analysis/build_pit_snapshots.py` — PIT 스냅샷 생성기

병목을 직접 겨냥합니다. 기계가 할 일(pykrx 시총·거래대금·상장경과)과
사람이 할 일(HBM노출도·메모리향비중)을 나누고, 후자는 **판정 원장**에서 읽습니다.

핵심은 `as_of_ledger()` 한 함수입니다:

```python
d = d[d["disclosed_at"] <= asof]        # 그 시점에 공개돼 있던 자료만
return d.groupby("ticker").tail(1)      # 종목별 최신 1건
```

사업보고서는 회계연도 종료 후 ~3개월 뒤에 나옵니다. 2020년 6월 심사에
2020년 사업보고서(2021년 3월 공시)를 쓰면 미래를 훔쳐본 것이고, 성과표
어디에도 그 사실이 드러나지 않습니다. **look-ahead는 조용히 실패하기 때문에
테스트로 못 박아야 합니다** — `test_pit_changes_selection_outcome`이
"FY2019 노출도 20%면 2020년 미편입 / FY2021 55%면 2022년 편입"으로
PIT 규율이 실제 편입 결과를 바꾼다는 것까지 검증합니다.

기초 유니버스 필터(시총 350억·ADV60 10억·상장 3개월·유동비율 10%)도
여기서 적용하며 **탈락사유를 문자열로 남깁니다** — '남음' 2번(탈락 26종목의
사유까지 코드가 재현하는가)의 재료가 됩니다.

### 2-3. `analysis/run_backtest.py` — 실데이터 백테스트 드라이버

스냅샷 + pykrx 가격 → 이벤트 스케줄 → 지수 재생 → 지표 전 배선. 특히:

- **커버리지 진단이 먼저 나옵니다.** 엔진은 결측을 만나면 멈추는데(설계대로),
  예외 메시지만으로는 "2020년엔 아직 상장 전"인지 "수집 실패"인지 구분이 안 됩니다.
  그 구분을 표로 먼저 뽑아 줍니다.
- **`--policy all`이 버퍼 4안을 동일 조건으로 재실행**합니다. 지금은 합성
  데이터(`sensitivity_v2.py`)로만 있는 27/67 근거를 실측으로 바꾸는 경로입니다.
  선택 기준은 기존 원칙을 유지했습니다 — CAGR 단독으로 고르지 않습니다.
- 벤치마크 지수를 **코드가 아니라 이름으로** 찾고, 무엇을 골랐는지 콘솔에
  남깁니다. 지수 코드 하드코딩은 KRX 개편 때 조용히 틀린 지수를 물어옵니다.

### 2-4. `index_calc.build_index_series()` — 접합 노트 3-2 이행

`build_daily_series`는 정기변경 전용이라, 수시편출이 오면 호출자가 매번
저수준 조합을 손으로 써야 했습니다. 동치성 테스트 B의 참조 구현을 함수로
올렸습니다. 정기(제수 리셋) + 수시편출(ΔM<0) + 기업행위(ΔM 호출자 제공)를
한 경로로 처리하고, 정기변경일과 수시가 겹치면 **"상위에서 원자 병합하라"고
명시적으로 실패**합니다(`build_event_schedule`과 같은 규칙).

신규 테스트 4개로 봉인했습니다 — 정기 전용은 구 함수와 **상대차 0.00e+00**,
수시편출은 소연 파트 정규화 경로와 2.18e-15.

### 2-5. 문서-코드 정합성 + 재현성

README·methodology 머리말에 **"구성종목: 12종목 (앵커2·핵심7·위성3)"이 남아
있었습니다.** 같은 문서 2장에서 "고정 정원을 폐지한다"고 선언하므로 직접적인
자기모순이고, 심사·발표에서 가장 먼저 지적당할 지점입니다. 세 곳
(README·methodology·index_calc 헤더) 모두 정정했습니다.

한계점 절에 백테스트 데이터 한계·생존편향·유동비율 원천·유지 임계값 잠정을
신설했습니다. 그리고 `tests/run_all.py`로 **PowerShell에서 한 줄 실행**이
가능해졌습니다(`&&` 미지원 문제 해소). `pytest.ini`·`requirements.txt` 동봉.

*(이 시점 상태: 7개 파일 48개 테스트 통과. 이후 확장은 6절 참조.)*

---

## 3. 다음 4주 — 우선순위

### 1순위: 판정 원장 채우기 (사람 작업, 대체 불가)

이게 전부입니다. 나머지는 배선이 끝나 있습니다.

```powershell
python analysis/build_pit_snapshots.py --ledger data/verdict_ledger.csv --template-only
```

먼저 이걸 돌리면 **어느 시점에 몇 종목의 판정이 필요한지** 표로 나옵니다.
`data/verdict_ledger_TEMPLATE.csv`가 형식 예시입니다.

작업량을 줄이는 순서 제안:

1. **필수 시점을 먼저 채웁니다.** 원장 1행은 심사시점이 아니라
   `(종목, 사업연도)` 한 쌍이므로 현재 골격은 223행입니다. 값이 같아 보여도
   2026 판정을 과거로 복사하지 않고 해당 연도 공개 자료로 확인합니다.
2. **사업연도별 카드를 생성합니다.**
   `python hbm_evidence.py --input universe_code.csv --fiscal-year 2025`처럼
   실행하면 해당 연도 사업보고서만 조회합니다.
3. **일부 연도만 확보되면 보간하지 않습니다.** 공개 판정이 존재하는 시점만
   산출하고 커버리지 부족을 보고합니다. 미관측 판정을 보간하면 패시브 규칙의
   재현성이 사라집니다.

### 2순위: 백테스트 실행 → 27/67 사후 점검 (기계 작업, 반나절)

```powershell
python analysis/build_pit_snapshots.py --ledger data/verdict_ledger.csv --out data/snapshots
python analysis/run_backtest.py --snapshots data/snapshots --prices-cache out/px.csv --coverage-only
# 커버리지 경고를 정리한 뒤
python analysis/run_backtest.py --snapshots data/snapshots --prices-cache out/px.csv --policy all
```

27/67은 현행 운영값이며 회차별로 재량 조정하지 않습니다.
`policy_comparison.csv`는 최소 2회차 실측이 축적된 뒤 방법론 개정 절차(7.3)의
재검토 근거로만 사용합니다.

### 3순위: 생존편향 정면 대응 (반나절, 발표 방어력 큼)

`run_backtest.listing_check()`로 시점별 상장 명단과 대조합니다. 더 중요한 건
**그 시점엔 자격이었으나 지금은 없는 종목**입니다 — 이건 코드가 못 찾고
사람이 찾아야 합니다. 최소한 "찾아봤고 N종목이 해당하며 편향 방향은 위쪽"
이라고 말할 수 있으면, "생존편향 고려 안 했다"는 지적을 정면으로 막습니다.

### 4순위: 방법론 확장 (여유 있으면)

- **용량 기반 비중 상한**: `capacity_analysis.py`는 지금 '초기 전량 편입' 기준의
  보수적 상한만 봅니다. 실측 회전율이 나오면 "AUM X억에서 정기변경 1회를
  소화하는 데 며칠"로 바꿀 수 있고, 이게 소부장 유동성 한계를 수치로
  방어하는 가장 설득력 있는 형태입니다.
- **TR 지수**: `simulate_index(mode="gross_tr")` 경로는 이미 있고 배당 데이터만
  없습니다. pykrx 배당 수집을 붙이면 PR/TR 병기가 됩니다.
- **유동비율 원천 단일화**: 교차검증 리포트의 마지막 잔여 안건. 하이닉스
  −3.3%·디아이 +23.8% 차이가 순수 FF 원천 차이로 100% 설명된 상태이므로,
  KRX 공식 유동비율을 확보하면 숫자 한 곳 교체로 끝납니다.
- **데이터 계약 v2 강제**: 계보 5필드를 `--require-lineage`로 이미 강제할 수
  있게 해뒀습니다. 팀 인계 시 기본값으로 켜기를 권합니다.

---

## 4. 발표에서 바로 강해지는 문장들

| 지금 | 이번 작업 후 |
|---|---|
| "동치가 되도록 설계했다" | "인서님 산출기와 공동 대조 테스트로 동치를 실증했다(정기 0.00e+00, 수시 2.18e-15)" |
| "기준일은 2020-06-15로 정했다" | "기준일은 3장 일정 조문의 산출값이다 — 캘린더 함수가 독립 재생했고 테스트로 봉인했다" |
| "버퍼 27/67은 잠정이다" | "실측 6년 민감도에서 회전율 −X%p 대 적합도 −Y%p 트레이드오프로 채택했다" |
| "백테스트로 성과를 산출한다" | "PIT 원장으로 look-ahead를 구조적으로 차단했고, 그 규율이 편입 결과를 실제로 바꾼다는 것을 테스트로 보였다" |
| (생존편향 미언급) | "시점별 상장 명단과 대조했고, 잔여 편향의 방향과 크기를 고지한다" |

---

## 5. 파일 목록 (1차 작업분)

```
analysis/index_calendar.py          신규  일정 조문 결정론 구현
analysis/build_pit_snapshots.py     신규  PIT 스냅샷 생성 (as_of_ledger)
analysis/run_backtest.py            신규  실데이터 백테스트 드라이버
src/index_calc.py                   수정  build_index_series 추가 + 헤더 v2 현행화
README.md / docs/methodology.md     수정  12종목 잔존 문구 정정 + 한계점 신설
tests/test_run_backtest_smoke.py    신규  드라이버 end-to-end (pykrx 불필요)
tests/test_pit_snapshots.py         보강  PIT 규율 6종(감사의견 하드 탈락 포함)
tests/test_ledger_bridge.py         신규  확정/잠정 원장·사업연도 근거수집 7종
tests/test_index_calc_series.py     신규  build_index_series 동치성 4종
tests/run_all.py                    신규  한 줄 실행 러너 (PowerShell 대응)
conftest.py / pytest.ini            신규  pytest 전환
requirements.txt                    신규  버전 고정
data/verdict_ledger_TEMPLATE.csv    신규  판정 원장 형식 예시
```

엔진 코드(`rebalance.py` · `backtest.py` · `selection.py` · `weighting.py`)는
**한 줄도 건드리지 않았습니다.** 수정한 것은 `index_calc.py`(기능 추가 +
헤더 현행화)와 문서 2개뿐이며, 기존 33개 테스트는 그대로 통과합니다.

---

## 6. 이후 추가된 것 (외부 리뷰 대응 · 대시보드 · 수치 검증)

로드맵 작성 이후 세 라운드가 더 진행됐습니다.

### 6-1. 외부 리뷰 감사 → `docs/REVIEW_AUDIT.md`

리뷰 7개 항목을 **구현 전에 코드로 검증**했습니다. 절반은 이미 구현돼
있었고(월말 30% 캡), 하나는 개념 정정이 필요했으며(PR 배당락은 오류가 아님),
하나는 역산해보니 안전하지 않았습니다(위성 5% 상한 = ADV 15억에서 100거래일).

```
src/index_calc.py                수정  adjust_divisor_for_dividend + dividend 이벤트
analysis/run_backtest.py         수정  --mode pr|gross_tr|both, --dividends
analysis/audit_review_claims.py  신규  리뷰 지적 재현 검증
analysis/capacity_v2.py          신규  Δw 기반 용량 + 유동성 함의 상한 역산
tests/test_tr_equivalence.py     신규  TR 두 경로 동치 4종
```

### 6-2. Streamlit 대시보드 → `RUN_APP.md`

```
app.py                           신규  6화면 대시보드 (①~③은 데이터 없이 동작)
tests/test_app_smoke.py          신규  streamlit 없이 대시보드 로직 검증 5종
```

계산은 전부 기존 모듈이 하고 `app.py`는 표시만 합니다 — 대시보드가 자체
계산 경로를 가지면 "엔진과 대시보드가 다른 답을 내는" 사고가 시작됩니다.

### 6-3. 수치 검증 체계 → `docs/DASHBOARD_NUMBER_AUDIT.md` · `docs/FACTSHEET.md`

별도로 제작된 대시보드의 표시 수치를 엔진으로 검산한 결과, 목표 비중이
재현되지 않고(최대 오차 11.32%p) 조문 상한을 위반했으며(한미반도체 29.32%
> core 18%) 화면끼리도 모순됐습니다. 편출입 건수는 `demo_why_pit.py` 의
**합성 출력과 8개 숫자 전부 일치**했습니다.

같은 사고가 세 번 반복됐으므로 사후 적발 대신 **구조적 차단**을 넣었습니다:

```
analysis/audit_dashboard_numbers.py  신규  화면 수치 검산 (비중·상한·정합·출처)
analysis/verify_claims.py            신규  발표 문장 재현 검증 + 인용 금지 스캐너
tests/test_claims.py                 신규  클레임 등록부 회귀(스캐너 오탐·미탐 포함)
docs/FACTSHEET.md                    생성  verify_claims --factsheet 산출물
docs/00_INDEX.md                     신규  문서 진입점
```

**규칙: `verify_claims.py` 에 재현 함수가 등록된 수치만 인용한다.**
발표 전 `python analysis/verify_claims.py` 가 전부 PASS 여야 합니다.

### 6-4. 2026-07-23 확정 판정 단면

파트2의 확정 PDF 2종과 구성종목 인계 CSV를 패키지에 보존하고,
`analysis/verify_judgment_snapshot.py`로 판정과 비중을 독립 재생했습니다.

- 후보 33 -> 편입 7(앵커 2·핵심 4·위성 1) 전량 일치
- 공표 비중 합계 100%
- 인계 유동시총 기반 비중 최대 오차 0.001302%p
- 테크윙 30% 경계 편입, 솔브레인 70%이나 C2 미충족, 와이씨켐 C 3요건
  충족을 회귀 테스트로 고정

단, 이 자료는 한 시점의 횡단면이다. 역사적 PIT 원장으로 소급하지 않는다.

---

## 7. 현재 상태

**13개 파일 107개 테스트 통과 · 확정 단면 교차검증·판정 원장 경계·발표
문장 등록부 회귀 포함**

```powershell
python tests/run_all.py                              # 13/13 파일 · 107 케이스
python analysis/verify_judgment_snapshot.py          # 2026-07-23 33→7·비중 재현
python analysis/verify_claims.py                     # 발표 문장 재현
python analysis/verify_claims.py --scan 발표자료.md  # 금지 수치 유출 점검
streamlit run app.py                                 # 대시보드
```

2026-07-23 단면은 완료했습니다. 남은 병목은 **역사적 PIT 판정 원장**이며,
이를 채우기 전에는 성과·회전율·벤치마크 수치를 해제하지 않습니다.
