# -*- coding: utf-8 -*-
"""판정 추인용 DART 원문 키워드 검사 - 로컬 실행 (.env 의 DART_API_KEY 사용)

두 판정의 추인 근거를 원문에서 기계 수집한다. 판정 자체를 바꾸지 않으며,
발췌를 사람이 읽고 docs/judgment_record_322310_atsemicon_20260730.md 의
체크박스를 닫기 위한 재료를 만든다.

  판정 1 (322310 오로스): FY2024·FY2025 사업보고서에 HBM 고유공정
      (TSV·본딩·계측) 관여 서술이 실재하는가 -> HBM 언급 >0 + 발췌 문맥
  판정 2 (089530 에이티세미콘): FY2019~FY2022 사업보고서에 HBM 계열
      서술이 부재한가 -> HBM 언급 0건 기대

    python analysis\\check_judgment_ratification.py
    python analysis\\check_judgment_ratification.py --dry-run   # 대상만 출력

산출물: evidence/judgment_ratification_20260730/
  - <코드>_<rcpNo>.txt  발췌(키워드 문장), SHA-256은 summary.json 에 기록
  - summary.json        문서별 언급 횟수·해시·판정 가이드
키워드·본문 정제는 src/hbm_evidence.py 와 동일 함수를 사용한다(도구 단일화).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.hbm_evidence import (_clean, _make_dart, keyword_lines,  # noqa: E402
                              HBM_KW, PROC_KW)

OUT = ROOT / "evidence" / "judgment_ratification_20260730"

#: 판정 1 - 오로스테크놀로지 (원장 True/True 의 근거 원문)
OROS = [("322310", "오로스테크놀로지", "FY2024", "20250312000665"),
        ("322310", "오로스테크놀로지", "FY2025", "20260313000952")]
#: 판정 2 - 에이티세미콘 (부재 확인 대상 사업연도)
ATSEMI_CODE, ATSEMI_NAME = "089530", "에이티세미콘"
ATSEMI_RANGE = ("2020-01-01", "2023-12-31")   # FY2019~FY2022 사업보고서 접수 구간

EXCERPT_KW = sorted(set(HBM_KW + ["TSV", "하이브리드본딩", "하이브리드 본딩",
                                  "TC본더", "TC 본더", "오버레이", "계측"]))


def _atsemi_reports(dart) -> list[tuple[str, str, str, str]]:
    """에이티세미콘 사업보고서 최초 접수분 (final=False - 정정본 함정 회피)."""
    lst = dart.list(ATSEMI_CODE, start=ATSEMI_RANGE[0], end=ATSEMI_RANGE[1],
                    kind="A", final=False)
    if lst is None or len(lst) == 0:
        raise SystemExit("[FAIL] 에이티세미콘 정기보고서 목록 조회 실패")
    lst = lst[lst["report_nm"].str.contains("사업보고서", na=False)].copy()
    lst["fy"] = lst["report_nm"].str.extract(r"\((\d{4})\.")[0]
    lst = lst.sort_values(["rcept_dt", "rcept_no"])
    rows = []
    for fy, g in lst.groupby("fy"):
        first = g.iloc[0]                     # 최초 접수분
        rows.append((ATSEMI_CODE, ATSEMI_NAME, f"FY{fy}",
                     str(first["rcept_no"])))
    return sorted(rows, key=lambda r: r[2])


def _scan_one(dart, code: str, name: str, fy: str, rcp: str) -> dict:
    raw = dart.document(rcp)
    text = _clean(raw if isinstance(raw, str) else str(raw))
    if len(text) < 1000:
        raise SystemExit(f"[FAIL] {name} {fy} rcpNo={rcp} 본문이 비정상적으로 "
                         f"짧음({len(text)}자) - 조회 실패로 간주, 결론 확정 금지")
    n_hbm = sum(text.count(k) for k in HBM_KW)
    n_proc = sum(text.count(k) for k in PROC_KW)
    lines = keyword_lines(text, EXCERPT_KW, limit=40)
    body = (f"# {name}({code}) {fy} rcpNo={rcp}\n"
            f"# 본문 {len(text):,}자 · HBM계열 {n_hbm}회 · 공정키워드 {n_proc}회\n"
            f"# 원문: https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp}\n\n"
            + "\n".join(f"- {l}" for l in lines)
            + ("\n- (키워드 문장 없음)" if not lines else "") + "\n")
    OUT.mkdir(parents=True, exist_ok=True)
    dst = OUT / f"{code}_{rcp}.txt"
    dst.write_text(body, encoding="utf-8", newline="\n")
    return {"code": code, "name": name, "fy": fy, "rcp_no": rcp,
            "chars": len(text), "hbm_mentions": n_hbm, "proc_mentions": n_proc,
            "excerpt_file": dst.relative_to(ROOT).as_posix(),
            "excerpt_sha256": hashlib.sha256(body.encode()).hexdigest()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    key = os.environ.get("DART_API_KEY")
    if not key or key.startswith("여기에"):
        raise SystemExit("[FAIL] DART_API_KEY 가 .env 에 없습니다 "
                         "(hbm_evidence.py 와 동일한 키 사용)")
    dart = _make_dart(key)

    targets = list(OROS)
    if a.dry_run:
        print("대상(오로스 고정 2건 + 에이티세미콘 사업보고서 조회 예정):")
        for t in targets:
            print(" ", t)
        return 0
    targets += _atsemi_reports(dart)

    results = [_scan_one(dart, *t) for t in targets]

    oros = [r for r in results if r["code"] == "322310"]
    ats = [r for r in results if r["code"] == ATSEMI_CODE]
    summary = {
        "generated_for": "docs/judgment_record_322310_atsemicon_20260730.md",
        "documents": results,
        "guide": {
            "판정1_오로스": ("HBM계열 언급이 두 해 모두 >0 이고 발췌 문맥이 "
                            "공정·계측 관여를 보이면 A안(원장 True/True) 추인"),
            "판정2_에이티세미콘": "HBM계열 언급이 전 연도 0이면 비자격 추인",
        },
        "auto_reading": {
            "판정1_hbm_mentions": {r["fy"]: r["hbm_mentions"] for r in oros},
            "판정2_hbm_mentions": {r["fy"]: r["hbm_mentions"] for r in ats},
            "판정1_충족(기계판독)": all(r["hbm_mentions"] > 0 for r in oros),
            "판정2_충족(기계판독)": all(r["hbm_mentions"] == 0 for r in ats),
        },
        "note": "기계 판독은 참고이며, 추인은 발췌 문맥을 사람이 읽고 서명한다.",
    }
    (OUT / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8", newline="\n")

    print(f"{'문서':26s} {'HBM계열':>7s} {'공정KW':>7s}  발췌파일")
    for r in results:
        print(f"{r['name']}({r['code']}) {r['fy']:7s} {r['hbm_mentions']:7d} "
              f"{r['proc_mentions']:7d}  {r['excerpt_file']}")
    print(f"\n판정1 기계판독: {summary['auto_reading']['판정1_충족(기계판독)']} "
          f"| 판정2 기계판독: {summary['auto_reading']['판정2_충족(기계판독)']}")
    print(f"[OK] 발췌·요약 저장: {OUT.relative_to(ROOT)}")
    print("다음: 발췌 파일과 summary.json 을 Claude 세션에 공유 - 문맥 판독 후 "
          "판정 기록 체크박스를 닫습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
