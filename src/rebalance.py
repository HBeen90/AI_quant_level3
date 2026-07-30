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
       -> 5종목 미만이어도 산출 지속 + 긴급 재심사(안건 3 확정).
          전 종목 편출만 산출 불가.

귀속(발표 시 명확히)
--------------------
  히스테리시스 임계값 후보·정책 비교(버퍼룰=회전율 관리)는 이 모듈(소연 파트).
  비중 산정(40/60·상한·재배분 폭포·희소/퇴화)은 src/weighting.py(민수님,
  2.3.x)의 술식이며, 본 모듈의 assign_weights_v2 는 그에 대한 '위임
  어댑터'다(라벨/자료형 변환 + 합계 100% 통합 검증만 수행 - 과거의 잠정
  자체 구현은 weighting 확정과 함께 삭제됨).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import warnings

import numpy as np
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
    """산출이 불가능한 예외 상황(예: 전 종목 편출) - 조용히 계속 산출하지
    않는다. 구성 5 미만은 중단 사유가 아니라 긴급 재심사 트리거다(안건 3
    확정 - 플래그·경고로 처리)."""


class InfeasibleComposition(Exception):
    """구조적으로 비중 산정이 불가능한 구성(앵커 부재·비앵커 전무 등 퇴화).
    앵커 1종목 등 희소 상황은 weighting의 '합계 100% 최우선 + 경고'로
    산출을 지속한다(팀 확인 4번 해소) - 본 예외는 진짜 퇴화에만 쓴다."""


@dataclass(frozen=True)
class ConfigV2:
    entry_core: float = 0.30           # 규칙 A 신규 편입 임계값
    entry_sat: float = 0.70            # 규칙 C① 신규 편입 임계값
    hold_core: float = 0.27            # 기존 유지 임계값 (정책으로 교체)
    hold_sat: float = 0.67
    min_constituents: int = 5          # 공식본 수시변경 ② - 하한(긴급심사 트리거)
    # 안건 3(2026-07-23 확정): 하한 미달 시 산출 지속 + 긴급 재심사.
    # 60영업일은 외부 표준이 아닌 '팀 운영안' - 종료 결정일·통지기간·최종
    # 산출일은 7.3 이관 시 위원회가 결정론적으로 확정(엔진 범위 밖).
    emergency_window_days: int = 60    # 미해소 허용 거래일(팀 운영안)
    anchor_total: float = 0.40
    anchor_ind_cap: float = 0.25       # 2.3.3 (v2에서도 유지)
    core_ind_cap: float = 0.18         # 비앵커버킷 30% x 0.60
    sat_ind_cap: float = 0.15          # 비앵커버킷 25% x 0.60
    sat_total_cap: float = 0.18
    rulebook_version: str = "v2.2+suspension-merger(hold 27/67 operating)"

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
            "dropped": prev_members - new, "n": len(members),
            "buffer_binding": buffer_binding(c, prev_members, cfg)}


# ----------------------------------------------------------------------
# 버퍼 발동 진단 - '유지 임계값이 실제로 일을 했는가'를 계량한다
# ----------------------------------------------------------------------
def buffer_binding(candidates: pd.DataFrame, prev_members: set,
                   cfg: ConfigV2 = ConfigV2()) -> pd.DataFrame:
    """유지 임계값(버퍼) **덕분에** 편출을 면한 기존 종목을 식별한다.

    왜 필요한가
    -----------
    정책 비교표(none/narrow/mid/wide)가 네 행 모두 같은 숫자를 내면 두 가지
    해석이 가능하다 - (a) 코드가 정책을 안 읽는 버그, (b) 표본 안에서 버퍼가
    한 번도 발동하지 않은 정상 동작. 표만 봐서는 구분되지 않는다.
    이 함수는 발동 건수를 **직접 세어** 둘을 갈라준다. 발동 0건이 명시되면
    "동일 결과"는 버그가 아니라 표본의 성질이라는 증거가 된다.

    판정
    ----
    기존 종목(prev_members) 중 비앵커만 대상. 앵커는 규칙0(유형·양산)으로
    판정하므로 임계값과 무관하다.
        핵심: value = exposure,  entry = entry_core,  hold = hold_core
        위성: value = mem_ratio, entry = entry_sat,   hold = hold_sat
        binding = (value < entry) AND (value >= hold)
    즉 '신규였다면 못 들어왔을 값인데 기존이라 남았다'는 관측만 True.

    반환 컬럼: ticker · name · group · metric · value · entry_th · hold_th ·
              binding · in_band(정책 무관하게 버퍼가 물릴 수 있는 값의 존재)
    hold == entry 인 정책('none')에서는 binding 이 항상 False다(버퍼 없음).
    """
    c = candidates.copy()
    if "eligible" in c.columns:
        c = c[c["eligible"].astype(bool)]          # 하드 탈락은 버퍼가 못 구한다
    c = c[c["ticker"].isin(prev_members) & c["group"].isin([CORE, SAT])]
    if c.empty:
        return pd.DataFrame(columns=["ticker", "name", "group", "metric",
                                     "value", "entry_th", "hold_th",
                                     "binding", "in_band"])
    is_core = c["group"] == CORE
    value = pd.to_numeric(c["exposure"], errors="coerce").where(
        is_core, pd.to_numeric(c["mem_ratio"], errors="coerce"))
    entry = pd.Series(cfg.entry_sat, index=c.index).mask(is_core, cfg.entry_core)
    hold = pd.Series(cfg.hold_sat, index=c.index).mask(is_core, cfg.hold_core)
    # in_band 는 '정책 최대폭(wide)'이 아니라 현재 cfg 기준 - 정책별 비교가 목적.
    out = pd.DataFrame({
        "ticker": c["ticker"].to_numpy(),
        "name": c["name"].to_numpy() if "name" in c.columns else "",
        "group": c["group"].to_numpy(),
        "metric": np.where(is_core.to_numpy(), "exposure", "mem_ratio"),
        "value": value.to_numpy(dtype=float),
        "entry_th": entry.to_numpy(dtype=float),
        "hold_th": hold.to_numpy(dtype=float),
    })
    out["binding"] = ((out["value"] < out["entry_th"] - 1e-12)
                      & (out["value"] >= out["hold_th"] - 1e-12))
    out["in_band"] = out["value"].between(
        out["hold_th"] - 1e-12, out["entry_th"] - 1e-12)
    return out.sort_values("ticker").reset_index(drop=True)


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
    if len(out) < cfg.min_constituents:               # 안건 3: 중단 대신 경고
        warnings.warn(f"편입 {len(out)}종목 < 하한 {cfg.min_constituents} - "
                      "산출 지속·긴급 재심사 개시(안건 3)")
    return out


