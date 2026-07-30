# 벤치마크 확정 준비 메모 v2

작성일: 2026-07-28
상태: `PROVISIONAL`

## 1. 운영 결정

- 헤드라인 벤치마크는 **KRX 반도체 PR**로 한다.
- `gross_tr` 결과는 **KRX 반도체 TR**과 별도로 비교한다.
- `both` 실행은 PR과 TR을 각각 조회한다. PR 결과가 헤드라인이고, TR 결과는
  두 시계열이 함께 존재하는 공통기간의 보조 비교다.
- 코스피 200과 동일 33종목 동일가중은 보조 비교군이며, iSelect 글로벌
  HBM반도체 지수는 참고 병기 대상이다. 현재 공식 추종오차 배선은
  `data/benchmark.yaml`의 `primary`만 소비한다.

PR 채택은 국내 반도체 섹터 대비 HBM 선별 효과를 설명하기 위한 운영
결정이다. TR을 사용할 수 없다는 전제에 근거한 결정은 아니다.

## 2. TR 이력 해석

KRX 반도체 TR의 산출 개시일은 2024-12-09이다. 다만 당시 보도에는
2011년 이후 성과 비교가 함께 제시되어 과거 시계열이 소급 제공됐을 가능성이
있다. 따라서 **산출 개시일을 데이터 최초 관측일로 간주하지 않는다.**

공식 사용 가능 기간은 다음 명령이 실제로 반환한 최초·최종 관측일로 정한다.

```powershell
python analysis\resolve_benchmark_code.py
```

resolver는 한국어·영문 표기 후보를 보여주고, 정확히 한 개씩 식별된 PR/TR에
대해 코드·정확한 이름·최초일·최종일·관측 수를 출력한다. 후보가 없거나
복수면 자동 확정하지 않는다.

## 3. CONFIRMED 전환 조건

아래 항목이 모두 충족되기 전에는 `status: PROVISIONAL`을 유지한다.

1. resolver로 PR/TR 실제 코드와 정확한 표기명을 확인한다.
2. 조회된 각 계열의 최초 관측일을 회의록에 기록한다.
3. `primary.pr_name`, `primary.tr_name`, `pr_code`, `tr_code`를 입력한다.
4. 위원회가 `effective_date`와 `resolved_by`를 입력한다.
5. 원천 시계열의 저장·재배포·발표 사용 조건을 확인한다.
6. 설정 반영 후 테스트와 PR/TR 공통기간 결과를 재생한다.

## 4. 근거 자료

- KRX 반도체 지수는 KRX 섹터지수 계열에 포함된다.
  <https://global.krx.co.kr/contents/GLB/02/0201/0201040213/GLB0201040213.jsp>
- KODEX 반도체 ETF는 KRX Semicon 계열을 기초지수로 사용한다.
  <https://www.samsungfund.com/etf/product/view.do?id=2ETF07>
- KRX 반도체 TR 산출 개시와 2011년 이후 비교 성과가 함께 보도됐다.
  <https://www.yna.co.kr/view/AKR20241204156900008>

## 5. 발표 표현

> 헤드라인 벤치마크는 국내 반도체 섹터와의 비교 목적에 따라 KRX 반도체
> PR로 지정했다. TR은 별도 보조 계열로 조회하며, 사용 가능 기간은 출범일을
> 추정해 정하지 않고 KRX 원천 시계열의 실제 최초 관측일로 확정한다.
