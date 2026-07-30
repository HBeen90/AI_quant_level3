"""Collect historical management-issue actions from the official KIND site.

The input file is used only for the 33 ticker/name pairs. Existing Y/N values,
ledger values, and prior evidence are deliberately ignored.

KIND limits disclosure-type searches to one year. The collector therefore
submits seven exact-ticker queries (2020 through the as-of date in 2026) for
official market-action type 0350, "관리종목". Every raw response is preserved
in a ZIP and hashed in the query log so that zero-result findings are auditable.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import time
import zipfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
KIND_BASE = "https://kind.krx.co.kr"
KIND_SEARCH_PAGE = f"{KIND_BASE}/disclosure/details.do?method=searchDetailsMain"
KIND_SEARCH_ENDPOINT = f"{KIND_BASE}/disclosure/details.do"
KIND_TYPE_CODE = "0350"
POSITIVE_CONTROL = {
    "ticker": "003620",
    "name": "KG모빌리티",
    "start": date(2020, 1, 1),
    "end": date(2020, 12, 31),
    "expected_title_fragment": "관리종목지정",
}
DISCLOSURE_GROUPS = (
    "01", "02", "03", "04", "05", "06", "07",
    "08", "09", "10", "11", "13", "14", "20",
)
USER_AGENT = "HBM-index-methodology-audit/1.0"


@dataclass(frozen=True)
class KindEvent:
    ticker: str
    requested_name: str
    event_time: str
    market: str
    company_name_on_kind: str
    title: str
    submitter: str
    acpt_no: str
    source_url: str
    query_start: str
    query_end: str


class _ResultTableParser(HTMLParser):
    """Parse the compact HTML fragment returned by KIND's detail search."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_tbody = False
        self.in_tr = False
        self.in_td = False
        self.cells: list[str] = []
        self.cell_parts: list[str] = []
        self.market = ""
        self.acpt_no = ""
        self.rows: list[dict[str, str]] = []
        self.all_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if tag == "tbody":
            self.in_tbody = True
        elif tag == "tr" and self.in_tbody:
            self.in_tr = True
            self.cells = []
            self.market = ""
            self.acpt_no = ""
        elif tag == "td" and self.in_tr:
            self.in_td = True
            self.cell_parts = []
        elif tag == "img" and self.in_td and not self.market:
            alt = (attr.get("alt") or "").strip()
            if alt in {"유가증권", "코스닥", "코넥스"}:
                self.market = alt
        elif tag == "a" and self.in_tr:
            onclick = attr.get("onclick") or ""
            match = re.search(r"openDisclsViewer\('(\d+)'", onclick)
            if match:
                self.acpt_no = match.group(1)

    def handle_data(self, data: str) -> None:
        self.all_text.append(data)
        if self.in_td:
            self.cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self.in_td:
            text = re.sub(r"\s+", " ", "".join(self.cell_parts)).strip()
            self.cells.append(text)
            self.in_td = False
        elif tag == "tr" and self.in_tr:
            if len(self.cells) >= 5 and self.cells[0].replace(",", "").isdigit():
                self.rows.append(
                    {
                        "number": self.cells[0],
                        "event_time": self.cells[1],
                        "company": self.cells[2],
                        "title": self.cells[3],
                        "submitter": self.cells[4],
                        "market": self.market,
                        "acpt_no": self.acpt_no,
                    }
                )
            self.in_tr = False
        elif tag == "tbody":
            self.in_tbody = False

    def total_count(self) -> int:
        text = re.sub(r"\s+", " ", "".join(self.all_text))
        match = re.search(r"전체\s*([\d,]+)\s*건", text)
        if match:
            return int(match.group(1).replace(",", ""))
        if "조회된 결과값이 없습니다" in text:
            return 0
        raise ValueError("KIND response has neither a result count nor a no-result marker")


