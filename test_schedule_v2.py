# -*- coding: utf-8 -*-
"""
src/rebalance.py - HBM 지수 공식 v2: 정원 폐지 · 히스테리시스 버퍼 · 무대체 수시변경
=====================================================================================
2026-07-23 팀 승인으로 v2가 공식 방법론이 되었다. v1 검증 동결본은 legacy/v1.

v1 대비 변경 요약
-----------------
  quota 삭제              -> min_constituents=5 하한만 유지 (공식본 수시변경 ② 준용)
  순위 버퍼(정원 1.5배)    -> 임계값 히스테리시스
       핵심 신규: exposure >= 0.30      핵심 기존: exposure >= hold_core
       위성 신규: mem_ratio >= 0.70     위성 기존: mem_ratio >= hold_sat
  하드 탈락(eligible=False: 사전 스크린·자료 누락·공정요건 상실)은 버퍼에 우선한다.
  예비종목·차순위 충원·결원 트리거·9 미만 재선정 -> 전부 삭제.
       수시편출 발생 -> D+2 편출 -> 대체 없음 -> 잔여로 운영
       -> 전체 5종목 미만 시 MethodologyReviewRequired 로 산출 중단.

귀속(발표 시 명확히)
--------------------
  히스테리시스 임계값 후보·정책 비교(버퍼룰=회전율 관리)는 이 모듈(소연 파트).
  assign_weights_v2 의 재배분 폭포·희소/퇴화 조항은 민수님 v2 2.3.4의 술식이며,
  백테스트 구동을 위한 '잠정 구현'이다. 민수님 weighting.py 확정 시 그쪽 산출물
  (target_weights)로 교체하고, 본 모듈은 합계 100%·결정론·회전율 검증만 남긴다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import warnings

import pandas as pd

ANCHOR, CORE, SAT = "anchor", "core", "satellite"

#: 버퍼 정책: 신규 임계값은 규칙 그대로(핵심 30% / 위성 70%), 유지 임계값만 완화.
BUFFER_POLICIES = {
    "none":   {"hold_core": 0.30, "hold_sat": 0.70},   # 버퍼 없음
    "narrow": {"hold_core": 0.29, "hold_sat": 0.69},   # 좁은 버퍼
    "mid":    {"hold_core": 0.27, "hold_sat": 0.67},   # 중간 버퍼
    "wide":   {"hold_core": 0.25, "hold_sat": 0.65},   # 넓은 버퍼
}


class MethodologyReviewRequired(Exception):
    """구성종목 5 미만 등 방법론 재심사 사유 - 조용히 계속 산출하지 않는다."""


class InfeasibleComposition(Exception):
    """구조적으로 비중 제약을 충족할 수 없는 구성(예: 앵커 1종목 40%/25% 충돌).
    팀 확인 4번 안건이 해소되기 전에는 중단이 정답이다."""


@dataclass(frozen=True)
class ConfigV2:
    entry_core: float = 0.30           # 규칙 A 신규 편입 임계값
    entry_sat: float = 0.70            # 규칙 C① 신규 편입 임계값
    hold_core: float = 0.27            # 기존 유지 임계값 (정책으로 교체)
    hold_sat: float = 0.67
    min_constituents: int = 5          # 공식본 수시변경 ② - 유일한 종목 수 규정
    anchor_total: float = 0.40
    anchor_ind_cap: float = 0.25       # 2.3.3 (v2에서도 유지)
    core_ind_cap: float = 0.18         # 비앵커버킷 30% x 0.60
    sat_ind_cap: float = 0.15          # 비앵커버킷 25% x 0.60
    sat_total_cap: float = 0.18
    rulebook_version: str = "v2.0+hysteresis(hold 27/67 provisional)"

    @staticmethod
    def with_policy(name: str) -> "ConfigV2":
        p = BUFFER_POLICIES[name]
        return ConfigV2(hold_core=p["hold_core"], hold_sat=p["hold_sat"])


# ----------------------------------------------------------------------
# 데이터 계약 - 종목코드는 반드시 문자열
# ----------------------------------------------------------------------
REQUIRED_COLS = {"ticker", "name", "group", "exposure", "mem_ratio",
                 "float_mcap", "eligible"}


def load_snapshot_csv(path: str) -> pd.DataFrame:
    """심사 스냅샷 CSV 로더. 종목코드는 반드시 문자열로 읽는다 - dtype 지정을
    안 하면 판다스가 숫자로 인식해 '005930'의 앞자리 0이 '5930'으로 사라진다."""
    df = pd.read_csv(path, dtype={"ticker": str, "코드": str})
    df = df.rename(columns={"코드": "ticker", "종목명": "name",
                            "bucket": "group", "ff_market_cap": "float_mcap"})
    df["ticker"] = df["ticker"].str.strip().str.zfill(6)
    return validate_snapshot(df)


def validate_snapshot(df: pd.DataFrame) -> pd.DataFrame:
    missing = REQUIRED_COLS - set(df.columns)
    if missing:
        raise ValueError(f"심사 스냅샷 필수 컬럼 누락: {sorted(missing)}")
    if not pd.api.types.is_string_dtype(df["ticker"]):
        raise ValueError("ticker는 문자열이어야 함 (dtype=str 로 읽을 것)")
    if (df["ticker"].str.len() != 6).any():
        bad = df.loc[df["ticker"].str.len() != 6, "ticker"].tolist()
        raise ValueError(f"6자리 종목코드 위반: {bad}")
    if df["ticker"].duplicated().any():
        raise ValueError("티커 중복")
    if not pd.api.types.is_bool_dtype(df["eligible"]):
        raise ValueError("eligible은 엄격한 Boolean이어야 함")
    bad_grp = set(df["group"].dropna()) - {ANCHOR, CORE, SAT}
    if bad_grp:
        raise ValueError(f"허용하지 않는 group: {sorted(bad_grp)}")
    return df


# ----------------------------------------------------------------------
# 선정 - 임계값 히스테리시스 (v2 버퍼룰)
# ----------------------------------------------------------------------
def select_v2(candidates: pd.DataFrame, prev_members: set,
              cfg: ConfigV2 = ConfigV2()) -> dict:
    """규칙 통과자 전원 편입(정원 없음). 종목 수는 규칙이 정한다.

    판정 순서 (하드 탈락이 버퍼보다 우선):
      1) eligible=False (사전 스크린 탈락·자료 누락·HBM 공정요건 상실) -> 무조건 제외
      2) 앵커: 자격 충족 시 전원 편입
      3) 핵심: 신규 exposure >= entry_core / 기존 exposure >= hold_core
      4) 위성: 신규 mem_ratio >= entry_sat / 기존 mem_ratio >= hold_sat
         (exposure는 관여점수 표시 순위용 - 편입 판정 조건 아님)
    동점·표시 순서는 종목코드 오름차순 고정(결정론).
    """
    c = validate_snapshot(candidates).copy()
    c = c[c["eligible"]]                                   # 1) 하드 탈락 우선
    c["exposure"] = pd.to_numeric(c["exposure"], errors="coerce")
    c["mem_ratio"] = pd.to_numeric(c["mem_ratio"], errors="coerce")
    c["float_mcap"] = pd.to_numeric(c["float_mcap"], errors="coerce")
    bad = c["ticker"][c["float_mcap"].isna() | (c["float_mcap"] <= 0)].tolist()
    if bad:
        raise ValueError(f"적격 후보 유동시총 결측·비양수: {bad}")

    incumbent = c["ticker"].isin(prev_members)

    keep_anchor = c["group"] == ANCHOR
    keep_core = (c["group"] == CORE) & (
        (~incumbent & (c["exposure"] >= cfg.entry_core - 1e-12)) |
        (incumbent & (c["exposure"] >= cfg.hold_core - 1e-12)))
    # 위성 편입 요건은 메모리향 비중 + 공정 문서 + 위원회 확인(eligible)뿐이다.
    # exposure는 관여점수(표시 순위) 계산에만 쓰며 편입 판정 조건이 아니다
    # (리뷰 반영: 문서에 없는 exposure>0 조건 제거).
    keep_sat = (c["group"] == SAT) & (
        (~incumbent & (c["mem_ratio"] >= cfg.entry_sat - 1e-12)) |
        (incumbent & (c["mem_ratio"] >= cfg.hold_sat - 1e-12)))

    members = c[keep_anchor | keep_core | keep_sat] \
        .sort_values("ticker").reset_index(drop=True)
    new = set(members["ticker"])
    return {"members": members, "added": new - prev_members,
            "dropped": prev_members - new, "n": len(members)}


# ----------------------------------------------------------------------
# 비중 - weighting.py(노민수, 2.3.x) 위임 어댑터
# ----------------------------------------------------------------------
# 귀속 원칙: 40/60 배분·개별/합계 상한·재배분 폭포·희소/퇴화 처리는 전부
# src/weighting.py(민수님)의 술식이다. 본 함수는 (a) 라벨/자료형 변환,
# (b) 산출 결과의 통합 검증(합계 100%·상한·결정론)만 수행한다.
# 과거의 잠정 구현(자체 폭포)은 weighting.py 확정에 따라 삭제했다.
# 앵커 1종목 등 희소·퇴화 상황은 weighting의 '합계 100% 최우선 + 경고'
# 동작을 그대로 따른다(팀 확인 4번 처리 방식).
try:
    from src import weighting as _weighting
except ImportError:                      # 단독 실행·테스트 경로 호환
    import weighting as _weighting

_GRP_KR = {ANCHOR: "앵커", CORE: "핵심", SAT: "위성"}


def assign_weights_v2(members: pd.DataFrame, cfg: ConfigV2 = ConfigV2(),
                      cap_basis: str = "bucket") -> pd.Series:
    """members(ticker/group/float_mcap) -> weighting.allocate 위임 -> 검증."""
    m = members.set_index("ticker")
    groups_kr = m["group"].map(_GRP_KR)
    if groups_kr.isna().any():
        raise InfeasibleComposition(
            f"허용하지 않는 group: {sorted(set(m['group']) - set(_GRP_KR))}")
    fmc = pd.to_numeric(m["float_mcap"], errors="coerce")
    if fmc.isna().any() or (fmc <= 0).any():
        raise InfeasibleComposition(
            f"유동시총 결측·비양수: {fmc.index[fmc.isna() | (fmc <= 0)].tolist()}")
    try:
        w = _weighting.allocate(groups_kr.to_numpy(), fmc.to_numpy(dtype=float),
                                cap_basis=cap_basis)
    except ValueError as e:              # 퇴화(앵커·비앵커 부재)는 산출 거부로 전파
        raise InfeasibleComposition(str(e)) from e
    out = pd.Series(w, index=m.index, dtype=float)
    if abs(float(out.sum()) - 1.0) > 1e-9:                     # 통합 검증 (9)
        raise InfeasibleComposition(f"합계 {out.sum():.6f} != 1 - weighting 산출 오류")
    return out


def selection_hold_group(row, hold_core: float, hold_sat: float) -> str:
    """selection.classify_row(민수님 규칙 0>A>C)와 동일 구조에 '유지 임계값'만
    치환한 판정. 하드 요건(유형·HBM양산·공정확인·위원회확인)은 완화하지 않는다.
    hold=entry 로 두면 selection.classify_row 와 동일해야 하며, 그 일치성은
    tests/test_develop_integration.py 가 검증한다."""
    if row.get("유형") == "메모리제조" and bool(row.get("HBM양산", False)):
        return "앵커"
    if float(row.get("HBM노출도", 0) or 0) >= hold_core - 1e-12:
        return "핵심"
    if (float(row.get("메모리향비중", 0) or 0) >= hold_sat - 1e-12
            and bool(row.get("HBM공정확인", False))
            and bool(row.get("위원회확인", False))):
        return "위성"
    return "미편입"


def select_from_selection(df_kr: pd.DataFrame, prev_members: set,
                          cfg: ConfigV2 = ConfigV2()) -> pd.DataFrame:
    """selection.py 표준(한글) 스냅샷에 히스테리시스 버퍼를 적용해 편입 확정.

    신규 후보: selection 판정 그대로(신규 임계값 30%/70%).
    기존 종목: 유지 임계값(hold_core/hold_sat)으로 완화 판정.
    하드 탈락(사전 스크린·공정요건 상실 등)은 유지 임계값과 무관 - 판정식에서
    하드 요건이 완화되지 않으므로 자동으로 우선한다.
    반환: `군` 확정된 편입 구성표(미편입 제외, 코드 오름차순 - 결정론).
    """
    d = df_kr.copy()
    d["코드"] = d["코드"].astype(str).str.zfill(6)
    is_inc = d["코드"].isin(prev_members)
    entry = d.apply(lambda r: selection_hold_group(
        r, cfg.entry_core, cfg.entry_sat), axis=1)
    hold = d.apply(lambda r: selection_hold_group(
        r, cfg.hold_core, cfg.hold_sat), axis=1)
    d["군"] = entry.where(~is_inc, hold)
    out = d[d["군"] != "미편입"].sort_values("코드").reset_index(drop=True)
    if len(out) < cfg.min_constituents:
        raise MethodologyReviewRequired(
            f"편입 {len(out)}종목 < 하한 {cfg.min_constituents} - 방법론 재심사")
    return out


def regular_rebalance_v2(candidates: pd.DataFrame, prev_members: set,
                         cfg: ConfigV2 = ConfigV2()) -> dict:
    sel = select_v2(candidates, prev_members, cfg)
    if sel["n"] < cfg.min_constituents:
        raise MethodologyReviewRequired(
            f"정기변경 구성 {sel['n']}종목 < 하한 {cfg.min_constituents} - 재심사")
    return {**sel, "weights": assign_weights_v2(sel["members"], cfg)}


# ----------------------------------------------------------------------
# 수시변경 - 무대체 운영
# ----------------------------------------------------------------------
@dataclass
class AdhocManagerV2:
    """v2 수시변경 규칙 계층: 편출 -> 대체 없음 -> 잔여 정규화(제수 흡수 동치)
    -> 5종목 미만이면 MethodologyReviewRequired. 예비·충원·재선정 없음.
    주의: 이 클래스는 '집행'만 담당한다. 공지일 -> D+2 거래일의 시점 관리는
    backtest.build_event_schedule 이 수행하며 tests/test_schedule_v2.py 로
    검증된다(규칙/시점 계층 분리 - v1과 동일 구조)."""
    weights: pd.Series
    groups: pd.Series
    cfg: ConfigV2 = field(default_factory=ConfigV2)
    log: list = field(default_factory=list)

    def drift(self, day_returns: pd.Series) -> None:
        """활성 구성종목의 수익률 결측은 0%로 조용히 대체하지 않는다(fail-closed,
        리뷰 반영) - 결측이면 예외를 발생시켜 데이터 문제를 표면화한다."""
        r = day_returns.reindex(self.weights.index)
        if r.isna().any():
            raise ValueError(
                f"활성 구성종목 수익률 결측: {r.index[r.isna()].tolist()} - "
                "0% 대체 금지, 가격 데이터 확인 필요")
        self.weights = self.weights * (1.0 + r)
        self.weights = self.weights / self.weights.sum()

    def apply_exclusions(self, batch: list) -> None:
        """동일 종가 일괄(원자적·순서 무관). batch=[(ticker, reason), ...]"""
        pre = self.weights.copy()
        for ticker, reason in batch:
            self.log.append(("exclude", ticker, str(self.groups.get(ticker)),
                             float(pre.get(ticker, float("nan"))), reason))
            self.weights = self.weights.drop(ticker)
            self.groups = self.groups.drop(ticker)
        if len(self.weights) < self.cfg.min_constituents:
            raise MethodologyReviewRequired(
                f"수시편출 후 {len(self.weights)}종목 < "
                f"{self.cfg.min_constituents} - 산출 중단·방법론 재심사")
        self.weights = self.weights / self.weights.sum()   # 1회 정규화


# ----------------------------------------------------------------------
# 운영 월간 캡 (README 3장 수시변경 ① - v2에서도 유지)
# ----------------------------------------------------------------------
def cap_algorithm(w: pd.Series, trigger: float = 0.30,
                  target: float = 0.25) -> pd.Series:
    """월말 점검: 30% 초과 종목을 25%로 캡, 초과분은 비캡 종목에 비례 배분.
    동시 식별 -> 일괄 캡 -> 재검증 반복(결정론·입력 순서 무관). v1과 동일 술식."""
    w = w.astype(float).copy()
    capped: set = set()
    for _ in range(len(w)):
        over = w[(w > trigger + 1e-12) & (~w.index.isin(capped))]
        if over.empty:
            break
        capped |= set(over.index)
        excess = float((w[list(capped)] - target).clip(lower=0).sum())
        w[list(capped)] = target
        free = w.index.difference(list(capped))
        if len(free) == 0:
            break
        w[free] += excess * w[free] / w[free].sum()
    return w


def monitor(w_drifted: pd.Series, cfg: ConfigV2 = ConfigV2()) -> tuple:
    """월말 점검 1회 - 계산만 하고 즉시 적용하지 않는다(D+2 집행은 backtest 계층)."""
    w1 = cap_algorithm(w_drifted, 0.30, 0.25)
    w1 = w1 / w1.sum()
    return w1, bool((w1 - w_drifted).abs().max() > 1e-9)
