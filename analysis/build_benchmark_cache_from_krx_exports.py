# -*- coding: utf-8 -*-
"""Build the confirmed benchmark cache from archived KRX CSV exports.

KRX exports the selected index in descending date order and CP949 encoding.
The raw files do not repeat the index code/name, so their identity and hashes
are pinned in a manifest.  This script validates that manifest, verifies every
reported daily change and segment boundary, then writes the one-column cache
consumed by ``analysis/run_backtest.py``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import pandas as pd


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _read_segment(path: Path, spec: dict) -> pd.DataFrame:
    actual_hash = sha256(path)
    if actual_hash != spec["sha256"]:
        raise ValueError(
            f"raw hash mismatch: {path.name}: {actual_hash} != {spec['sha256']}"
        )

    raw = pd.read_csv(path, encoding="cp949")
    if raw.shape[1] < 4:
        raise ValueError(f"unexpected KRX columns: {path.name}")
    out = pd.DataFrame({
        "date": pd.to_datetime(raw.iloc[:, 0], format="%Y/%m/%d", errors="raise"),
        "level": pd.to_numeric(raw.iloc[:, 1], errors="raise"),
        "change": pd.to_numeric(raw.iloc[:, 2], errors="raise"),
    }).sort_values("date")
    if out["date"].duplicated().any() or (out["level"] <= 0).any():
        raise ValueError(f"invalid dates or levels: {path.name}")
    if len(out) != int(spec["rows"]):
        raise ValueError(f"row count mismatch: {path.name}")
    if out["date"].min().strftime("%Y-%m-%d") != spec["start"]:
        raise ValueError(f"start date mismatch: {path.name}")
    if out["date"].max().strftime("%Y-%m-%d") != spec["end"]:
        raise ValueError(f"end date mismatch: {path.name}")

    # KRX 'change' is today's close minus the previous trading day's close.
    calc = out["level"].diff()
    bad = (calc.sub(out["change"]).abs() > 0.011) & calc.notna()
    if bad.any():
        row = out.loc[bad].iloc[0]
        raise ValueError(f"reported change mismatch: {path.name} {row['date'].date()}")
    return out[["date", "level", "change"]]


def build_cache(manifest_path: str, out_path: str) -> pd.DataFrame:
    manifest_file = Path(manifest_path).resolve()
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if manifest.get("index_code") != "5044" or manifest.get("return_type") != "PR":
        raise ValueError("manifest is not the confirmed KRX Semiconductor PR series (5044)")

    segments = []
    for spec in manifest.get("files", []):
        path = manifest_file.parent / spec["file"]
        if not path.is_file():
            raise ValueError(f"raw export missing: {path}")
        segments.append(_read_segment(path, spec))
    if not segments:
        raise ValueError("manifest contains no raw exports")

    combined = pd.concat(segments, ignore_index=True)
    overlap = combined[combined.duplicated("date", keep=False)]
    if not overlap.empty and overlap.groupby("date")["level"].nunique().max() != 1:
        raise ValueError("overlapping KRX exports disagree")
    combined = combined.sort_values("date").drop_duplicates("date", keep="first")

    expected_start = pd.Timestamp(manifest["coverage_start"])
    expected_end = pd.Timestamp(manifest["coverage_end"])
    if combined.iloc[0]["date"] != expected_start or combined.iloc[-1]["date"] != expected_end:
        raise ValueError("combined coverage does not match manifest")
    if len(combined) != int(manifest["observations"]):
        raise ValueError("combined observation count does not match manifest")
    gaps = combined["date"].diff().dropna().dt.days
    if not gaps.empty and gaps.max() > 10:
        raise ValueError(f"suspicious calendar gap: {int(gaps.max())} days")

    # Recheck changes across file boundaries after deduplication.
    calc = combined["level"].diff()
    bad = (calc.sub(combined["change"]).abs() > 0.011) & calc.notna()
    if bad.any():
        row = combined.loc[bad].iloc[0]
        raise ValueError(f"segment boundary mismatch: {row['date'].date()}")

    cache = combined.set_index("date")[["level"]]
    cache.index.name = "date"
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    cache.to_csv(tmp, encoding="utf-8", lineterminator="\n")
    os.replace(tmp, target)
    return cache


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    try:
        cache = build_cache(args.manifest, args.out)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"[FAIL] KRX benchmark cache: {exc}")
        return 1
    print(
        f"[OK] KRX Semiconductor PR (5044): {len(cache)} observations, "
        f"{cache.index.min().date()}~{cache.index.max().date()} -> {args.out}"
    )
    print(f"[SHA-256] {sha256(Path(args.out))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
