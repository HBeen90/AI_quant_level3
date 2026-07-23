"""
HBM 테마 커스텀 인덱스 — 2.3 편입 비중 산정 · 캡(IIF) 계산  (담당: 노민수)

역할: `selection.py`가 확정한 편입 종목(규칙 통과자 전원, 수는 가변)을 받아
      **앵커 40% / 비앵커 60%**를 유동시가총액에
      비례 배분하고, 실링·위성군 합계 상한을 **수렴할 때까지** 적용한 뒤
      **IIF(i)**를 산출해 `index_calc.py`(김인서)에 넘긴다.

파이프라인에서의 위치
    universe.py  →  selection.py  →  **weighting.py**  →  index_calc.py
    (임효빈)         (노민수)          (노민수)             (김인서)

⭐ 산출물은 '편입비중(%)'이 아니라 **IIF(i)**다.
   팀 README 지수 산식:  M(t) = Σ IIF(i) × FF(i) × S(i,t) × P(i,t)
   비중은 리밸런싱 시점의 스냅샷이라 매일 주가가 움직이면 어긋난다. 반면 IIF는
   캡을 반영한 **고정 계수**이므로 divisor 방식과 정합한다. 편입비중은 검증용으로 함께 낸다.

입력 컬럼: 종목명, 코드, 군('앵커'|'핵심'|'위성'), 유동시총(= FF × S × P)
출력 컬럼: 입력 + 편입비중(0~1), 편입비중(%), **IIF**

사용 예 (index_calc.py 쪽)
-------------------------
    import selection, weighting

    sel = selection.select_constituents(universe_df, mapping=MAP)
    res = weighting.compute_weights(sel)          # cap_basis="bucket"(기본)

    issues = weighting.verify(res)
    if issues:
        raise RuntimeError(issues)                # 합계100·앵커40·상한·IIF 자동 점검

    iif = res.set_index("코드")["IIF"]             # ← 지수 산출에 쓰는 값
    # M(t) = Σ IIF(i) × FF(i) × S(i,t) × P(i,t)

IIF는 **정기변경 시점에 한 번 확정되는 고정 계수**다. 매일 바뀌는 것은 P(i,t)뿐이며,
IIF는 다음 정기변경까지 그대로 쓴다. (편입비중은 확정 시점 스냅샷이므로 지수 산출에 쓰지 말 것)

🟨 = 팀 공식본에 정의가 없어 확정한 항목 (근거는 `내파트_2장_확정규칙.md`).
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

__all__ = ["compute_weights", "verify", "allocate", "to_iif",
           "ANCHOR_W", "ANCHOR_CAP", "SAT_TOTAL_CAP",
           "CORE_CAP_BUCKET", "SAT_CAP_BUCKET"]

# ----- 규칙 파라미터 --------------------------------------------------------- #
ANCHOR_W = 0.40         # 앵커군 합계 비중(고정)
NON_ANCHOR_W = 1.0 - ANCHOR_W   # 비앵커 버킷 = 0.60
ANCHOR_CAP = 0.25       # 앵커 개별 상한 — 지수 전체 기준(공식본 명시)
SAT_TOTAL_CAP = 0.18    # 위성군 합계 상한 — 지수 전체 기준(공식본 "잔여 60%의 30%")

# 🟨 개별 실링의 **기준(분모)**은 공식본에 없다. 비앵커 버킷(60%) 대비로 확정.
#    근거: 지수 전체 기준으로 읽으면 위성군은 개별 상한 25% > 합계 상한 18%가 되어
#          어떤 데이터로도 발동할 수 없는 사문(死文)이 된다.
#          버킷 기준이면 25% × 60% = 지수 15% < 18%이므로 정상 작동한다.
CORE_CAP_BUCKET = 0.30  # 핵심군 개별 상한(버킷 대비) → 지수 18%p
SAT_CAP_BUCKET = 0.25   # 위성군 개별 상한(버킷 대비) → 지수 15%p

# 대조용 대안 해석(지수 전체 기준). 기본값은 위의 bucket 기준이며, 이 값들은
# `cap_basis="index"`로 호출할 때만 쓰인다 — 두 해석의 결과 차이를 보여줄 때 사용.
CORE_CAP_INDEX = 0.30
SAT_CAP_INDEX = 0.25

MAX_ITER = 100          # 수렴 반복 상한(무한루프 방지)
_TOL = 1e-12


# --------------------------------------------------------------------------- #
# 상한 적용 — 그룹 합계를 유지한 채 개별 상한까지 재배분
# --------------------------------------------------------------------------- #
def _cap_within(w: np.ndarray, mask: np.ndarray, cap: float):
    """mask 그룹 안에서 개별 상한(cap)을 반복 적용.

    초과분은 아직 상한에 닿지 않은 같은 그룹 종목에 **현재 비중(=유동시총) 비례**로 넘긴다.
    그룹 안에서 다 수용하지 못하면 **남은 초과분을 반환**한다 — 조용히 버리면
    합계가 100%에서 모자라게 되므로, 호출부의 재배분 폭포가 이어받는다.
    반환: (조정된 w, 그룹 내 수용 불가 초과분)
    """
    w = w.copy()
    for _ in range(MAX_ITER):
        over = mask & (w > cap + _TOL)
        if not over.any():
            return w, 0.0
        excess = (w[over] - cap).sum()
        w[over] = cap
        under = mask & (w < cap - _TOL)
        if w[under].sum() <= 0:
            return w, float(excess)          # 군 내 수용 불가 → 폭포로
        w[under] += excess * w[under] / w[under].sum()
    return w, 0.0


def allocate(groups: np.ndarray, fmc: np.ndarray,
             cap_basis: str = "bucket") -> np.ndarray:
    """40/60 배분 + 상한 수렴. 확정규칙 2.3.4의 ④~⑧.

    ④ 앵커 40% × 유동시총 비율 → 개별 25% 상한 재배분
    ⑤ 비앵커 60% × 유동시총 비율
    ⑥ 위성군 합계 18% 초과분 → 핵심군으로 이전
    ⑦ 개별 실링 재배분
    ⑧ ⑥~⑦을 변화가 없을 때까지 반복

    ⑧이 필수인 이유: ⑥에서 위성 초과분을 핵심군에 넘기면 핵심군 종목이 **새로** 상한에
    걸릴 수 있다. 한 번만 계산하면 상한을 넘긴 상태로 결과가 나온다.

    Parameters
    ----------
    cap_basis : "bucket"(확정 기준) | "index"(대조용 대안 해석)
    """
    A = groups == "앵커"
    C = groups == "핵심"
    S = groups == "위성"
    N = C | S
    w = np.zeros(len(groups), dtype=float)

    # 퇴화 상황 방어(정원 폐지로 군이 빌 수 있음): 조용히 넘기지 않는다.
    if not A.any():
        raise ValueError("앵커군(HBM 양산 제조사)이 없습니다 — 지수 구성 요건 미달. "
                         "테마의 양산 주체가 없으면 지수가 성립하지 않습니다.")
    if not N.any():
        raise ValueError("비앵커(핵심·위성) 편입 종목이 없어 60%를 배분할 수 없습니다 — "
                         "지수 구성 요건 미달. 정기변경에서 재검토가 필요합니다.")

    w[A] = ANCHOR_W * fmc[A] / fmc[A].sum()              # ④
    w, _ = _cap_within(w, A, ANCHOR_CAP)
    w[N] = NON_ANCHOR_W * fmc[N] / fmc[N].sum()          # ⑤

    if cap_basis == "bucket":
        core_cap, sat_cap = CORE_CAP_BUCKET * NON_ANCHOR_W, SAT_CAP_BUCKET * NON_ANCHOR_W
    elif cap_basis == "index":
        core_cap, sat_cap = CORE_CAP_INDEX, SAT_CAP_INDEX
    else:
        raise ValueError(f"cap_basis는 'bucket' 또는 'index' — 받은 값: {cap_basis!r}")

    for i in range(MAX_ITER):                            # ⑥~⑧
        before = w.copy()
        if S.any() and w[S].sum() > SAT_TOTAL_CAP + _TOL:
            excess = w[S].sum() - SAT_TOTAL_CAP
            w[S] *= SAT_TOTAL_CAP / w[S].sum()
            if C.any():
                w[C] += excess * fmc[C] / fmc[C].sum()
            else:
                w[A] += excess * fmc[A] / fmc[A].sum()
                logger.warning("핵심군 부재 — 위성 초과분 %.2f%%p 를 앵커군이 흡수", excess * 100)
        w, ex_c = _cap_within(w, C, core_cap)
        w, ex_s = _cap_within(w, S, sat_cap)

        # 재배분 폭포: 같은 군 → 다른 비앵커 군(여유 한도 내) → 앵커군.
        # 자격 종목이 소수면(예: 핵심 1개) 군 내 수용이 불가능한데, 초과분을
        # 조용히 버리면 합계가 100%에서 모자란다 — 반드시 어딘가가 흡수해야 한다.
        pool = ex_c + ex_s
        if pool > _TOL:
            if C.any():
                head = np.maximum(core_cap - w[C], 0.0)
                if head.sum() > _TOL:
                    give = min(pool, head.sum())
                    w[C] += give * head / head.sum()      # 초과되면 다음 반복이 회수
                    pool -= give
            if pool > _TOL and S.any():
                grp_head = max(SAT_TOTAL_CAP - w[S].sum(), 0.0)
                head = np.minimum(np.maximum(sat_cap - w[S], 0.0), grp_head)
                if grp_head > _TOL and head.sum() > _TOL:
                    give = min(pool, grp_head)
                    w[S] += give * head / head.sum()
                    pool -= give
            if pool > _TOL:
                # 희소 조항: 비앵커의 상한 수용량이 60%에 미달 → 잔여는 앵커군이
                # 흡수한다(합계 100% 최우선 — 이 경우 앵커 40%·개별 25% 초과 허용).
                w[A] += pool * fmc[A] / fmc[A].sum()
                logger.warning("희소 조항 발동 — 비앵커 상한 수용량 부족, "
                               "%.2f%%p 를 앵커군이 흡수", pool * 100)
        if np.abs(w - before).max() < _TOL:
            logger.debug("상한 수렴: %d회", i + 1)
            break
    else:
        logger.warning("상한이 %d회 내에 수렴하지 않음 — 입력을 확인하세요", MAX_ITER)

    # 최종 안전핀: 어떤 경로로든 합계가 1.0에 못 미치면 앵커가 흡수(합계 100% 최우선)
    resid = 1.0 - float(w.sum())
    if resid > 1e-9:
        w[A] += resid * fmc[A] / fmc[A].sum()
        logger.warning("합계 보정 — 잔여 %.4f%%p 를 앵커군이 흡수(희소 조항)", resid * 100)
    return w


# --------------------------------------------------------------------------- #
# IIF — 김인서 index_calc.py 로 넘기는 산출물
# --------------------------------------------------------------------------- #
def to_iif(w: np.ndarray, fmc: np.ndarray) -> np.ndarray:
    """목표 비중 w를 지수 산식의 캡 조정계수 IIF로 환산.

    지수 산식:  M(t) = Σ IIF(i) × FF(i) × S(i,t) × P(i,t) = Σ IIF(i) × 유동시총(i)
    비중 정의:  w(i) = IIF(i) × 유동시총(i) / M(t)
      ⇒ IIF(i) ∝ w(i) / 유동시총(i)

    비례상수는 지수값에 영향을 주지 않는다(분자·분모에서 상쇄되고, 남는 배율은
    기준시가총액 B(t)가 흡수한다). 따라서 **최댓값이 1.0이 되도록 정규화**해
    IIF를 (0, 1] 구간의 캡 계수로 해석 가능하게 만든다.

    읽는 법 — 상한이 하나도 발동하지 않으면 **IIF는 군마다 한 값**으로 나온다.
    비앵커는 w = 0.60 × 유동시총/Σ유동시총 이므로 w/유동시총이 전 종목 동일하기 때문이며,
    버그가 아니다. 예시 데이터 기준 비앵커 1.0 / 앵커 0.0213 → **"삼성·SK하이닉스는
    유동시총의 2.1%만 지수에 반영한다"**는 뜻이고, 이것이 앵커 40% 제약의 실체다.
    (상한이 걸리는 종목이 생기면 그 종목만 IIF가 따로 낮아진다)
    """
    with np.errstate(divide="ignore", invalid="ignore"):
        raw = np.where(fmc > 0, w / fmc, 0.0)
    peak = raw.max()
    if peak <= 0:
        raise ValueError("IIF 산출 실패 — 유동시총이 모두 0이거나 비중이 산출되지 않았습니다")
    return raw / peak


# --------------------------------------------------------------------------- #
# 공개 진입점
# --------------------------------------------------------------------------- #
def compute_weights(df: pd.DataFrame, cap_basis: str = "bucket") -> pd.DataFrame:
    """편입 구성표(규칙 통과자 전원) → 편입비중 + IIF.

    Parameters
    ----------
    df : `selection.select_constituents()` 산출물 (`군`, `유동시총` 필요)
    cap_basis : "bucket"(확정) | "index"(대안 해석)
    """
    for col in ("군", "유동시총"):
        if col not in df.columns:
            raise ValueError(f"필수 컬럼 누락: {col}")

    d = df.copy()
    fmc = pd.to_numeric(d["유동시총"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
    if (fmc <= 0).any():
        bad = d.loc[fmc <= 0, "종목명"].tolist() if "종목명" in d.columns else "(종목명 없음)"
        raise ValueError(f"유동시총이 0 이하인 종목이 있습니다: {bad}")

    w = allocate(d["군"].to_numpy(), fmc, cap_basis=cap_basis)
    d["편입비중"] = w
    d["편입비중(%)"] = (w * 100).round(2)
    d["IIF"] = to_iif(w, fmc).round(6)
    return d


def verify(d: pd.DataFrame, cap_basis: str = "bucket", tol: float = 1e-6) -> list[str]:
    """산출 결과 자체 검증. 위반 항목을 문자열 리스트로 돌려준다(빈 리스트 = 정상).

    ⑨ 합계 100% · 앵커 40% · 각 상한 준수. 발표 전 자동 점검용.
    """
    w = d["편입비중"].to_numpy(dtype=float)
    g = d["군"].to_numpy()
    core_cap = CORE_CAP_BUCKET * NON_ANCHOR_W if cap_basis == "bucket" else CORE_CAP_INDEX
    sat_cap = SAT_CAP_BUCKET * NON_ANCHOR_W if cap_basis == "bucket" else SAT_CAP_INDEX

    # 희소 조항 판정: 비앵커가 개별·합계 상한을 다 지키며 담을 수 있는 최대치(수용량).
    # 수용량 < 60% 면 잔여를 앵커가 흡수하는 것이 규칙이므로, 앵커 40% 초과·개별
    # 25% 초과를 위반으로 잡으면 안 된다(오탐). 합계 100%가 모든 상한에 우선한다.
    nC, nS = int((g == "핵심").sum()), int((g == "위성").sum())
    capacity = core_cap * nC + min(SAT_TOTAL_CAP, sat_cap * nS)
    scarce = capacity < NON_ANCHOR_W - tol

    issues = []
    if abs(w.sum() - 1.0) > tol:
        issues.append(f"비중 합계 {w.sum()*100:.4f}% ≠ 100%")
    anchor_sum = w[g == "앵커"].sum() if (g == "앵커").any() else 0.0
    if scarce:
        # 희소·퇴화 상황: 앵커는 40% + 비앵커 미수용분을 흡수 → 40% 이상만 요구
        if (g == "앵커").any() and anchor_sum < ANCHOR_W - tol:
            issues.append(f"앵커 합계 {anchor_sum*100:.4f}% < {ANCHOR_W*100:.0f}% (희소 상황에서도 미달)")
    else:
        # 정상 상황: 앵커는 정확히 40%
        if (g == "앵커").any() and abs(anchor_sum - ANCHOR_W) > tol:
            issues.append(f"앵커 합계 {anchor_sum*100:.4f}% ≠ {ANCHOR_W*100:.0f}%")
    if (g == "위성").any() and w[g == "위성"].sum() > SAT_TOTAL_CAP + tol:
        issues.append(f"위성 합계 {w[g=='위성'].sum()*100:.4f}% > 상한 {SAT_TOTAL_CAP*100:.0f}%")
    for grp, cap in (("앵커", ANCHOR_CAP), ("핵심", core_cap), ("위성", sat_cap)):
        m = g == grp
        if not m.any():
            continue
        # 희소·퇴화 조항: 앵커가 잔여를 흡수하는 상황에서는 합계 100% 유지가
        # 앵커 개별 상한(25%)에 우선한다(예: 2종목×25%=50%로는 100%를 못 채움).
        if grp == "앵커" and scarce:
            continue
        if w[m].max() > cap + tol:
            issues.append(f"{grp} 개별 최대 {w[m].max()*100:.4f}% > 상한 {cap*100:.2f}%")

    # IIF 정합성 — IIF × 유동시총으로 되돌린 비중이 목표 비중과 일치해야 한다.
    if "IIF" in d.columns:
        fmc = pd.to_numeric(d["유동시총"], errors="coerce").fillna(0.0).to_numpy(dtype=float)
        m = d["IIF"].to_numpy(dtype=float) * fmc
        if m.sum() > 0 and np.abs(m / m.sum() - w).max() > 1e-4:
            issues.append("IIF로 환산한 비중이 편입비중과 불일치")
    return issues
