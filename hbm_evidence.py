"""
HBM 판정 근거 자동 수집 - 손으로 채워야 하는 3개 값의 '읽을 재료'를 차려준다.

자동으로 못 채우는 값
  · HBM양산      (규칙 0)   True / False
  · HBM노출도    (규칙 A)   HBM 밸류체인 매출 ÷ 전사 매출
  · 메모리향비중  (규칙 C①)  메모리 반도체용 매출 ÷ 전사 매출

이 값들은 기업이 별도 공시하지 않아 사람이 판단해야 한다. 다만 **판단에 필요한 근거를
찾아 여는 작업**은 자동화할 수 있다. 이 도구가 하는 일:

  1. 지정 사업연도의 사업보고서(미지정 시 최신)를 찾아 본문을 내려받는다
  2. 「매출 및 수주상황」의 제품별 매출 표를 뽑는다
  3. HBM 관련 키워드(HBM · TSV · 하이브리드본딩 · TC본더 …)가 나온 문장을 전부 모은다
  4. 최근 공급계약(수주) 공시 제목을 모은다  ← HBM 양산·납품 정황 증거
  5. 종목별 '판정 카드'(.md)로 저장 → 이것만 읽고 숫자만 적으면 끝

즉 **찾는 시간을 없애고 판단만 남긴다.**

사용법
    python hbm_evidence.py --input 코드리스트.csv
    python hbm_evidence.py --codes 042700,005930
결과: evidence/<코드>_<종목명>.md  +  evidence/판정입력_템플릿.csv
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
from pathlib import Path

import pandas as pd

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

def _make_dart(key: str):
    """OpenDartReader 버전차 흡수. 0.3.x=opendartreader.OpenDartReader / 0.2.x=OpenDartReader()."""
    try:
        import opendartreader
        return opendartreader.OpenDartReader(key)
    except ImportError:
        import OpenDartReader
        cls = getattr(OpenDartReader, "OpenDartReader", OpenDartReader)
        return cls(key)

OUTDIR = Path(__file__).parent / "evidence"

# 근거 문장을 뽑을 키워드 - 규칙 A(HBM 노출도)와 규칙 C②(고유공정) 판정용
HBM_KW = ["HBM", "고대역폭", "High Bandwidth"]
PROC_KW = ["TSV", "하이브리드본딩", "하이브리드 본딩", "본딩", "TC본더", "TC 본더",
           "적층", "패키징", "리플로우", "다이본더"]
MEM_KW = ["메모리", "DRAM", "D램", "NAND", "낸드", "SSD"]
NONMEM_KW = ["디스플레이", "OLED", "LCD", "시스템반도체", "비메모리", "파운드리", "이차전지", "전장"]


def _clean(html: str) -> str:
    """공시 원문(XML/HTML) → 읽을 수 있는 평문."""
    t = re.sub(r"<BR\s*/?>", "\n", html, flags=re.I)
    t = re.sub(r"</(TR|P|TABLE|DIV)>", "\n", t, flags=re.I)
    t = re.sub(r"</TD>", " | ", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = (t.replace("&nbsp;", " ").replace("&amp;", "&")
         .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t)
    return t.strip()


def latest_report(dart, code: str, years_back: int = 2):
    """최신 사업보고서(없으면 반기·분기) 접수번호. 반환: (제목, 접수번호, 접수일)"""
    start = (dt.date.today() - dt.timedelta(days=365 * years_back)).strftime("%Y-%m-%d")
    try:
        lst = dart.list(code, start=start, kind="A")
    except Exception:
        return None
    if lst is None or len(lst) == 0:
        return None
    for pat in ("사업보고서", "반기보고서", "분기보고서"):
        hit = lst[lst["report_nm"].astype(str).str.contains(pat, na=False)]
        if len(hit):
            r = hit.sort_values("rcept_dt", ascending=False).iloc[0]
            return str(r["report_nm"]).strip(), str(r["rcept_no"]), str(r["rcept_dt"])
    return None


def annual_report(dart, code: str, fiscal_year: int):
    """Return the annual report filed for one fiscal year.

    A fiscal-year report is normally filed in the following calendar year.
    Restricting the query window prevents a current report from leaking into a
    historical judgment card.

    Note: the window assumes a **December fiscal-year end** (the report lands in
    the next calendar year). This holds for essentially all Korean listed
    semiconductor names in scope; a non-December-FYE issuer would file within
    its own calendar year and fall outside this window. Widening the window is
    not safe without each firm's FYE (it would re-admit look-ahead), so this is
    left as a documented assumption rather than a silent guess.
    """
    start = f"{fiscal_year + 1}-01-01"
    end = f"{fiscal_year + 1}-12-31"
    try:
        # OpenDartReader defaults to final=True, which hides original filings
        # when a corrected report exists. PIT reconstruction must see both.
        lst = dart.list(code, start=start, end=end, kind="A", final=False)
    except Exception:
        return None
    if lst is None or len(lst) == 0:
        return None
    hit = lst[lst["report_nm"].astype(str).str.contains("사업보고서", na=False)]
    if len(hit) == 0:
        return None
    original = hit[~hit["report_nm"].astype(str).str.contains("정정", na=False)]
    chosen = original if len(original) else hit
    r = chosen.sort_values(
        ["rcept_dt", "rcept_no"], ascending=[True, True]
    ).iloc[0]
    return str(r["report_nm"]).strip(), str(r["rcept_no"]), str(r["rcept_dt"])


def sales_section(text: str, span: int = 6000) -> str:
    """「매출 및 수주상황」 본문 구간 - 제품별 매출 표가 여기 있다.

    목차에도 같은 제목이 나오므로, 뒤쪽(본문) 것을 고른다.
    """
    hits = [m.start() for m in re.finditer(r"매출\s*및\s*수주\s*상황", text)]
    if not hits:
        hits = [m.start() for m in re.finditer(r"주요\s*제품\s*등의\s*현황", text)]
    if not hits:
        return ""
    return text[hits[-1]: hits[-1] + span]


def keyword_lines(text: str, keywords, limit: int = 25, width: int = 160) -> list[str]:
    """키워드가 나온 줄을 중복 없이 모은다(짧은 목차 줄은 제외)."""
    out, seen = [], set()
    for line in text.split("\n"):
        s = line.strip()
        if len(s) < 15 or s.count("|") > 12:
            continue
        if any(k in s for k in keywords):
            key = s[:60]
            if key in seen:
                continue
            seen.add(key)
            out.append(s[:width])
            if len(out) >= limit:
                break
    return out


_IR_KW = ("IR자료", "IR 자료", "IR/PR", "IR", "investor", "Investor",
          "투자정보", "투자자", "실적", "공시정보", "기업설명")
_IR_SKIP = ("javascript", "mailto:", "#", "tel:")


def ir_links(dart, code: str, limit: int = 6) -> tuple[str, list[str]]:
    """회사 홈페이지에서 IR 페이지 링크를 찾아 준다.

    IR 자료(실적발표 PPT)는 DART에 올라오지 않으므로(첨부파일 확인 결과 없음)
    회사 홈페이지를 봐야 한다. 홈페이지 주소는 DART 기업개황(hm_url)에 있으므로,
    거기 접속해 IR·투자정보 링크를 뽑아 **바로 클릭할 수 있게** 카드에 넣는다.

    실패해도 조용히 넘어가지 않고 사유를 문자열로 돌려준다.
    반환: (홈페이지 URL, ["표시명 → 링크", ...])
    """
    try:
        info = dart.company(code)
        home = str(info.get("hm_url") or "").strip()
        ir_direct = str(info.get("ir_url") or "").strip()
    except Exception as e:
        return "", [f"(기업개황 조회 실패: {str(e)[:40]})"]
    if not home:
        return "", ["(DART 기업개황에 홈페이지 주소 없음)"]
    if not home.startswith("http"):
        home = "https://" + home

    out = []
    if ir_direct:
        out.append(f"IR 페이지(공시 등록) → {ir_direct}")

    try:
        import requests
        r = requests.get(home, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
        r.encoding = r.apparent_encoding or "utf-8"
        html = r.text
    except Exception as e:
        out.append(f"(홈페이지 접속 실패: {str(e)[:40]})")
        return home, out

    from urllib.parse import urljoin
    seen = set()
    for href, txt in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                                html, re.I | re.S):
        label = re.sub(r"<[^>]+>", "", txt)
        label = re.sub(r"\s+", " ", label).strip()
        if not label or len(label) > 25:
            continue
        if any(s in href.lower() for s in _IR_SKIP) or href.strip() == "#":
            continue
        if not any(k in (label + " " + href) for k in _IR_KW):
            continue
        full = urljoin(home, href.replace("&amp;", "&"))
        if full in seen:
            continue
        seen.add(full)
        out.append(f"{label} → {full}")
        if len(out) >= limit:
            break
    if len(out) == (1 if ir_direct else 0):
        out.append("(홈페이지에서 IR 링크를 찾지 못함 - 사이트에서 직접 탐색 필요)")
    return home, out


#: build_card 가 관리종목 판정을 '아직 모름'으로 남기는 표기값. 빈칸으로 나가
#: 사람이 확인한다 - False(=관리종목 아님)로 조용히 코어스하지 않는다.
ADMIN_UNKNOWN = ("PIT 수동확인", "조회실패-수동확인")


def admin_status_map() -> dict[str, str] | None:
    """전 종목의 소속부(관리종목·투자주의환기 등)를 한 번에 조회.

    FinanceDataReader 의 KRX 상장목록 `Dept` 컬럼에 거래소 지정 상태가 들어 있다.
    (예: '관리종목(소속부없음)', '투자주의환기종목(소속부없음)')
    pykrx·DART 에는 없는 정보라 여기서 받는다. 전체를 한 번만 받아 사전으로 만든다.

    반환: 성공 시 {코드: 상태} 사전(해당 종목이 없으면 빈 사전). **조회 실패 시
    None** - 빈 사전(성공했으나 지정 종목 0)과 구분해야 한다. 실패를 빈 사전으로
    돌려주면 호출부가 전 종목을 '관리종목 아님'으로 오판한다(조용한 오분류).
    """
    try:
        import FinanceDataReader as fdr
        df = fdr.StockListing("KRX")
    except Exception:
        return None
    out = {}
    for _, r in df.iterrows():
        dept = str(r.get("Dept") or "")
        code = str(r.get("Code") or "").zfill(6)
        if "관리종목" in dept:
            out[code] = "관리종목"
        elif "투자주의환기" in dept:
            out[code] = "투자주의환기"
        elif "정리매매" in dept:
            out[code] = "정리매매"
    return out


def admin_flag_for(code: str, admin: dict | None, fiscal_year: int | None) -> str:
    """카드에 넣을 관리종목 표기값을 정한다.

    · PIT(과거연도) 모드: 현재 상태 자동 혼입 금지 → 'PIT 수동확인'
    · 조회 실패(admin is None): 'False' 로 코어스하지 않고 '조회실패-수동확인'
    · 조회 성공: 지정 종목이면 그 상태, 아니면 '해당없음'
    """
    if fiscal_year is not None:
        return "PIT 수동확인"
    if admin is None:
        return "조회실패-수동확인"
    return admin.get(code, "해당없음")


def admin_issue_cell(v):
    """판정 템플릿의 관리종목 셀 값. UNKNOWN 은 빈칸(사람이 채움), 아니면 bool.
    '조회 실패'와 '조회했으나 해당없음(False)'을 뭉개지 않는 것이 핵심.

    카드 생성이 조기 실패한 행(보고서·본문 조회 실패)은 '관리종목' 키가 없어
    summary 에서 NaN 이 된다. NaN 을 그대로 두면 `NaN != '해당없음'`이 True 가
    되어 미조회 종목이 '관리종목'으로 오기록되므로, NaN·빈칸도 UNKNOWN(빈칸)으로
    본다(수집 실패를 False 로도 True 로도 코어스하지 않는다)."""
    if pd.isna(v) or v in ADMIN_UNKNOWN or (isinstance(v, str) and v.strip() == ""):
        return ""
    return v != "해당없음"


def audit_opinion(text: str) -> tuple[str, list[str]]:
    """사업보고서 본문에서 회계감사인의 감사의견을 추출.

    「V. 회계감사인의 감사의견 등」 표에 '적정 / 한정 / 부적정 / 의견거절'이 나온다.
    DART report() API 로는 제공되지 않아 본문에서 찾는다.
    반환: (판정, 근거문장들).  판정이 '확인필요'면 사람이 원문을 봐야 한다.
    """
    lines = []
    for m in re.finditer(r"감사의견", text):
        seg = re.sub(r"\s+", " ", text[max(0, m.start() - 150): m.start() + 300]).strip()
        if seg not in lines:
            lines.append(seg)
    joined = " ".join(lines)
    # 부정 의견이 하나라도 있으면 그쪽이 우선(보수적)
    for bad in ("의견거절", "부적정", "한정의견", "한정 의견"):
        if bad in joined:
            return bad, lines[:4]
    if "적정" in joined:
        return "적정", lines[:4]
    return "확인필요", lines[:4]


def dilution_events(dart, code: str, years_back: int = 2,
                    end_date: str | None = None) -> list[str]:
    """유상증자·전환사채 등 희석 이벤트 공시 - 편입 직후 주가 희석 리스크 점검용."""
    end = (pd.Timestamp(end_date).date() if end_date else dt.date.today())
    start = (end - dt.timedelta(days=365 * years_back)).strftime("%Y-%m-%d")
    try:
        lst = dart.list(code, start=start, end=end.strftime("%Y-%m-%d"))
    except Exception:
        return []
    if lst is None or len(lst) == 0:
        return []
    pat = "유상증자|전환사채|신주인수권부사채|교환사채|무상증자|주식분할|주식병합"
    hit = lst[lst["report_nm"].astype(str).str.contains(pat, na=False)]
    hit = hit.sort_values("rcept_dt", ascending=False).head(10)
    return [f"{r['rcept_dt']}  {str(r['report_nm']).strip()}" for _, r in hit.iterrows()]


def supply_contracts(dart, code: str, years_back: int = 2,
                     end_date: str | None = None) -> list[str]:
    """단일판매·공급계약 공시 제목 - 누구에게 무엇을 납품하는지의 정황 증거."""
    end = (pd.Timestamp(end_date).date() if end_date else dt.date.today())
    start = (end - dt.timedelta(days=365 * years_back)).strftime("%Y-%m-%d")
    try:
        lst = dart.list(code, start=start, end=end.strftime("%Y-%m-%d"))
    except Exception:
        return []
    if lst is None or len(lst) == 0:
        return []
    m = lst["report_nm"].astype(str).str.contains("공급계약|판매계약|수주", na=False)
    hit = lst[m].sort_values("rcept_dt", ascending=False).head(12)
    return [f"{r['rcept_dt']}  {str(r['report_nm']).strip()}" for _, r in hit.iterrows()]


def build_card(dart, code: str, name: str | None = None,
               admin: dict[str, str] | None = None,
               fiscal_year: int | None = None) -> dict:
    """종목 1개의 판정 카드(마크다운) 생성. 반환: 요약 dict"""
    admin_flag = admin_flag_for(code, admin, fiscal_year)
    info = (annual_report(dart, code, fiscal_year)
            if fiscal_year is not None else latest_report(dart, code))
    if info is None:
        return {"코드": code, "종목명": name or "", "상태": "정기보고서 없음"}
    title, rcept, rdate = info
    try:
        raw = dart.document(rcept)
    except Exception as e:
        return {"코드": code, "종목명": name or "", "상태": f"본문 조회 실패({str(e)[:30]})"}
    text = _clean(raw if isinstance(raw, str) else str(raw))

    sales = sales_section(text)
    hbm_lines = keyword_lines(text, HBM_KW, limit=20)
    proc_lines = keyword_lines(text, PROC_KW, limit=15)
    mem_lines = keyword_lines(text, MEM_KW, limit=15)
    non_lines = keyword_lines(text, NONMEM_KW, limit=10)
    report_asof = pd.to_datetime(rdate, format="%Y%m%d").strftime("%Y-%m-%d")
    contracts = supply_contracts(dart, code, end_date=report_asof)
    opinion, op_lines = audit_opinion(text)
    dilution = dilution_events(dart, code, end_date=report_asof)
    if fiscal_year is None:
        home, irs = ir_links(dart, code)
    else:
        home, irs = "", ["(과거 PIT 모드: 현재 IR 링크 자동수집 생략)"]

    n_hbm = sum(text.count(k) for k in HBM_KW)
    n_proc = sum(text.count(k) for k in PROC_KW)

    md = [f"# {name or code} ({code}) - HBM 판정 근거",
          "",
          f"> 출처: **{title}** (접수 {rdate}, 접수번호 {rcept})",
          f"> 본문 {len(text):,}자 · HBM 언급 **{n_hbm}회** · HBM 고유공정 키워드 **{n_proc}회**",
          "",
          "## ■ 판정 입력값 - 여기에 적으세요",
          "",
          "| 항목 | 값 | 근거(본 문서 어느 부분) |",
          "|---|---|---|",
          "| HBM양산 (규칙 0) | True / False |  |",
          "| HBM노출도 (규칙 A) | 0.__ |  |",
          "| 메모리향비중 (규칙 C①) | 0.__ |  |",
          "| HBM공정확인 (규칙 C②) | True / False |  |",
          "",
          "**자동 조회된 사전 스크린 항목**",
          "",
          f"- 감사의견: **{opinion}**"
          + ("  ← 적정이 아니므로 **제외 대상**" if opinion != "적정" else ""),
          f"- 거래소 지정: **{admin_flag}**"
          + ("  ← **제외 대상**" if admin_flag != "해당없음" else ""),
          "",
          "**IR 자료 바로가기** (DART에는 IR 자료가 올라오지 않아 회사 홈페이지에서 봐야 한다)",
          "",
          (f"- 홈페이지: {home}" if home else "- 홈페이지: (DART에 등록된 주소 없음)"),
          ]
    md += [f"- {x}" for x in irs]
    md += ["",
           "> 아래 3번 'HBM 언급'만으로 노출도 판단이 서면 IR은 열지 않아도 된다.",
           "",
          "---",
          "",
          "## 1. 제품별 매출 (「매출 및 수주상황」 본문)",
          "",
          "```",
          (sales[:4000] if sales else "(해당 섹션을 찾지 못함 - 아래 원문 링크에서 직접 확인)"),
          "```",
          "",
          "## 2. HBM 언급 문장",
          ""]
    md += [f"- {l}" for l in hbm_lines] or ["- (언급 없음 → HBM 노출도 산정 곤란 가능성. 규칙 A의 보수 원칙 적용 검토)"]
    md += ["", "## 3. HBM 고유공정 관련 (규칙 C② 판정용)", ""]
    md += [f"- {l}" for l in proc_lines] or ["- (관련 언급 없음)"]
    md += ["", "## 4. 메모리 관련 언급 (메모리향 비중 판단용)", ""]
    md += [f"- {l}" for l in mem_lines] or ["- (언급 없음)"]
    md += ["", "## 5. 비메모리·타 산업 언급 (메모리향 비중을 낮추는 요인)", ""]
    md += [f"- {l}" for l in non_lines] or ["- (언급 없음 → 메모리 전문 기업일 가능성)"]
    md += ["", "## 6. 최근 공급계약·수주 공시 (양산·납품 정황)", ""]
    md += [f"- {c}" for c in contracts] or ["- (해당 공시 없음)"]
    md += ["", "## 7. 감사의견 근거 문장 (사전 스크린용)", ""]
    md += [f"- {l}" for l in op_lines] or ["- (본문에서 찾지 못함 → 원문 확인 필요)"]
    md += ["", "## 8. 희석 이벤트 공시 (유상증자·CB 등)", ""]
    md += [f"- {c}" for c in dilution] or ["- (최근 2년 내 해당 공시 없음)"]
    md += ["",
           "---",
           "",
           f"원문 보기: https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept}",
           "",
           "> IR 자료(실적발표 PPT)는 DART에 없으므로 회사 IR 페이지에서 별도 확인.",
           "> 노출도 산정이 곤란하면 **규칙 A에 따라 제외**한다(억지로 추정하지 않는다).",
           ]

    OUTDIR.mkdir(exist_ok=True)
    safe = re.sub(r"[^\w가-힣]", "", (name or code))
    (OUTDIR / f"{code}_{safe}.md").write_text("\n".join(md), encoding="utf-8")

    return {"코드": code, "종목명": name or "", "보고서": title, "접수일": rdate,
            "접수번호": rcept, "사업연도": fiscal_year,
            "HBM언급": n_hbm, "고유공정언급": n_proc,
            "공급계약수": len(contracts), "감사의견": opinion,
            "관리종목": admin_flag, "희석공시": len(dilution), "상태": "ok"}


def main():
    ap = argparse.ArgumentParser(description="HBM 판정 근거 자동 수집")
    ap.add_argument("--input", help="코드 리스트 CSV (컬럼: 코드[, 종목명])")
    ap.add_argument("--codes", help="쉼표로 구분한 종목코드 (예: 042700,005930)")
    ap.add_argument("--fiscal-year", type=int,
                    help="PIT 카드 사업연도. 지정 시 해당 연도 사업보고서만 사용")
    args = ap.parse_args()

    if args.codes:
        df = pd.DataFrame({"코드": [c.strip() for c in args.codes.split(",")]})
    elif args.input:
        df = pd.read_csv(args.input, dtype={"코드": str})
    else:
        df = pd.DataFrame({"코드": ["005930", "000660", "042700"]})
        print("※ 입력이 없어 데모 3종목으로 실행합니다.\n")
    df["코드"] = df["코드"].astype(str).str.zfill(6)

    key = os.environ.get("DART_API_KEY")
    if not key or key.startswith("여기에"):
        print("DART_API_KEY 가 .env 에 없습니다. 중단합니다.")
        return
    dart = _make_dart(key)

    try:
        from pykrx import stock
    except Exception:
        stock = None

    if args.fiscal_year is None:
        print("거래소 지정(관리종목·투자주의환기) 목록 조회 중... ",
              end="", flush=True)
        admin = admin_status_map()
        print("조회 실패(관리종목은 UNKNOWN 으로 표기 - 수동 확인 필요)"
              if admin is None else f"{len(admin)}종목 확인")
    else:
        admin = {}
        print(f"FY{args.fiscal_year} PIT 모드: 현재 관리종목·IR 정보 자동 혼입 차단")

    rows = []
    for _, r in df.iterrows():
        code = r["코드"]
        name = r.get("종목명")
        if (not name or pd.isna(name)) and stock is not None:
            try:
                name = stock.get_market_ticker_name(code)
            except Exception:
                name = None
        print(f"  {code} {name or ''} ... ", end="", flush=True)
        res = build_card(dart, code, name, admin, args.fiscal_year)
        rows.append(res)
        if res["상태"] != "ok":
            print(res["상태"])
        else:
            warn = ("  [WARN] " + res["관리종목"]) if res["관리종목"] != "해당없음" else ""
            print(f"HBM {res['HBM언급']}회 · 공정 {res['고유공정언급']}회 · "
                  f"계약 {res['공급계약수']}건 · 감사 {res['감사의견']}{warn}")

    summary = pd.DataFrame(rows)
    OUTDIR.mkdir(exist_ok=True)

    # 판정 입력 템플릿 - 카드를 읽고 이 CSV만 채우면 build_index.py 에 바로 들어간다
    tpl = summary[["종목명", "코드"]].copy()
    tpl["사업연도"] = args.fiscal_year if args.fiscal_year is not None else ""
    if "감사의견" in summary.columns:                  # 자동 추출값 미리 채움
        tpl["감사의견"] = summary["감사의견"]
    if "관리종목" in summary.columns:
        tpl["관리종목"] = summary["관리종목"].map(admin_issue_cell)
    for c in ["유형", "HBM양산", "HBM노출도", "메모리향비중", "HBM공정확인", "위원회확인"]:
        tpl[c] = ""
    receipt = summary.get("접수일", pd.Series("", index=summary.index))
    tpl["근거공개일"] = pd.to_datetime(
        receipt, format="%Y%m%d", errors="coerce").dt.strftime("%Y-%m-%d")
    rcept = summary.get("접수번호", pd.Series("", index=summary.index))
    tpl["근거출처"] = rcept.map(
        lambda value: (f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={value}"
                       if str(value).strip() else ""))
    tpl["판정자"] = ""
    tpl["판정상태"] = "DRAFT"
    tpl.to_csv(OUTDIR / "판정입력_템플릿.csv", index=False, encoding="utf-8-sig")

    print(f"\n판정 카드: {OUTDIR}\\<코드>_<종목명>.md  ({len(summary)}건)")
    print(f"입력 템플릿: {OUTDIR}\\판정입력_템플릿.csv")
    print("  · 자동 채움: 사업연도 · 감사의견 · DART 공시일·출처")
    print("  · 수동 확인: 관리종목 · 유형 · HBM양산 · HBM노출도 · "
          "메모리향비중 · HBM공정확인")
    print("  · 위원회 입력: 위원회확인 · 판정자 · FINAL 상태")
    print("→ 카드를 읽고 빈칸만 채우면 판정 입력이 완성됩니다.")


if __name__ == "__main__":
    main()
