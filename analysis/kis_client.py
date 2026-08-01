# -*- coding: utf-8 -*-
"""
kis_client.py ― 한국투자증권 KIS Developers REST 클라이언트
=============================================================
KRX Data Marketplace 접근 제한(2026-07-31 통보) 이후의 **공식 대체 경로**다.
KIS Open API는 이용약관상 허용된 자동화 경로이며 인증키로 접근한다.

무엇을 하는가 / 하지 않는가
  한다     : OAuth 토큰 발급·디스크 캐시, 일별 수정주가 종가·거래대금
             수집(기간별시세 FHKST03010100, 100행 페이지네이션)
  하지 않는다: 과거 시점 시가총액. KIS 는 상장주식수 **이력**을 주지 않아
             과거 시총을 정확히 산출할 수 없다(현재 주식수 × 과거 종가는
             증자·분할 구간에서 틀린다). 시총 스냅샷은 공공데이터포털
             금융위 API 또는 KRX 화면 수동 다운로드로 보완한다.

설계 원칙 (프로젝트 공통)
  fail-closed ― rt_cd != "0" 이면 예외로 중단한다. 조용히 빈 값을 돌려
  주지 않는다. 재시도는 일시 장애(HTTP 5xx·타임아웃)에만 하고, 응답이
  명시적 오류면 재시도하지 않는다.

토큰
  /oauth2/tokenP 는 발급 빈도 제한이 있다(잦은 요청 시 EGW00133).
  발급받은 토큰(유효 24시간)을 파일로 캐시하고 만료 10분 전까지 재사용
  한다. 캐시 파일 권한은 0600 으로 둔다.

환경 변수
  KIS_APP_KEY / KIS_APP_SECRET ― KIS Developers 에서 발급한 앱키·시크릿.
  코드·커밋·로그에 키를 남기지 않는다.

사용
    from analysis.kis_client import KISClient
    c = KISClient.from_env()
    df = c.daily_prices("005930", "20200601", "20260723")
    # -> index: 날짜(DatetimeIndex) · columns: close, value, volume
"""
from __future__ import annotations

import json
import os
import stat
import time

import pandas as pd
import requests

REAL_BASE = "https://openapi.koreainvestment.com:9443"
PAPER_BASE = "https://openapivts.koreainvestment.com:29443"

#: 기간별시세(일/주/월/년) ― 실전·모의 공통 tr_id
TR_DAILY_CHART = "FHKST03010100"

#: 한 번의 호출이 돌려주는 최대 행수 (KIS 명세)
PAGE_ROWS = 100

#: 초당 호출 상한. 계정 한도(실전 20건/초)보다 훨씬 보수적으로 둔다.
#: 시세 조회는 급하지 않고, 한도 근처에서 돌리면 오류 처리가 시끄러워진다.
DEFAULT_RATE_PER_SEC = 3.0

#: 일시 장애 재시도 횟수·초기 대기(지수 백오프)
RETRIES = 3
BACKOFF0 = 2.0


class KISError(RuntimeError):
    """KIS 가 명시적으로 거절한 응답. 재시도하지 않는다."""


def _default_token_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".kis_token.json")


