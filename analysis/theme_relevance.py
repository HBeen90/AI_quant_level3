"""비앵커 테마 적합도 점수와 현재 비중 기준 유지선 시나리오를 계산한다."""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

NON_ANCHOR = {"core", "satellite"}


def _numeric(df: pd.DataFrame, column: str) -> pd.Series:
    values = pd.to_numeric(df[column], errors="coerce")
    if not np.isfinite(values.to_numpy(dtype=float)).all():
        raise ValueError(f"{column} 결측·비유한값")
    return values


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """점수 산정 계약을 검증하고 행별 적용 점수 열을 만든다."""
    required = {"코드", "종목명", "bucket", "weight", "exposure", "mem_ratio"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"필수 컬럼 누락: {sorted(missing)}")

    d = df.copy()
    d["bucket"] = d["bucket"].astype("string").str.strip().str.lower()
    bad = set(d["bucket"].dropna()) - {"anchor", *NON_ANCHOR}
    if d["bucket"].isna().any() or bad:
        raise ValueError(f"허용하지 않는 bucket: {sorted(map(str, bad))}")

    d["weight"] = _numeric(d, "weight")
    d["exposure"] = pd.to_numeric(d["exposure"], errors="coerce")
    d["mem_ratio"] = pd.to_numeric(d["mem_ratio"], errors="coerce")
    if (d["weight"] < 0).any():
        raise ValueError("weight는 음수일 수 없습니다")

    d["theme_score"] = np.nan
    core = d["bucket"].eq("core")
    satellite = d["bucket"].eq("satellite")
    d.loc[core, "theme_score"] = d.loc[core, "exposure"]
    d.loc[satellite, "theme_score"] = d.loc[satellite, "mem_ratio"]

    active = core | satellite
    scores = d.loc[active, "theme_score"].to_numpy(dtype=float)
    if not np.isfinite(scores).all() or ((scores < 0) | (scores > 1)).any():
        raise ValueError("비앵커 적용 점수는 결측 없이 0~1 범위여야 합니다")
    if d.loc[active, "weight"].sum() <= 0:
        raise ValueError("비앵커 비중 합계가 0입니다")
    return d


def relevance_score(df: pd.DataFrame) -> float:
    """비앵커 비중으로 정규화한 복합 테마 적합도 점수를 반환한다."""
    d = prepare(df)
    active = d["bucket"].isin(NON_ANCHOR)
    weights = d.loc[active, "weight"]
    return float((weights * d.loc[active, "theme_score"]).sum() / weights.sum())


def current_weight_floor(
    df: pd.DataFrame,
    core_hold: float = 0.27,
    satellite_hold: float = 0.67,
) -> float:
    """현재 군별 비중을 고정했을 때 모든 종목이 유지선에 놓인 점수다."""
    d = prepare(df)
    active = d["bucket"].isin(NON_ANCHOR)
    floor_score = d["bucket"].map({"core": core_hold, "satellite": satellite_hold})
    weights = d.loc[active, "weight"]
    return float((weights * floor_score[active]).sum() / weights.sum())


def single_name_stress(
    df: pd.DataFrame,
    core_hold: float = 0.27,
    satellite_hold: float = 0.67,
) -> pd.DataFrame:
    """각 비앵커 종목 하나만 해당 군 유지선으로 낮춘 점수를 반환한다."""
    d = prepare(df)
    rows = []
    for idx in d.index[d["bucket"].isin(NON_ANCHOR)]:
        stressed = d.copy()
        hold = core_hold if d.at[idx, "bucket"] == "core" else satellite_hold
        if d.at[idx, "theme_score"] < hold:
            raise ValueError(f"{d.at[idx, '종목명']}의 현재 점수가 유지선보다 낮습니다")
        if d.at[idx, "bucket"] == "core":
            stressed.at[idx, "exposure"] = hold
        else:
            stressed.at[idx, "mem_ratio"] = hold
        rows.append({
            "코드": d.at[idx, "코드"],
            "종목명": d.at[idx, "종목명"],
            "유지선": hold,
            "스트레스 점수": relevance_score(stressed),
        })
    return pd.DataFrame(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True)
    parser.add_argument("--core-hold", type=float, default=0.27)
    parser.add_argument("--satellite-hold", type=float, default=0.67)
    args = parser.parse_args()

    try:
        source = pd.read_csv(args.csv, dtype={"코드": str})
        score = relevance_score(source)
        floor = current_weight_floor(source, args.core_hold, args.satellite_hold)
        stress = single_name_stress(source, args.core_hold, args.satellite_hold)
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        print(f"[FAIL] {exc}")
        return 1

    print(f"비앵커 테마 적합도: {score:.4%}")
    print(f"현재 비중 기준 유지선 시나리오: {floor:.4%}")
    print(f"방법론 공통 보수 하한: {min(args.core_hold, args.satellite_hold):.4%}")
    print("\n단일 종목 유지선 스트레스")
    shown = stress.copy()
    shown["유지선"] = shown["유지선"].map(lambda value: f"{value:.2%}")
    shown["스트레스 점수"] = shown["스트레스 점수"].map(lambda value: f"{value:.4%}")
    print(shown.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