def regular_rebalance_v2(candidates: pd.DataFrame, prev_members: set,
                         cfg: ConfigV2 = ConfigV2()) -> dict:
    # 안건 3(확정): 하한 미달이어도 산출을 중단하지 않는다 - 잔여(자격자)로
    # 산출을 지속하고 under_min 플래그로 긴급 재심사 개시를 알린다.
    # (앵커 부재·비앵커 전무 등 퇴화는 여전히 weighting에서 산출 거부)
    sel = select_v2(candidates, prev_members, cfg)
    under = sel["n"] < cfg.min_constituents
    if under:
        warnings.warn(f"구성 {sel['n']}종목 < 하한 {cfg.min_constituents} - "
                      "산출 지속·긴급 재심사 개시(안건 3)")
    return {**sel, "under_min": under,
            "weights": assign_weights_v2(sel["members"], cfg)}


# ----------------------------------------------------------------------
# 수시변경 - 무대체 운영
# ----------------------------------------------------------------------
@dataclass
class AdhocManagerV2:
    """v2 수시변경 규칙 계층: 편출 -> 대체 없음 -> 잔여 정규화(제수 흡수 동치).
    5종목 미만이면 산출을 지속하며 under_min_emergency_review 플래그를 세운다
    (안건 3 확정 - 중단은 운용 사고이므로 긴급 재심사·A+2 편입으로 회복).
    예비·충원·재선정 없음(긴급 편입은 스케줄러가 별도 절차로 집행).
    주의: 이 클래스는 '집행'만 담당한다. 공지일 -> D+2 거래일의 시점 관리는
    backtest.build_event_schedule 이 수행하며 tests/test_schedule_v2.py 로
    검증된다(규칙/시점 계층 분리 - v1과 동일 구조)."""
    weights: pd.Series
    groups: pd.Series
    cfg: ConfigV2 = field(default_factory=ConfigV2)
    log: list = field(default_factory=list)
    flags: set = field(default_factory=set)

    def drift(self, day_returns: pd.Series) -> None:
        """활성 구성종목의 수익률 결측·비유한(inf)은 0%로 조용히 대체하지 않는다
        (fail-closed, 리뷰 반영) - 데이터 문제를 예외로 표면화한다.

        NaN 뿐 아니라 **inf 도 막는다**: 0원 가격에서 복구된 날의 pct_change 는
        inf 를 내고, 그대로 곱하면 weights 가 inf -> NaN 으로 조용히 오염돼
        캡·회전율 판정 전체가 무너진다(실패한 티도 안 난다). simulate_index 가
        px<=0 을 거부하는 것과 동일한 규율을 이 병렬 추적 경로에도 건다."""
        r = day_returns.reindex(self.weights.index)
        inf = float("inf")
        bad = r.isna() | r.eq(inf) | r.eq(-inf)
        if bad.any():
            raise ValueError(
                f"활성 구성종목 수익률 결측·비유한: {r.index[bad].tolist()} - "
                "0% 대체 금지, 가격 데이터 확인 필요(inf 는 0원 가격 복구 등)")
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
        if len(self.weights) == 0:
            raise MethodologyReviewRequired("전 종목 편출 - 산출 불가")
        self.weights = self.weights / self.weights.sum()   # 1회 정규화
        # 안건 3(확정): 하한 미달은 중단 사유가 아니라 긴급 재심사 트리거다.
        if len(self.weights) < self.cfg.min_constituents:
            self.flags.add("under_min_emergency_review")
            warnings.warn(f"편출 후 {len(self.weights)}종목 < "
                          f"{self.cfg.min_constituents} - 산출 지속·긴급 재심사")
        else:
            self.flags.discard("under_min_emergency_review")


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
