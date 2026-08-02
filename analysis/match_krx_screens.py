from __future__ import annotations

import argparse
import hashlib
import shutil
from pathlib import Path

import pandas as pd


MIN_MATCH_RATE = 0.95
MIN_RUNNER_UP_GAP = 0.20


def read_screen_close(path: Path) -> pd.Series:
    frame = None
    for encoding in ("cp949", "utf-8-sig", "utf-8"):
        try:
            frame = pd.read_csv(path, encoding=encoding, dtype=str)
            break
        except UnicodeDecodeError:
            continue
    if frame is None:
        raise ValueError(f"cannot decode KRX screen: {path}")

    required = {"종목코드", "종가"}
    if not required.issubset(frame.columns):
        raise ValueError(f"not a KRX all-stock screen: {path.name}")
    ticker = frame["종목코드"].astype(str).str.strip()
    valid = ticker.str.fullmatch(r"\d{6}")
    close = pd.to_numeric(
        frame.loc[valid, "종가"].str.replace(",", "", regex=False),
        errors="coerce",
    )
    close.index = ticker[valid]
    return close.dropna().groupby(level=0).first()


def selection_dates(snapshot_dir: Path) -> list[pd.Timestamp]:
    dates = []
    for path in sorted(snapshot_dir.glob("snapshot_*.csv")):
        frame = pd.read_csv(path, nrows=1)
        if "selection_date" not in frame.columns or frame.empty:
            raise ValueError(f"snapshot has no selection_date: {path.name}")
        dates.append(pd.Timestamp(frame.loc[0, "selection_date"]))
    if not dates:
        raise ValueError(f"no dated snapshots in {snapshot_dir}")
    if len(dates) != len(set(dates)):
        raise ValueError("duplicate snapshot dates")
    return dates


def score_screen(
    close: pd.Series,
    prices: pd.DataFrame,
    dates: list[pd.Timestamp],
) -> list[dict]:
    rows = []
    for date in dates:
        if date not in prices.index:
            raise ValueError(f"price panel misses selection date {date.date()}")
        common = close.index.intersection(prices.columns)
        left = close.reindex(common)
        right = pd.to_numeric(prices.loc[date, common], errors="coerce")
        valid = left.notna() & right.notna()
        compared = int(valid.sum())
        exact = int(((left[valid] - right[valid]).abs() <= 0.5).sum())
        rows.append({
            "selection_date": date,
            "exact_matches": exact,
            "compared": compared,
            "match_rate": exact / compared if compared else 0.0,
        })
    return sorted(rows, key=lambda row: row["exact_matches"], reverse=True)


def map_screens(
    paths: list[Path],
    prices: pd.DataFrame,
    dates: list[pd.Timestamp],
) -> pd.DataFrame:
    assigned = set()
    rows = []
    for path in paths:
        scores = score_screen(read_screen_close(path), prices, dates)
        best = scores[0]
        second = scores[1] if len(scores) > 1 else {
            "exact_matches": 0, "match_rate": 0.0,
        }
        gap = best["match_rate"] - second["match_rate"]
        if best["match_rate"] < MIN_MATCH_RATE:
            raise ValueError(
                f"unmatched KRX screen {path.name}: "
                f"best rate {best['match_rate']:.2%}")
        if gap < MIN_RUNNER_UP_GAP:
            raise ValueError(
                f"ambiguous KRX screen {path.name}: "
                f"best {best['match_rate']:.2%}, second {second['match_rate']:.2%}")
        date = best["selection_date"]
        if date in assigned:
            raise ValueError(f"duplicate KRX screen assignment for {date.date()}")
        assigned.add(date)
        rows.append({
            "source_file": path.name,
            "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "selection_date": date.strftime("%Y-%m-%d"),
            "exact_matches": best["exact_matches"],
            "compared": best["compared"],
            "match_rate": best["match_rate"],
            "runner_up_matches": second["exact_matches"],
            "runner_up_rate": second["match_rate"],
            "copied_file": f"krx_all_{date:%Y%m%d}.csv",
        })

    expected = set(dates)
    if assigned != expected:
        missing = sorted(date.strftime("%Y-%m-%d") for date in expected - assigned)
        raise ValueError(f"KRX screen batch does not cover all review dates: {missing}")
    return pd.DataFrame(rows).sort_values("selection_date").reset_index(drop=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--screens-in", required=True)
    parser.add_argument("--pattern", default="data_*.csv")
    parser.add_argument("--prices-raw", required=True)
    parser.add_argument("--snapshots", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    source_dir = Path(args.screens_in)
    paths = sorted(source_dir.glob(args.pattern))
    if not paths:
        raise SystemExit(f"[FAIL] no KRX screen files in {source_dir}")
    valid_paths = []
    for path in paths:
        try:
            read_screen_close(path)
        except ValueError:
            print(f"[SKIP] not an all-stock screen: {path.name}")
            continue
        valid_paths.append(path)
    paths = valid_paths
    if not paths:
        raise SystemExit(f"[FAIL] no valid KRX all-stock screens in {source_dir}")
    prices = pd.read_csv(args.prices_raw, index_col=0, parse_dates=True)
    prices.columns = [str(column).zfill(6) for column in prices.columns]
    dates = selection_dates(Path(args.snapshots))

    try:
        mapping = map_screens(paths, prices, dates)
    except ValueError as exc:
        raise SystemExit(f"[FAIL] {exc}") from exc
    price_path = Path(args.prices_raw)
    mapping["price_reference_file"] = price_path.name
    mapping["price_reference_sha256"] = hashlib.sha256(
        price_path.read_bytes()).hexdigest()
    print(mapping.to_string(index=False))
    if args.dry_run:
        return 0

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    by_name = {path.name: path for path in paths}
    for row in mapping.itertuples(index=False):
        shutil.copyfile(by_name[row.source_file], out / row.copied_file)
    mapping.to_csv(out.parent / "source_mapping.csv", index=False,
                   encoding="utf-8", lineterminator="\n")
    print(f"[OK] mapped {len(mapping)} KRX screens to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
