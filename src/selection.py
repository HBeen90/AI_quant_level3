"""
HBM 테마 커스텀 인덱스 - 2.2.2 편입 판정 · 2.3 종목 선정  (담당: 노민수)

역할: `universe.py`(임효빈)가 걸러 준 후보를 받아 **규칙 0/A/C로 3군 분류**하고,
      **규칙 통과자 전원을 편입**한다. 종목 수는 고정 정원이 아니라 **규칙이 정한다**
      (2026-07-22 개정 - 아래 [개정 이력] 참조). 비중 산정은 하지 않는다 → `weighting.py`.

[개정 이력] 고정 정원(핵심 7 · 위성 3) 폐지 → 규칙 기반 가변 편입
    · 이유 1: 임계값을 통과하고도 정원에 밀려 배제되는 종목이 생기면
      "노출도 30% 이상은 핵심군"이라는 규칙 자체가 무력화된다(8등의 억울함).
    · 이유 2: HBM 생태계가 성장해 자격 기업이 늘어도 지수가 따라 커지지 못한다.
    · 안전장치: 잡성 종목 유입은 사전 스크린(universe.py 거래대금 하한)이,
      집중 위험은 개별·합계 상한(weighting.py)이 통제한다 - 정원 없이도 안전.
    · 현 예시 데이터에서는 자격자가 정확히 앵커2·핵심7·위성3 = 12라
      **개정 전후 결과가 동일**하다(검증 완료). 바뀌는 것은 미래의 동작뿐.

파이프라인에서의 위치
    universe.py  →  **selection.py**  →  weighting.py  →  index_calc.py
    (임효빈)          (노민수)            (노민수)          (김인서)

[주의] 사전 스크린(자본잠식·관리종목·감사의견·거래대금·유동비율 등)은 **본 모듈의 책임이 아니다.**
   팀 README '기초 유니버스 필터'에 따라 `universe.py`에서 이미 처리된 것으로 전제한다.

입력 컬럼 (표준명) - 다른 이름으로 오면 `adapt_columns()`로 변환할 것
    필수   : 종목명, 코드, 유형, 유동시총
    규칙 0 : HBM양산(bool)          ← 유형='메모리제조'와 **함께** 참이어야 앵커(결합 요건)
    규칙 A : HBM노출도(0~1)
    규칙 C : 메모리향비중(0~1), HBM공정확인(bool), 위원회확인(bool)

출력: 입력 컬럼 + `군`('앵커'|'핵심'|'위성'). 행 수 = 규칙 통과자 수(가변).

사용 예
-------
    import selection, weighting

    # universe.py 컬럼명이 표준명과 다르면 mapping으로 맞춘다(로직은 손대지 않음)
    MAP = {"float_mcap": "유동시총", "exposure": "HBM노출도", "sector": "유형"}

    sel = selection.select_constituents(universe_df, mapping=MAP)  # → 규칙 통과자 전원 + 군
    res = weighting.compute_weights(sel)                           # → 편입비중 + IIF
    assert not weighting.verify(res), weighting.verify(res)        # 합계·상한 자동 점검

[TEAM] = 팀 공식본에 정의가 없어 확정한 항목 (근거는 `내파트_2장_확정규칙.md`).
"""
from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger(__name__)

__all__ = ["select_constituents", "classify", "adapt_columns", "validate",
           "CORE_TH", "SAT_MEM_TH"]

# ----- 규칙 파라미터 --------------------------------------------------------- #
CORE_TH = 0.30      # 규칙 A: 핵심군 편입 HBM 매출 노출도 하한
SAT_MEM_TH = 0.70   # 규칙 C①: 위성군 메모리 반도체향 매출 비중 하한
# (고정 정원 N_CORE=7 / N_SAT=3 은 2026-07-22 폐지 - 규칙 통과자 전원 편입)

GROUPS = ("앵커", "핵심", "위성")
UNSELECTED = "미편입"

# 표준 컬럼명. universe.py 산출물이 다른 이름을 쓰면 adapt_columns()로 맞춘다.
REQUIRED_COLS = ("종목명", "코드", "유형", "유동시총")
RULE_COLS = ("HBM양산", "HBM노출도", "메모리향비중", "HBM공정확인", "위원회확인")

