# -*- coding: utf-8 -*-
"""
recollect_pit_evidence.py ― DART 원문 215건 독립 재수집·대조 (L09 승격)
=========================================================================
`lineage_level: L09_PARTIAL` 을 `METADATA_VERIFIED` 로 올리기 위한 마지막
절차다. 판정원장이 **형식상 완성**된 것과 그 근거가 **원문에서 재확인**된
것은 다른 사실이며, 지금까지는 앞의 것만 끝나 있었다.

왜 필요한가 ― 표적 추인에서 이미 1건이 뒤집혔다
  evidence/judgment_ratification_20260730/ 의 표적 재검증 6건 중 오로스
  테크놀로지 FY2024·2025 가 원문 HBM 언급 0회로 확인돼 원장 판정이
  정정됐다. 6건 중 1건이다. 나머지 209건에 같은 것이 없다는 근거는 없다.
  발견이 늦을수록 대응 비용이 커진다(원장 정정 -> 스냅샷 재생성 ->
  백테스트 재실행 -> FACTSHEET 재생성 연쇄).

독립성을 지키는 방법 ― 3단계 분리
  이 스크립트는 세 단계를 **명령으로 분리**한다. 수집하면서 대조하면
  기존 값에 맞춰 고르게 되고, 그 순간 순환검증이 되어 아무것도 증명하지
  못한다.

    1) --scope     원장에서 (종목코드, 종목명, 사업연도) 215쌍만 뽑는다.
                   판정값·접수번호·접수일은 읽지 않는다.
    2) --collect   그 범위표만 입력으로 DART 를 조회한다. 기존 원장을
                   열지 않는다. 원문·메타데이터·SHA-256 을 동결한다.
    3) --crosscheck 동결이 끝난 뒤에만 원장과 대조한다.

  2단계가 끝나기 전에는 3단계를 돌리지 말 것. 순서를 지키는 것이 이
  절차의 유일한 가치다.

무엇을 대조하는가 ― 판정이 아니라 근거
  접수번호·접수일·보고서명(원본/정정본)·원문 해시를 본다. 매출 노출도나
  메모리향 비중 같은 **내용 판정값은 재판정하지 않는다**(파트2 책임).
  다만 원문에서 HBM 언급 횟수를 함께 세어, 판정과 원문이 어긋나는
  종목·연도를 사람이 볼 수 있게 표시한다.

사용
    python analysis\\recollect_pit_evidence.py --scope
    python analysis\\recollect_pit_evidence.py --collect        # 중단 시 재실행하면 이어받음
    python analysis\\recollect_pit_evidence.py --crosscheck

산출
    evidence/recollect_<날짜>/scope.csv          범위 215쌍
    evidence/recollect_<날짜>/records.csv        재수집 메타데이터
    evidence/recollect_<날짜>/raw/<코드>_<접수번호>.txt   원문
    evidence/recollect_<날짜>/run_manifest.json  수집 계보
    evidence/recollect_<날짜>/crosscheck.csv     대조 결과
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import date

import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

LEDGER = os.path.join(HERE, "data", "verdict_ledger.csv")
HBM_KEYWORDS = ("HBM", "고대역폭", "High Bandwidth")


def _outdir(tag: str | None) -> str:
    tag = tag or date.today().strftime("%Y%m%d")
    return os.path.join(HERE, "evidence", f"recollect_{tag}")


def _digits(v) -> str:
    """숫자만 남긴다. outer merge 로 결측이 생기면 정수 컬럼이 float 으로
    승격돼 '20210323' 이 '20210323.0' 이 된다. 그대로 문자열 비교하면
    **누락 1건이 215건 전부를 오판**시키므로 여기서 정규화한다."""
    s = str(v)
    if s.endswith(".0"):
        s = s[:-2]
    return "".join(ch for ch in s if ch.isdigit())


def _sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest().upper()


# ----------------------------------------------------------------------
# 1) 범위 ― 원장에서 (코드, 이름, 사업연도)만 읽는다
# ----------------------------------------------------------------------
def build_scope(out: str) -> pd.DataFrame:
    if not os.path.exists(LEDGER):
        sys.exit(f"[FAIL] 원장이 없다: {LEDGER}")
    cols = ["ticker", "name", "fiscal_year"]          # 판정값은 읽지 않는다
    led = pd.read_csv(LEDGER, dtype={"ticker": str}, usecols=cols)
    led["ticker"] = led["ticker"].str.strip().str.zfill(6)
    scope = led.drop_duplicates(["ticker", "fiscal_year"]).sort_values(
        ["ticker", "fiscal_year"]).reset_index(drop=True)
    os.makedirs(out, exist_ok=True)
    p = os.path.join(out, "scope.csv")
    scope.to_csv(p, index=False, encoding="utf-8-sig", lineterminator="\n")
    print(f"[범위] {len(scope)}쌍 · {scope['ticker'].nunique()}종목 -> {p}")
    print("       판정값·접수번호·접수일은 읽지 않았다(독립성 유지).")
    return scope


# ----------------------------------------------------------------------
# 2) 수집 ― 범위표만 보고 DART 를 다시 조회한다
# ----------------------------------------------------------------------
def collect(out: str, sleep: float = 0.35, limit: int | None = None) -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass
    key = os.environ.get("DART_API_KEY")
    if not key:
        sys.exit("[FAIL] DART_API_KEY 가 없다(.env 또는 환경변수). 중단한다.")

    scope_path = os.path.join(out, "scope.csv")
    if not os.path.exists(scope_path):
        sys.exit(f"[FAIL] 범위표가 없다: {scope_path} ― 먼저 --scope 를 실행할 것")
    scope = pd.read_csv(scope_path, dtype={"ticker": str})

    from hbm_evidence import _clean, _make_dart, annual_report
    dart = _make_dart(key)

    raw_dir = os.path.join(out, "raw")
    os.makedirs(raw_dir, exist_ok=True)
    rec_path = os.path.join(out, "records.csv")
    done: dict = {}
    if os.path.exists(rec_path):                       # 이어받기
        prev = pd.read_csv(rec_path, dtype={"ticker": str})
        done = {(r.ticker, int(r.fiscal_year)): r._asdict()
                for r in prev.itertuples(index=False)}
        print(f"[이어받기] 기존 {len(done)}건 확인 ― 나머지만 조회한다")

    rows = list(done.values())
    todo = [(str(t).zfill(6), n, int(y)) for t, n, y in
            scope[["ticker", "name", "fiscal_year"]].itertuples(index=False, name=None)
            if (str(t).zfill(6), int(y)) not in done]
    if limit:
        todo = todo[:limit]
    print(f"[수집] 대상 {len(todo)}건 (전체 {len(scope)}건)")

    for i, (code, name, fy) in enumerate(todo, 1):
        row = {"ticker": code, "name": name, "fiscal_year": fy,
               "report_nm": "", "rcept_no": "", "rcept_dt": "",
               "source_url": "", "raw_sha256": "", "raw_bytes": 0,
               "hbm_hits": -1, "status": ""}
        try:
            got = annual_report(dart, code, fy)
        except Exception as e:                          # 조회 실패는 기록만
            got, row["status"] = None, f"ERROR:{type(e).__name__}"
        if not got:
            row["status"] = row["status"] or "NO_REPORT"
        else:
            nm, rcp, dt = got
            row.update(report_nm=nm, rcept_no=rcp, rcept_dt=dt,
                       source_url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp}")
            try:
                txt = _clean(dart.document(rcp))
                b = txt.encode("utf-8")
                fn = os.path.join(raw_dir, f"{code}_{rcp}.txt")
                with open(fn, "w", encoding="utf-8", newline="\n") as f:
                    f.write(txt)
                row.update(raw_sha256=_sha256(b), raw_bytes=len(b),
                           hbm_hits=sum(txt.count(k) for k in HBM_KEYWORDS),
                           status="OK")
            except Exception as e:
                row["status"] = f"DOC_ERROR:{type(e).__name__}"
        rows.append(row)
        if i % 10 == 0 or i == len(todo):
            pd.DataFrame(rows).to_csv(rec_path, index=False,
                                      encoding="utf-8-sig", lineterminator="\n")
            print(f"  ... {i}/{len(todo)}  최근 {code} FY{fy} {row['status']}")
        time.sleep(sleep)

    df = pd.DataFrame(rows)
    df.to_csv(rec_path, index=False, encoding="utf-8-sig", lineterminator="\n")
    man = {
        "collected_at": date.today().isoformat(),
        "scope_rows": int(len(scope)),
        "collected_rows": int(len(df)),
        "status_counts": df["status"].value_counts().to_dict(),
        "raw_files": len(os.listdir(raw_dir)),
        "selection_rule": "kind=A · final=False · '사업보고서' 포함 · 정정본 제외 후 "
                          "최초 접수일·접수번호 오름차순 1건",
        "independence": "scope.csv(코드·이름·사업연도)만 입력 · 원장 판정값·"
                        "접수번호·접수일 미참조",
        "note": "이 파일은 수집 계보만 기록한다. 원장과의 대조는 --crosscheck 가 수행한다.",
    }
    with open(os.path.join(out, "run_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=2)
    print(f"\n[수집 완료] {len(df)}건 · 상태 {man['status_counts']}")
    print(f"[동결] {rec_path}")
    print("이제 --crosscheck 를 실행하십시오(수집과 대조를 섞지 않는다).")


# ----------------------------------------------------------------------
# 3) 대조 ― 동결이 끝난 뒤에만
# ----------------------------------------------------------------------
def crosscheck(out: str) -> int:
    rec_path = os.path.join(out, "records.csv")
    if not os.path.exists(rec_path):
        sys.exit(f"[FAIL] 재수집 결과가 없다: {rec_path} ― 먼저 --collect 를 완료할 것")
    rec = pd.read_csv(rec_path, dtype={"ticker": str, "rcept_no": str})
    led = pd.read_csv(LEDGER, dtype={"ticker": str})
    led["ticker"] = led["ticker"].str.strip().str.zfill(6)
    led["led_rcept_no"] = led["source"].astype(str).str.extract(r"rcpNo=(\d+)")
    m = rec.merge(led[["ticker", "fiscal_year", "disclosed_at", "led_rcept_no",
                       "hbm_exposure", "judgment_status"]],
                  on=["ticker", "fiscal_year"], how="outer", indicator=True)

    def verdict(r) -> str:
        if r["_merge"] != "both":
            return "ONLY_" + ("RECOLLECT" if r["_merge"] == "left_only" else "LEDGER")
        if str(r.get("status")) != "OK":
            return "NOT_COLLECTED"
        same_no = _digits(r["rcept_no"]) == _digits(r["led_rcept_no"])
        same_dt = _digits(r["rcept_dt"])[:8] == _digits(r["disclosed_at"])[:8]
        if same_no and same_dt:
            return "MATCH"
        if same_no:
            return "MATCH_RCPNO_DATE"          # 접수번호 같고 날짜 표기만 상이
        return "MISMATCH"

    m["verdict"] = m.apply(verdict, axis=1)
    # 판정-원문 정합 참고: 노출도가 있는데 원문에 HBM 언급 0회면 사람이 볼 것
    m["flag_zero_hbm"] = (m["hbm_hits"].fillna(-1) == 0) & \
                         (pd.to_numeric(m["hbm_exposure"], errors="coerce").fillna(0) > 0)
    p = os.path.join(out, "crosscheck.csv")
    m.drop(columns=["_merge"]).to_csv(p, index=False, encoding="utf-8-sig",
                                      lineterminator="\n")

    vc = m["verdict"].value_counts()
    print("[대조 결과]")
    for k, v in vc.items():
        print(f"  {k:<18} {v:4d}")
    flags = int(m["flag_zero_hbm"].sum())
    if flags:
        print(f"\n[확인 요청] 노출도>0 인데 원문 HBM 언급 0회: {flags}건 "
              "― 파트2 판정 확인 대상(오로스 사례와 같은 유형)")
        cols = ["ticker", "name", "fiscal_year", "hbm_exposure", "hbm_hits"]
        print(m[m["flag_zero_hbm"]][cols].to_string(index=False))
    bad = int(vc.get("MISMATCH", 0) + vc.get("ONLY_RECOLLECT", 0)
              + vc.get("ONLY_LEDGER", 0) + vc.get("NOT_COLLECTED", 0))
    print(f"\n[저장] {p}")
    if bad == 0 and flags == 0:
        print("\n[판정] 전수 일치 ― lineage_level 을 METADATA_VERIFIED 로 "
              "승격할 수 있다(위원회 기록 후).")
        return 0
    print(f"\n[판정] 불일치·확인대상 {bad + flags}건 ― L09_PARTIAL 유지. "
          "원장을 재수집값에 맞추지 말고 사유를 먼저 기록할 것.")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", action="store_true", help="1단계: 범위 215쌍 추출")
    ap.add_argument("--collect", action="store_true", help="2단계: DART 재수집(이어받기)")
    ap.add_argument("--crosscheck", action="store_true", help="3단계: 원장 대조")
    ap.add_argument("--tag", default=None, help="산출 폴더 접미사(기본 오늘 날짜)")
    ap.add_argument("--sleep", type=float, default=0.35, help="호출 간 대기(초)")
    ap.add_argument("--limit", type=int, default=None, help="이번 실행 최대 건수(시험용)")
    a = ap.parse_args()
    out = _outdir(a.tag)
    if not (a.scope or a.collect or a.crosscheck):
        ap.error("--scope / --collect / --crosscheck 중 하나를 지정할 것")
    if a.scope:
        build_scope(out)
    if a.collect:
        collect(out, sleep=a.sleep, limit=a.limit)
    if a.crosscheck:
        return crosscheck(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