def _read_universe(path: Path) -> list[tuple[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    out: dict[str, str] = {}
    for row in rows:
        ticker = str(row.get("ticker") or row.get("코드") or "").strip().zfill(6)
        name = str(row.get("종목명") or row.get("name") or "").strip()
        if not re.fullmatch(r"\d{6}", ticker) or not name:
            raise ValueError(f"invalid universe row: ticker={ticker!r}, name={name!r}")
        out[ticker] = name
    if len(out) != 33:
        raise ValueError(f"expected 33 unique tickers, found {len(out)}")
    return list(out.items())


def _windows(as_of: date) -> list[tuple[date, date]]:
    if as_of < date(2020, 1, 1):
        raise ValueError("as-of date must be on or after 2020-01-01")
    windows = []
    for year in range(2020, as_of.year + 1):
        start = date(year, 1, 1)
        end = min(date(year, 12, 31), as_of)
        if start <= end:
            windows.append((start, end))
    return windows


def _form(ticker: str, start: date, end: date) -> dict[str, str]:
    data = {
        "method": "searchDetailsSub",
        "currentPageSize": "100",
        "pageIndex": "1",
        "orderMode": "1",
        "orderStat": "D",
        "forward": "details_sub",
    }
    for group in DISCLOSURE_GROUPS:
        selected = f"{KIND_TYPE_CODE}|" if group == "02" else ""
        data[f"disclosureType{group}"] = selected
        data[f"pDisclosureType{group}"] = selected
    data.update(
        {
            "searchCodeType": "number",
            "repIsuSrtCd": f"A{ticker}",
            "allRepIsuSrtCd": "",
            "oldSearchCorpName": "",
            "disclosureType": "",
            "disTypevalue": "",
            "reportNm": "",
            "reportCd": "",
            "searchCorpName": "",
            "business": "",
            "marketType": "",
            "settlementMonth": "",
            "securities": "",
            "submitOblgNm": "",
            "enterprise": "",
            "fromDate": start.isoformat(),
            "toDate": end.isoformat(),
            "reportNmTemp": "",
            "reportNmPop": "",
            "disclosureTypeArr02": KIND_TYPE_CODE,
        }
    )
    return data


def _fetch(opener, ticker: str, start: date, end: date, retries: int = 3) -> bytes:
    payload = urlencode(_form(ticker, start, end)).encode("utf-8")
    request = Request(
        KIND_SEARCH_ENDPOINT,
        data=payload,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": KIND_SEARCH_PAGE,
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        },
        method="POST",
    )
    for attempt in range(retries):
        try:
            with opener.open(request, timeout=30) as response:
                body = response.read()
            if "페이지 오류".encode("utf-8") in body:
                raise RuntimeError("KIND returned its error page")
            return body
        except (HTTPError, URLError, TimeoutError, RuntimeError):
            if attempt + 1 == retries:
                raise
            time.sleep(1.0 * (attempt + 1))
    raise AssertionError("unreachable")


def _parse_events(
    body: bytes,
    ticker: str,
    name: str,
    start: date,
    end: date,
) -> tuple[int, list[KindEvent]]:
    text = body.decode("utf-8")
    parser = _ResultTableParser()
    parser.feed(text)
    total = parser.total_count()
    if total > 100 or total != len(parser.rows):
        raise ValueError(
            f"{ticker} {start}..{end}: result truncation "
            f"(total={total}, parsed={len(parser.rows)})"
        )
    events = []
    for row in parser.rows:
        acpt_no = row["acpt_no"]
        source_url = (
            f"{KIND_BASE}/common/disclsviewer.do?method=search"
            f"&acptno={acpt_no}&docno="
            if acpt_no
            else KIND_SEARCH_PAGE
        )
        events.append(
            KindEvent(
                ticker=ticker,
                requested_name=name,
                event_time=row["event_time"],
                market=row["market"],
                company_name_on_kind=row["company"],
                title=row["title"],
                submitter=row["submitter"],
                acpt_no=acpt_no,
                source_url=source_url,
                query_start=start.isoformat(),
                query_end=end.isoformat(),
            )
        )
    return total, events


def _write_csv(path: Path, fieldnames: Iterable[str], rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _event_date(event: KindEvent) -> str:
    return event.event_time[:10]


def _summaries(
    universe: list[tuple[str, str]],
    events: list[KindEvent],
    checked_on: str,
) -> list[dict[str, str | int]]:
    by_ticker: dict[str, list[KindEvent]] = {ticker: [] for ticker, _ in universe}
    for event in events:
        by_ticker[event.ticker].append(event)

    rows = []
    for ticker, name in universe:
        found = sorted(by_ticker[ticker], key=lambda item: item.event_time)
        designated = [
            event
            for event in found
            if "지정" in event.title and "해제" not in event.title and "우려" not in event.title
        ]
        released = [event for event in found if "해제" in event.title]
        rows.append(
            {
                "코드": ticker,
                "종목명": name,
                "지정여부(Y/N)": "Y" if found else "N",
                "지정일": _event_date(designated[0]) if designated else "",
                "해제일": _event_date(released[-1]) if released else "",
                "사유": " | ".join(dict.fromkeys(event.title for event in found)),
                "확인자": "collect_kind_admin_history.py",
                "확인일자": checked_on,
                "근거URL": found[0].source_url if found else KIND_SEARCH_PAGE,
                "검색기간": f"2020-01-01~{checked_on}",
                "공식분류코드": KIND_TYPE_CODE,
                "조회건수": len(found),
                "검토상태": "HUMAN_REVIEW_REQUIRED" if found else "AUTOMATED_KIND_EXACT_TICKER",
                "비고": (
                    "KIND 시장조치 0350 이력 발견; 지정·해제 구간 사람 검토 필요"
                    if found
                    else "연도별 7개 정확 종목코드 질의 결과 0건"
                ),
                "ticker": ticker,
            }
        )
    return rows


def _report(
    path: Path,
    universe: list[tuple[str, str]],
    events: list[KindEvent],
    query_rows: list[dict],
    control_events: list[KindEvent],
    as_of: date,
    raw_zip: Path,
) -> None:
    positives = sorted({event.ticker for event in events})
    lines = [
        "# D3 KIND 관리종목 이력 조사",
        "",
        f"- 조사범위: 2020-01-01~{as_of.isoformat()}",
        f"- 후보 유니버스: {len(universe)}종목",
        f"- 공식 조회 분류: KIND 시장조치 `{KIND_TYPE_CODE}`(관리종목)",
        f"- 질의 수: {len(query_rows)}건(종목별·연도별 정확 종목코드 질의)",
        (
            f"- 양성 대조군: `{POSITIVE_CONTROL['ticker']}` "
            f"{POSITIVE_CONTROL['name']} {len(control_events)}건 검출"
        ),
        f"- 관리종목 이력 발견 종목: {len(positives)}종목",
        "- 자동 조사 상태: 완료 (`human_signoff` 미기입)",
        f"- 원응답 보관: `{raw_zip.as_posix()}`",
        "",
        "## 판정",
        "",
    ]
    if positives:
        lines.append("다음 종목은 KIND 관리종목 이력이 발견되어 편출 이벤트 반영 검토가 필요합니다.")
        lines.append("")
        for ticker in positives:
            name = dict(universe)[ticker]
            lines.append(f"- `{ticker}` {name}")
    else:
        lines.append(
            f"33종목 모두 공식 분류 0350 조회 결과가 0건이므로, "
            f"해당 기간의 관리종목 지정·해제 이력은 **미발견**입니다."
        )
        lines.append("")
        lines.append("따라서 이 조사로 백테스트에 추가할 관리종목 수시편출 이벤트는 **0건**입니다.")
    lines.extend(
        [
            "",
            "## 조사 규율",
            "",
            "- 기존 `admin_history_normalized.csv`의 Y/N 값은 판정 입력으로 사용하지 않았다.",
            "- 현재 소속부나 사업보고서 문구로 과거 이력을 대체하지 않았다.",
            "- KIND의 1년 검색 제한에 맞춰 연도별로 질의했다.",
            "- 동일 수집기로 기지의 관리종목 지정 종목을 조회해 양성 검출을 확인했다.",
            "- 각 응답 원문은 ZIP에 보존하고 SHA-256을 질의 로그에 기록했다.",
            "- `미발견`은 조회 범위와 공식 분류코드에 한정된 결론이다.",
            "",
            "## 산출물",
            "",
            "- `data/admin_history_kind_2020_2026.csv`: 33종목 요약",
            "- `evidence/kind_admin_history_20260730/events.csv`: 발견 이벤트 전량",
            "- `evidence/kind_admin_history_20260730/query_log.csv`: 231개 질의와 응답 해시",
            "- `evidence/kind_admin_history_20260730/control_log.csv`: 양성 대조군 결과",
            "- `evidence/kind_admin_history_20260730/raw_kind_responses.zip`: 원응답",
            "- `evidence/kind_admin_history_20260730/run_manifest.json`: 실행 계보",
            "",
            "## 공식 조회 화면",
            "",
            f"- {KIND_SEARCH_PAGE}",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def collect(
    universe_csv: Path,
    as_of: date,
    delay: float,
    output_csv: Path,
    evidence_dir: Path,
) -> None:
    universe = _read_universe(universe_csv)
    windows = _windows(as_of)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    raw_zip = evidence_dir / "raw_kind_responses.zip"

    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    opener.open(Request(KIND_SEARCH_PAGE, headers={"User-Agent": USER_AGENT}), timeout=30).close()

    all_events: list[KindEvent] = []
    query_rows: list[dict[str, str | int]] = []
    checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    control_events: list[KindEvent] = []
    control_row: dict[str, str | int] = {}
    with zipfile.ZipFile(raw_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for ticker, name in universe:
            for start, end in windows:
                body = _fetch(opener, ticker, start, end)
                count, events = _parse_events(body, ticker, name, start, end)
                response_name = f"{ticker}/{start.year}.html"
                archive.writestr(response_name, body)
                query_rows.append(
                    {
                        "ticker": ticker,
                        "name": name,
                        "query_start": start.isoformat(),
                        "query_end": end.isoformat(),
                        "kind_type_code": KIND_TYPE_CODE,
                        "result_count": count,
                        "response_file": response_name,
                        "response_sha256": hashlib.sha256(body).hexdigest(),
                        "queried_at_utc": checked_at,
                    }
                )
                all_events.extend(events)
                if delay:
                    time.sleep(delay)

        control_body = _fetch(
            opener,
            str(POSITIVE_CONTROL["ticker"]),
            POSITIVE_CONTROL["start"],
            POSITIVE_CONTROL["end"],
        )
        control_count, control_events = _parse_events(
            control_body,
            str(POSITIVE_CONTROL["ticker"]),
            str(POSITIVE_CONTROL["name"]),
            POSITIVE_CONTROL["start"],
            POSITIVE_CONTROL["end"],
        )
        expected = str(POSITIVE_CONTROL["expected_title_fragment"])
        if control_count < 1 or not any(expected in event.title for event in control_events):
            raise RuntimeError(
                f"KIND positive control failed: count={control_count}, expected={expected!r}"
            )
        control_response_name = (
            f"positive_control/{POSITIVE_CONTROL['ticker']}_"
            f"{POSITIVE_CONTROL['start'].year}.html"
        )
        archive.writestr(control_response_name, control_body)
        control_row = {
            "ticker": POSITIVE_CONTROL["ticker"],
            "name": POSITIVE_CONTROL["name"],
            "query_start": POSITIVE_CONTROL["start"].isoformat(),
            "query_end": POSITIVE_CONTROL["end"].isoformat(),
            "kind_type_code": KIND_TYPE_CODE,
            "expected_title_fragment": expected,
            "result_count": control_count,
            "matched": "Y",
            "titles": " | ".join(event.title for event in control_events),
            "response_file": control_response_name,
            "response_sha256": hashlib.sha256(control_body).hexdigest(),
            "queried_at_utc": checked_at,
        }

    summary = _summaries(universe, all_events, as_of.isoformat())
    _write_csv(output_csv, summary[0].keys(), summary)
    event_rows = [asdict(event) for event in all_events]
    event_fields = list(KindEvent.__dataclass_fields__)
    _write_csv(evidence_dir / "events.csv", event_fields, event_rows)
    _write_csv(evidence_dir / "query_log.csv", query_rows[0].keys(), query_rows)
    _write_csv(evidence_dir / "control_log.csv", control_row.keys(), [control_row])

    manifest = {
        "schema_version": 1,
        "status": "AUTOMATED_KIND_QUERY",
        "official_source": KIND_SEARCH_PAGE,
        "kind_type_code": KIND_TYPE_CODE,
        "period": {"from": "2020-01-01", "to": as_of.isoformat()},
        "universe": {
            "count": len(universe),
            "source": universe_csv.relative_to(ROOT).as_posix(),
            "source_sha256": hashlib.sha256(universe_csv.read_bytes()).hexdigest(),
        },
        "query_count": len(query_rows),
        "positive_control": {
            "ticker": POSITIVE_CONTROL["ticker"],
            "name": POSITIVE_CONTROL["name"],
            "result_count": len(control_events),
            "status": "PASS",
        },
        "event_count": len(all_events),
        "positive_tickers": sorted({event.ticker for event in all_events}),
        "raw_response_zip": raw_zip.relative_to(ROOT).as_posix(),
        "raw_response_zip_sha256": hashlib.sha256(raw_zip.read_bytes()).hexdigest(),
        "raw_response_entry_count": len(query_rows) + 1,
        "build_environment": {
            "collector": Path(__file__).relative_to(ROOT).as_posix(),
            "collector_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "python": sys.version.split()[0],
        },
        "generated_at_utc": checked_at,
        "human_signoff": None,
    }
    (evidence_dir / "run_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _report(
        evidence_dir / "KIND_ADMIN_HISTORY_REPORT_20260730.md",
        universe,
        all_events,
        query_rows,
        control_events,
        as_of,
        raw_zip.relative_to(ROOT),
    )

    print(f"[OK] universe={len(universe)} queries={len(query_rows)} events={len(all_events)}")
    print(f"[OK] summary={output_csv}")
    print(f"[OK] evidence={evidence_dir}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--universe-csv",
        type=Path,
        default=ROOT / "data" / "admin_history_normalized.csv",
    )
    parser.add_argument("--as-of", type=date.fromisoformat, default=date(2026, 7, 30))
    parser.add_argument("--delay", type=float, default=0.10)
    parser.add_argument(
        "--output-csv",
        type=Path,
        default=ROOT / "data" / "admin_history_kind_2020_2026.csv",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=ROOT / "evidence" / "kind_admin_history_20260730",
    )
    args = parser.parse_args()
    collect(
        universe_csv=args.universe_csv.resolve(),
        as_of=args.as_of,
        delay=max(0.0, args.delay),
        output_csv=args.output_csv.resolve(),
        evidence_dir=args.evidence_dir.resolve(),
    )


if __name__ == "__main__":
    main()