_BOOL_COLS = ("HBM양산", "HBM공정확인", "위원회확인")
_NUM_COLS = ("HBM노출도", "메모리향비중", "유동시총")


# --------------------------------------------------------------------------- #
# 0. 입력 정규화
# --------------------------------------------------------------------------- #
def adapt_columns(df: pd.DataFrame, mapping: dict[str, str] | None = None) -> pd.DataFrame:
    """외부 컬럼명 → 본 모듈 표준 컬럼명.

    `universe.py` 산출물의 컬럼명이 확정되면 **이 함수 호출부의 mapping만** 바꾸면 되고,
    아래 판정 로직은 손대지 않는다.

    >>> adapt_columns(df, {"float_mcap": "유동시총", "exposure": "HBM노출도"})
    """
    return df.rename(columns=mapping) if mapping else df.copy()


def _truthy(v) -> bool:
    """Boolean 값 해석. 결측/미상은 False(보수적)로 본다.

    문자열뿐 아니라 **숫자형(1.0/0.0)**도 처리한다. pandas가 CSV의 불리언 열을
    float로 추론하면(같은 열에 결측이 하나라도 있으면 1/0 이 1.0/0.0 이 된다)
    과거 구현은 str(1.0)='1.0' 이 참 집합에 없어 True 를 False 로 뒤집었다 -
    HBM양산=1.0 인 앵커(삼성·SK하이닉스)가 규칙0에서 조용히 탈락하는 사고다.
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v) if v == v else False        # v==v 로 NaN 제외
    s = str(v).strip().lower()
    if s in ("true", "1", "y", "yes", "참"):
        return True
    if s in ("false", "0", "n", "no", "거짓", "", "nan", "none"):
        return False
    try:                                            # "1.0" · "0.0" 등 숫자 문자열
        return bool(float(s))
    except ValueError:
        return False


def _coerce(df: pd.DataFrame) -> pd.DataFrame:
    """dtype 정리 - 숫자는 float(결측 0), 불리언은 bool(결측 False)."""
    d = df.copy()
    for c in _NUM_COLS:
        if c in d.columns:
            d[c] = pd.to_numeric(d[c], errors="coerce").fillna(0.0)
    for c in _BOOL_COLS:
        if c in d.columns:
            d[c] = d[c].map(_truthy).astype(bool)
    return d


def validate(df: pd.DataFrame) -> None:
    """필수 컬럼 존재 확인. 없으면 즉시 실패시킨다(조용히 0으로 넘기지 않는다)."""
    missing = [c for c in REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"필수 컬럼 누락: {missing}  (adapt_columns()로 컬럼명을 맞추세요)")
    absent = [c for c in RULE_COLS if c not in df.columns]
    if absent:
        logger.warning("판정 컬럼 없음 → 해당 요건 미충족 처리: %s", absent)


# --------------------------------------------------------------------------- #
# 1. 규칙 0 / A / C 분류
# --------------------------------------------------------------------------- #
def classify_row(row) -> str:
    """규칙 0 > A > C 순차 적용. 한 종목은 한 군에만 속한다."""
    # 규칙 0 - 앵커군: 메모리 반도체 제조사 **이면서** HBM을 실제 양산·공급.
    #   공식본 "HBM을 실제 양산·공급하는 메모리 반도체 제조사"는 두 조건의 **결합 요건**이다.
    #   둘 중 하나만 보면 HBM을 양산하지 않는 메모리사(예: NAND 전용)까지 필수 편입된다.
    #   매출 노출도 기준의 예외이므로 노출 비율은 보지 않는다.
    if row.get("유형") == "메모리제조" and bool(row.get("HBM양산", False)):
        return "앵커"

    # 규칙 A - 핵심군: HBM 밸류체인 매출 노출도 30% 이상 (정공법)
    if float(row.get("HBM노출도", 0) or 0) >= CORE_TH:
        return "핵심"

    # 규칙 C - 위성군: 세 요건을 **모두** 충족할 때만 편입
    #   ① 메모리 반도체향 매출 비중 70% 이상
    #   ② HBM 고유공정(TSV·하이브리드본딩 등) 귀속 매출을 문서로 확인
    #   ③ 지수위원회 확인 절차 통과
    #      [TEAM] 위원회는 ①②의 **문서 충족 여부만** 확인하며 선정·배제 재량은 갖지 않는다.
    if (float(row.get("메모리향비중", 0) or 0) >= SAT_MEM_TH
            and bool(row.get("HBM공정확인", False))
            and bool(row.get("위원회확인", False))):
        return "위성"

    return UNSELECTED


def classify(df: pd.DataFrame) -> pd.DataFrame:
    """후보 전체에 규칙 0/A/C 적용 → `군` 컬럼 추가."""
    d = df.copy()
    d["군"] = d.apply(classify_row, axis=1)

    # 규칙 0이 결합 요건이므로 `HBM양산`이 비어 있으면 메모리제조사가 조용히 탈락한다.
    # 지수 전체가 뒤틀리는 사고이므로 침묵하지 않는다.
    dropped = d[(d["유형"] == "메모리제조") & (d["군"] != "앵커")]
    if not dropped.empty:
        logger.warning("규칙 0 미충족(메모리제조이나 HBM 양산·공급 미확인) - 앵커 제외: %s",
                       ", ".join(dropped["종목명"].astype(str)))
    return d


# --------------------------------------------------------------------------- #
# 2. 군별 순위 → 정원 선정
# --------------------------------------------------------------------------- #
def rank_key(group: str) -> list[str]:
    """군별 정렬 기준(1차 → 2차). 모두 내림차순.

    · 앵커군 - 전원 필수 편입이므로 순위 불요(유동시총순은 표시 목적).
    · 핵심군 - 공식본대로 **매출 노출도 1차**, 유동시가총액 2차.
    · 위성군 [TEAM] - **HBM관여점수 = 메모리향비중 × HBM노출도** 1차, 유동시가총액 2차.
        공식본은 위성군도 '노출도 1차'로 규정하나, 위성군의 정의 자체가
        "HBM 귀속 매출 **분리가 어려운**" 기업이다. 분리가 안 되는 값 하나로
        순위를 매기는 것은 내부 모순이므로, 문서로 확인 가능한 메모리향 비중과
        곱해 결합한다(둘 다 높아야 상위).
        ※ 정원 폐지 후에도 순위는 유지한다 - 구성표의 표시 순서이자,
          변경 규정(버퍼·대체)에서 '차순위'를 가리키는 기준이 되기 때문.
    """
    return {
        "앵커": ["유동시총"],
        "핵심": ["HBM노출도", "유동시총"],
        "위성": ["HBM관여점수", "유동시총"],
    }[group]


def select_constituents(df: pd.DataFrame,
                        mapping: dict[str, str] | None = None) -> pd.DataFrame:
    """후보 → 편입 구성(군 포함). 본 모듈의 공개 진입점.

    규칙 통과자 **전원**을 편입한다(고정 정원 없음). 종목 수는 시장 상황에 따라
    변하며, 그것이 의도된 동작이다 - 규칙이 종목 수를 정한다.

    Parameters
    ----------
    df : universe.py 통과 후보. 사전 스크린은 이미 끝난 것으로 전제한다.
    mapping : 외부 컬럼명 → 표준 컬럼명 (없으면 그대로 사용)

    Returns
    -------
    앵커 → 핵심 → 위성 순으로 정렬된 편입 종목 DataFrame (`군` 컬럼 포함)
    """
    d = _coerce(adapt_columns(df, mapping))
    validate(d)
    d = classify(d)

    # [TEAM] 위성군 1차 정렬키. 곱셈이므로 메모리향·노출도가 **둘 다** 높아야 상위.
    d["HBM관여점수"] = d.get("메모리향비중", 0.0) * d.get("HBM노출도", 0.0)

    picked = [d[d["군"] == g].sort_values(rank_key(g), ascending=False)
              for g in GROUPS]
    out = pd.concat(picked, ignore_index=True)

    n = {g: int((out["군"] == g).sum()) for g in GROUPS}
    logger.info("구성: 앵커 %d + 핵심 %d + 위성 %d = 총 %d종목 (규칙 기반 가변)",
                n["앵커"], n["핵심"], n["위성"], len(out))
    # 군이 비는 것은 지수 성립에 영향을 주는 사안이므로 침묵하지 않는다.
    for g in GROUPS:
        if n[g] == 0:
            logger.warning("%s군 편입 종목이 없음 - 비중 배분 규칙의 퇴화 조항이 적용됨", g)
    return out
