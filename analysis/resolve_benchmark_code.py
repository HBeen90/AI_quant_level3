# -*- coding: utf-8 -*-
"""Resolve the KRX Semiconductor PR/TR codes without guessing."""
from __future__ import annotations

import datetime as dt
import os


TARGET_ALIASES = {
    "PR": ("KRX 반도체", "KRX Semicon"),
    "TR": ("KRX 반도체 TR", "KRX Semicon TR"),
}


def _normalise_name(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def classify_target_name(name: object) -> str | None:
    """Return PR/TR only for an exact approved Korean or English alias."""
    normalised = _normalise_name(name)
    for return_type, aliases in TARGET_ALIASES.items():
        if normalised in {_normalise_name(alias) for alias in aliases}:
            return return_type
    return None


def _is_semiconductor_candidate(name: object) -> bool:
    normalised = _normalise_name(name)
    return "반도체" in normalised or "semicon" in normalised


def _as_of() -> str:
    value = os.environ.get("INDEX_ASOF", "").replace("-", "")
    if value:
        if len(value) != 8 or not value.isdigit():
            raise ValueError("INDEX_ASOF must be YYYY-MM-DD or YYYYMMDD")
        return value
    return dt.date.today().strftime("%Y%m%d")


def main() -> int:
    try:
        from pykrx import stock
    except ImportError:
        print("[FAIL] pykrx is required: pip install pykrx")
        return 1

    end = _as_of()
    matches: dict[str, list[dict]] = {"PR": [], "TR": []}
    candidates: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()

    for market in ("KRX", "KOSPI", "KOSDAQ", "테마"):
        try:
            tickers = stock.get_index_ticker_list(market=market)
        except Exception as exc:
            print(f"[WARN] {market} index list unavailable: {exc}")
            continue

        for code in tickers:
            name = str(stock.get_index_ticker_name(code) or "").strip()
            if not _is_semiconductor_candidate(name):
                continue
            key = (str(code), name)
            if key in seen:
                continue
            seen.add(key)
            candidates.append((market, str(code), name))

            return_type = classify_target_name(name)
            if return_type is None:
                continue
            try:
                history = stock.get_index_ohlcv("20100101", end, code)
            except Exception as exc:
                print(f"[WARN] history unavailable: {market}\t{code}\t{name}\t{exc}")
                continue
            if history is None or history.empty:
                print(f"[WARN] no observations: {market}\t{code}\t{name}")
                continue
            matches[return_type].append({
                "market": market,
                "code": str(code),
                "name": name,
                "first": history.index.min().strftime("%Y-%m-%d"),
                "last": history.index.max().strftime("%Y-%m-%d"),
                "observations": len(history),
            })

    print("[SEMICONDUCTOR CANDIDATES]")
    for market, code, name in candidates:
        print(f"{market}\t{code}\t{name}")

    if any(len(matches[return_type]) != 1 for return_type in ("PR", "TR")):
        for return_type in ("PR", "TR"):
            print(f"[PENDING] {return_type}: expected 1 exact alias, found "
                  f"{len(matches[return_type])}")
        print("[ACTION] Confirm the exact KRX display names; do not guess a code.")
        return 1

    print("[RESOLVED]")
    for return_type in ("PR", "TR"):
        item = matches[return_type][0]
        print(
            f"{return_type}\t{item['market']}\t{item['code']}\t{item['name']}\t"
            f"{item['first']}\t{item['last']}\t{item['observations']}"
        )
    print("[ACTION] Copy the exact code/name and observed first date into the "
          "committee record before setting status: CONFIRMED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
