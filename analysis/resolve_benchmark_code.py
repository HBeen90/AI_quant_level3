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
    import argparse

    ap = argparse.ArgumentParser(
        description="Resolve KRX Semiconductor PR/TR codes without guessing.")
    ap.add_argument("--headline", choices=("PR", "TR"), default="PR",
                    help="benchmark.yaml 의 headline_return_type. 이 계열만 "
                         "정확히 1건으로 해결되면 통과한다 (기본 PR).")
    ap.add_argument("--require-both", action="store_true",
                    help="PR·TR 두 계열 모두 해결돼야 통과 (과거 동작).")
    a = ap.parse_args()

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

    # 통과 조건은 '헤드라인 계열이 정확히 1건'이다. 과거에는 PR·TR 둘 다를
    # 요구했는데, KRX 지수 목록에 TR 표기가 존재하지 않으면 그 조건은 영원히
    # 충족되지 않는다. 그러면 해결된 PR 코드까지 [PENDING] 으로 묻혀 버려,
    # '아직 못 찾았다'와 '애초에 없다'가 구분되지 않는다. benchmark.yaml 의
    # 운영 결정도 헤드라인은 PR 하나이므로, 요구 조건을 그에 맞춘다.
    # (--require-both 로 과거 동작 복원 가능)
    required = ("PR", "TR") if a.require_both else (a.headline,)
    optional = tuple(rt for rt in ("PR", "TR") if rt not in required)

    if any(len(matches[rt]) != 1 for rt in required):
        for rt in required:
            print(f"[PENDING] {rt}: expected 1 exact alias, found "
                  f"{len(matches[rt])}")
        print("[ACTION] Confirm the exact KRX display names; do not guess a code.")
        return 1

    print("[RESOLVED]")
    for rt in required:
        item = matches[rt][0]
        print(
            f"{rt}\t{item['market']}\t{item['code']}\t{item['name']}\t"
            f"{item['first']}\t{item['last']}\t{item['observations']}"
        )
    for rt in optional:
        if len(matches[rt]) == 1:
            item = matches[rt][0]
            print(
                f"{rt}(optional)\t{item['market']}\t{item['code']}\t{item['name']}\t"
                f"{item['first']}\t{item['last']}\t{item['observations']}"
            )
        else:
            # 부재를 조용히 넘기지 않는다 - '보조 비교 생략'의 근거로 기록돼야 한다.
            print(f"[ABSENT] {rt}: exact alias not published in the KRX index "
                  f"list (found {len(matches[rt])}). Record this in the "
                  f"committee minutes as the reason the {rt} comparison is omitted.")
    print("[ACTION] Copy the exact code/name and observed first date into the "
          "committee record before setting status: CONFIRMED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