class KISClient:
    def __init__(self, appkey: str, appsecret: str, *,
                 base: str = REAL_BASE,
                 token_cache: str | None = None,
                 rate_per_sec: float = DEFAULT_RATE_PER_SEC,
                 session: requests.Session | None = None):
        if not appkey or not appsecret:
            raise KISError("앱키/시크릿이 비어 있다 - KIS_APP_KEY·KIS_APP_SECRET 확인")
        self.appkey = appkey
        self.appsecret = appsecret
        self.base = base.rstrip("/")
        self.token_cache = token_cache or _default_token_path()
        self.min_interval = 1.0 / max(rate_per_sec, 0.1)
        self.sess = session or requests.Session()
        self._last_call = 0.0
        self._token: str | None = None
        self._token_exp: float = 0.0

    @classmethod
    def from_env(cls, **kw) -> "KISClient":
        return cls(os.environ.get("KIS_APP_KEY", ""),
                   os.environ.get("KIS_APP_SECRET", ""), **kw)

    # ------------------------------------------------------------ 토큰
    def _load_cached_token(self) -> None:
        try:
            with open(self.token_cache, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("base") == self.base and d.get("appkey_tail") == self.appkey[-4:]:
                self._token, self._token_exp = d["token"], float(d["exp"])
        except (OSError, ValueError, KeyError):
            pass

    def _save_cached_token(self) -> None:
        d = {"token": self._token, "exp": self._token_exp,
             "base": self.base, "appkey_tail": self.appkey[-4:]}
        with open(self.token_cache, "w", encoding="utf-8") as f:
            json.dump(d, f)
        try:
            os.chmod(self.token_cache, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        except OSError:
            pass

    def token(self) -> str:
        """유효 토큰을 돌려준다. 만료 10분 전이면 재발급."""
        if self._token is None:
            self._load_cached_token()
        if self._token and time.time() < self._token_exp - 600:
            return self._token
        r = self.sess.post(
            self.base + "/oauth2/tokenP",
            json={"grant_type": "client_credentials",
                  "appkey": self.appkey, "appsecret": self.appsecret},
            timeout=15)
        try:
            body = r.json()
        except ValueError:
            raise KISError(f"토큰 응답이 JSON 이 아니다 (HTTP {r.status_code})")
        if r.status_code != 200 or "access_token" not in body:
            raise KISError(
                f"토큰 발급 거절 (HTTP {r.status_code}) "
                f"{body.get('error_code', '')} {body.get('error_description', body)}"
                " - 발급 빈도 제한(분당 1회)일 수 있으니 잠시 후 재시도")
        self._token = body["access_token"]
        self._token_exp = time.time() + float(body.get("expires_in", 86400))
        self._save_cached_token()
        return self._token

    # ------------------------------------------------------------ 공통 호출
    def _throttle(self) -> None:
        wait = self._last_call + self.min_interval - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def _get(self, path: str, tr_id: str, params: dict) -> dict:
        """GET 1회. 5xx·타임아웃만 재시도, 명시적 오류는 즉시 중단."""
        headers = {"content-type": "application/json; charset=utf-8",
                   "authorization": "Bearer " + self.token(),
                   "appkey": self.appkey, "appsecret": self.appsecret,
                   "tr_id": tr_id, "custtype": "P"}
        last = None
        for attempt in range(RETRIES + 1):
            self._throttle()
            try:
                r = self.sess.get(self.base + path, headers=headers,
                                  params=params, timeout=15)
            except requests.RequestException as e:
                last = f"전송 실패: {e}"
                time.sleep(BACKOFF0 * (2 ** attempt))
                continue
            if r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                time.sleep(BACKOFF0 * (2 ** attempt))
                continue
            try:
                body = r.json()
            except ValueError:
                raise KISError(f"응답이 JSON 이 아니다 (HTTP {r.status_code})")
            # 토큰 만료(EGW00121)면 1회에 한해 갱신 후 즉시 재호출
            if body.get("msg_cd") == "EGW00121" and attempt == 0:
                self._token = None
                headers["authorization"] = "Bearer " + self.token()
                continue
            if r.status_code != 200 or body.get("rt_cd") != "0":
                raise KISError(
                    f"KIS 거절 (HTTP {r.status_code}) rt_cd={body.get('rt_cd')} "
                    f"{body.get('msg_cd', '')} {body.get('msg1', '')}".strip())
            return body
        raise KISError(f"일시 장애 재시도 {RETRIES}회 초과 - 마지막: {last}")

    # ------------------------------------------------------------ 일별 시세
    def daily_prices(self, ticker: str, start: str, end: str,
                     adjusted: bool = True) -> pd.DataFrame:
        """일별 종가·거래대금·거래량. index 날짜 오름차순.

        FHKST03010100 은 한 호출에 최근 100행까지만 준다. 돌려받은 가장
        이른 날짜의 전일로 종료일을 옮겨 가며 start 까지 거슬러 간다.
        상장 전 구간은 행이 없으므로 빈 페이지가 나오면 멈춘다.
        """
        t = str(ticker).zfill(6)
        s = pd.to_datetime(start, format="%Y%m%d")
        cur_end = pd.to_datetime(end, format="%Y%m%d")
        frames = []
        while cur_end >= s:
            body = self._get(
                "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                TR_DAILY_CHART,
                {"FID_COND_MRKT_DIV_CODE": "J",
                 "FID_INPUT_ISCD": t,
                 "FID_INPUT_DATE_1": s.strftime("%Y%m%d"),
                 "FID_INPUT_DATE_2": cur_end.strftime("%Y%m%d"),
                 "FID_PERIOD_DIV_CODE": "D",
                 "FID_ORG_ADJ_PRC": "0" if adjusted else "1"})
            rows = [r for r in body.get("output2", [])
                    if r and r.get("stck_bsop_date")]
            if not rows:
                break                       # 상장 전 또는 데이터 없음
            df = pd.DataFrame(rows)
            frames.append(df)
            oldest = pd.to_datetime(df["stck_bsop_date"].min(),
                                    format="%Y%m%d")
            if oldest <= s or len(rows) < PAGE_ROWS:
                break
            cur_end = oldest - pd.Timedelta(days=1)
        if not frames:
            return pd.DataFrame(columns=["close", "value", "volume"])
        out = pd.concat(frames, ignore_index=True)
        out["date"] = pd.to_datetime(out["stck_bsop_date"], format="%Y%m%d")
        out = (out.drop_duplicates("date")
                  .set_index("date").sort_index())
        res = pd.DataFrame({
            "close": pd.to_numeric(out["stck_clpr"], errors="coerce"),
            "value": pd.to_numeric(out.get("acml_tr_pbmn"), errors="coerce"),
            "volume": pd.to_numeric(out.get("acml_vol"), errors="coerce"),
        })
        res = res.loc[(res.index >= s) & (res.index <= pd.to_datetime(
            end, format="%Y%m%d"))]
        # 0원 종가는 결측으로 간주 - run_backtest.fetch_prices 와 같은 규약
        res.loc[res["close"] == 0.0, "close"] = float("nan")
        return res
