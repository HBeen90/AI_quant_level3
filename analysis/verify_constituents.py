# -*- coding: utf-8 -*-
"""확정 구성종목 CSV로 공표 비중을 순방향 교차검증한다.

실행:
    python analysis/verify_constituents.py --csv data/구성종목_인계_20260723.csv

CSV 계약:
    코드, 종목명, bucket(anchor/core/satellite), weight, ff_market_cap

공표 비중은 0~1 스케일이며 소수점 둘째 자리 %p로 반올림되었다고 가정한다.
따라서 결과는 "반올림 허용오차 내 재현"이지 원시 비중의 완전 일치를 뜻하지
않는다.
"""
import argparse
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src import weighting  # noqa: E402

BUCKET_KR = {"anchor": "앵커", "core": "핵심", "satellite": "위성"}
TOL = 5e-5  # 0.005%p: 공표 비중이 소수 둘째 자리 %p로 반올림된 경우의 반 단위
REQUIRED = {"코드", "종목명", "bucket", "weight", "ff_market_cap"}


def _load_input(path: str | os.PathLike[str]) -> pd.DataFrame:
    """입력 계약을 엄격히 확인하고 정규화한 데이터프레임을 반환한다."""
    df = pd.read_csv(path, dtype=str)

    for alias, canonical in (("code", "코드"), ("name", "종목명")):
        if alias in df.columns and canonical in df.columns:
            raise ValueError(f"중복 의미 컬럼: {canonical!r}와 {alias!r}를 함께 쓸 수 없습니다")
        if alias in df.columns:
            df = df.rename(columns={alias: canonical})

    missing = REQUIRED - set(df.columns)
    if missing:
        raise ValueError(f"필수 컬럼 누락: {sorted(missing)}")
    if df.empty:
        raise ValueError("입력 행이 없습니다")

    code = df["코드"].astype("string").str.strip()
    bad_code = code.isna() | ~code.str.fullmatch(r"\d{1,6}", na=False)
    if bad_code.any():
        raise ValueError(f"코드는 1~6자리 숫자여야 합니다: {code[bad_code].tolist()}")
    df["코드"] = code.str.zfill(6)
    if df["코드"].duplicated().any():
        dup = df.loc[df["코드"].duplicated(keep=False), "코드"].tolist()
        raise ValueError(f"코드 중복: {sorted(set(dup))}")

    name = df["종목명"].astype("string").str.strip()
    if (name.isna() | name.eq("")).any():
        raise ValueError("종목명 결측·공백")
    df["종목명"] = name

    df["bucket"] = df["bucket"].astype("string").str.strip().str.lower()
    bad_bucket = set(df["bucket"].dropna()) - set(BUCKET_KR)
    if df["bucket"].isna().any() or bad_bucket:
        raise ValueError(f"허용하지 않는 bucket: {sorted(map(str, bad_bucket))}")

    for column in ("weight", "ff_market_cap"):
        df[column] = pd.to_numeric(df[column], errors="coerce")
        values = df[column].to_numpy(dtype=float)
        if not np.isfinite(values).all():
            raise ValueError(f"{column} 결측·비유한값")

    weights = df["weight"].to_numpy(dtype=float)
    if (weights < 0).any() or (weights > 1).any():
        raise ValueError("weight는 0~1 범위여야 합니다")
    # 각 공표 비중이 반올림되므로 합계도 행 수만큼 반올림 오차가 누적될 수 있다.
    sum_tolerance = len(df) * TOL + 1e-12
    if abs(float(weights.sum()) - 1.0) > sum_tolerance:
        raise ValueError(
            f"weight 합계 {weights.sum():.6f} != 1.0 "
            f"(반올림 허용 {sum_tolerance:.6f})"
        )

    if (df["ff_market_cap"].to_numpy(dtype=float) <= 0).any():
        raise ValueError("ff_market_cap은 양수여야 합니다")
    return df


def verify_csv(path: str | os.PathLike[str]) -> bool:
    """순방향 재현과 weighting 불변식을 모두 통과하면 True를 반환한다."""
    df = _load_input(path)
    groups = df["bucket"].map(BUCKET_KR).to_numpy()
    fmc = df["ff_market_cap"].to_numpy(dtype=float)
    published = df["weight"].to_numpy(dtype=float)

    try:
        w_engine = weighting.allocate(groups, fmc)
    except Exception as exc:
        raise ValueError(f"weighting.allocate 실패: {exc}") from exc

    out = df[["코드", "종목명", "bucket"]].copy()
    out["공표 weight"] = np.round(published, 6)
    out["엔진 재현"] = np.round(w_engine, 6)
    out["차이(%p)"] = np.round((w_engine - published) * 100, 4)
    print(out.to_string(index=False))

    worst = float(np.abs(w_engine - published).max())
    within_rounding = worst <= TOL + 1e-12

    issues = weighting.verify(pd.DataFrame({
        "종목명": df["종목명"],
        "코드": df["코드"],
        "군": pd.Series(groups),
        "유동시총": fmc,
        "편입비중": w_engine,
    }))
    valid_weights = not issues
    print(
        f"\n최대 오차 {worst * 100:.4f}%p "
        f"(허용 {TOL * 100:.3f}%p) -> "
        f"{'[PASS] 공표 반올림 범위 내 순방향 재현' if within_rounding else '[FAIL] 공표 비중 불일치'}"
    )
    print("weighting.verify:", "무위반" if valid_weights else issues)
    return within_rounding and valid_weights


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="확정본 CSV 경로")
    args = ap.parse_args()

    path = Path(args.csv)
    if not path.is_file():
        print(f"[FAIL] CSV 파일 없음: {path}")
        return 1
    try:
        return 0 if verify_csv(path) else 1
    except (OSError, ValueError, pd.errors.ParserError) as exc:
        print(f"[FAIL] {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
