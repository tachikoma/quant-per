"""DART(금융감독원 전자공시) OpenAPI 기반 재무제표 데이터 수집 모듈.

- corp_code(8자리) ↔ KRX ticker(6자리) 매핑 (corpCode.xml ZIP 파싱)
- 단일회사 전체 재무제표 (fnlttSinglAcntAll.json, BS/IS/CF)
- 공시일 기반 look-ahead bias 방지 (사업보고서 = 다음해 4월 15일 이후 사용 가능)
- Parquet 캐싱: .cache/backtest/dart_{name}.parquet
"""

import io
import time
import zipfile
from pathlib import Path

import pandas as pd
import requests
from tqdm import tqdm

from config import Config

DEFAULT_CACHE_DIR = Path(".cache") / "backtest"

DART_BASE = "https://opendart.fss.or.kr/api"

# ── 공시 보고서 코드 ──
RPT_ANNUAL = "11011"  # 사업보고서 (연간)
RPT_SEMI = "11012"  # 반기보고서
RPT_Q1 = "11013"  # 1분기보고서
RPT_Q3 = "11014"  # 3분기보고서

# 사업연도 말(결산월) 기준으로 보고서 공시 후 사용 가능한 달 (1=1월, 4=4월 15일)
# 결산월 12월 → 다음해 4월, 3월 → 6월, 6월 → 9월, 9월 → 12월
_RPT_AVAILABLE_MONTH = {
    RPT_ANNUAL: 4,
    RPT_Q1: 6,
    RPT_SEMI: 9,
    RPT_Q3: 12,
}


class DartDataError(Exception):
    pass


def _get_api_key() -> str:
    key = Config.from_env().dart_api_key
    if not key:
        raise DartDataError("DART_API_KEY가 .env에 설정되지 않았습니다.")
    return key


def _dart_get(endpoint: str, params: dict) -> dict:
    key = _get_api_key()
    resp = requests.get(
        f"{DART_BASE}/{endpoint}",
        params={"crtfc_key": key, **params},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") not in ("000", None):
        # 013 (데이터 없음)은 정상적인 빈 결과로 처리
        if data.get("status") == "013":
            return {"list": []}
        raise DartDataError(f"DART API 오류 ({endpoint}): {data.get('message')}")
    return data


# ══════════════════════════════════════════════════════════════════
# 1. corp_code ↔ ticker 매핑
# ══════════════════════════════════════════════════════════════════
def _fetch_corp_codes_raw(api_key: str) -> pd.DataFrame:
    resp = requests.get(
        f"{DART_BASE}/corpCode.xml", params={"crtfc_key": api_key}, timeout=60
    )
    resp.raise_for_status()
    if resp.content[:2] != b"PK":
        raise DartDataError("corpCode.xml 응답이 ZIP이 아닙니다.")

    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        xml_name = zf.namelist()[0]
        with zf.open(xml_name) as f:
            parsed = pd.read_xml(f)

    df = pd.DataFrame(parsed).rename(
        columns={"corp_code": "corp_code", "stock_code": "ticker"}
    )
    df["corp_code"] = df["corp_code"].astype(str).str.zfill(8)
    df["ticker"] = df["ticker"].fillna("").astype(str).str.strip()
    df = df[df["ticker"].str.fullmatch(r"\d{6}")].copy()
    out = pd.DataFrame(df[["corp_code", "ticker", "corp_name"]])
    return out.reset_index(drop=True)


def fetch_corp_codes(cache_dir=None, force_refresh=False) -> pd.DataFrame:
    """DART corp_code(8) ↔ ticker(6) 매핑 테이블 (캐싱)."""
    cache_base = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    cache_file = cache_base / "dart_corp_codes.parquet"

    if cache_file.exists() and not force_refresh:
        try:
            return pd.read_parquet(cache_file)
        except Exception:
            pass

    key = _get_api_key()
    df = _fetch_corp_codes_raw(key)
    cache_base.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_file, index=False)
    return df


def ticker_to_corp_code(cache_dir=None, force_refresh=False) -> dict:
    df = fetch_corp_codes(cache_dir=cache_dir, force_refresh=force_refresh)
    return dict(zip(df["ticker"], df["corp_code"]))


# ══════════════════════════════════════════════════════════════════
# 2. 재무제표 수집 (fnlttSinglAcntAll)
# ══════════════════════════════════════════════════════════════════
def _fetch_financials(
    corp_code: str, bsns_year: str, reprt_code: str, fs_div: str
) -> pd.DataFrame:
    data = _dart_get(
        "fnlttSinglAcntAll.json",
        {
            "corp_code": corp_code,
            "bsns_year": bsns_year,
            "reprt_code": reprt_code,
            "fs_div": fs_div,
        },
    )
    return pd.DataFrame(data.get("list", []))


def _normalize_sj_div(sj_div: str) -> str:
    """DART sj_div를 표준 구분으로 정규화.

    실제 응답은 'IS', 'IS1', 'IS2', 'CIS', 'CIS1', 'BS1', 'CF1' 등 다양하다.
    - IS/CIS → 'IS' (손익계산서/포괄손익계산서)
    - BS → 'BS'
    - CF → 'CF'
    """
    s = str(sj_div).upper().strip()
    if s.startswith("BS"):
        return "BS"
    if s.startswith("CIS"):
        return "IS"
    if s.startswith("IS"):
        return "IS"
    if s.startswith("CF"):
        return "CF"
    if s.startswith("SCE"):
        return "SCE"
    return s


def _extract_accounts(df: pd.DataFrame) -> dict:
    """fnlttSinglAcntAll 응답에서 주요 계정 금액(당기)을 추출.

    Returns:
        {계정키: 금액(원 단위)} — sj_div 정규화 후 계정명으로 매핑.
    """
    if df is None or df.empty:
        return {}
    rows = pd.DataFrame(df[["sj_div", "account_nm", "thstrm_amount"]])
    rows["thstrm_amount"] = pd.to_numeric(rows["thstrm_amount"], errors="coerce")
    rows = rows[rows["thstrm_amount"].notna()]

    accounts = {}
    for _, r in rows.iterrows():
        nm = str(r["account_nm"]).strip()
        div = _normalize_sj_div(str(r["sj_div"]))
        accounts.setdefault(div, {})[nm] = r["thstrm_amount"]
    return accounts


# 주기적으로 필요한 계정 키 (K-IFRS 표준 계정명 + 관용적 변형)
ACCOUNT_KEYS = {
    "BS": {
        "cash": ["현금및현금성자산", "현금및현금등가물"],
        "current_assets": ["유동자산"],
        "total_assets": ["자산총계"],
        "total_liabilities": ["부채총계"],
        "current_liabilities": ["유동부채"],
        "total_equity": ["자본총계", "자본합계"],
        "borrowings": ["차입금", "단기차입금", "장기차입금", "사채", "총차입금"],
        "tangible_assets": ["유형자산", "유형자산합계"],
    },
    "IS": {
        "revenue": ["매출액", "수익(매출액)", "영업수익", "수익"],
        "operating_income": ["영업이익", "영업이익(손실)", "영업손실"],
        "net_income": [
            "당기순이익",
            "반기순이익",
            "분기순이익",
            "당기순이익(손실)",
            "반기순이익(손실)",
            "분기순이익(손실)",
            "지배기업의 소유주에게 귀속되는 당기순이익",
            "지배기업의 소유주에게 귀속되는 당기순이익(손실)",
        ],
        "interest_expense": ["이자의 지급", "이자비용", "금융원가", "이자지급"],
        "depreciation": [
            "감가상각비",
            "무형자산상각비",
            "감가상각비(영업이익)",
            "유형자산감가상각비",
        ],
    },
    "CF": {
        "operating_cf": [
            "영업활동현금흐름",
            "영업활동으로인한현금흐름",
            "영업활동 현금흐름",
        ],
        "investing_cf": [
            "투자활동현금흐름",
            "투자활동으로인한현금흐름",
            "투자활동 현금흐름",
        ],
        "capex": [
            "유형자산의 취득",
            "유형자산의취득",
            "유형자산취득",
            "기계장치의취득",
            "유형자산취득액",
        ],
        "interest_paid": ["이자의 지급", "이자비용의 지급", "이자지급"],
    },
}


def _pick(account_map: dict, keys: list) -> float | None:
    for k in keys:
        if k in account_map:
            return account_map[k]
    return None


def _extract_financial_row(raw: dict) -> dict:
    """fnlttSinglAcntAll 원본 dict({sj_div: {계정명: 금액}}) → 지표용 flat dict."""
    out = {}
    for sj_div, keys_map in ACCOUNT_KEYS.items():
        acct_map = raw.get(sj_div, {})
        for out_key, keys in keys_map.items():
            out[out_key] = _pick(acct_map, keys)
    return out


def fetch_annual_financials(
    corp_code: str, year: int, fs_div="CFS", cache_dir=None, force_refresh=False
) -> dict:
    """특정 연도의 사업보고서(연간) 재무제표를 가져온다 (캐싱).

    Returns:
        {계정키: 금액} 형태의 flat dict. 없으면 {}.
    """
    cache_base = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    cache_file = cache_base / "dart_data" / f"{corp_code}.parquet"

    cached_rows = {}
    if cache_file.exists() and not force_refresh:
        try:
            df = pd.read_parquet(cache_file)
            row = df[df["year"] == year]
            if not row.empty:
                cached_rows = row.iloc[0].drop(labels=["year"]).to_dict()
        except Exception:
            pass
    if cached_rows:
        return cached_rows

    df = _fetch_financials(corp_code, str(year), RPT_ANNUAL, fs_div)
    accounts = _extract_accounts(df)

    # 연결(CFS) 실패 시 개별(OFS)로 폴백
    if not accounts:
        df = _fetch_financials(corp_code, str(year), RPT_ANNUAL, "OFS")
        accounts = _extract_accounts(df)

    result = _extract_financial_row(accounts) if accounts else {}

    cache_file.parent.mkdir(parents=True, exist_ok=True)
    new_row = pd.DataFrame([{"year": year, **result}])
    if cache_file.exists():
        existing = pd.read_parquet(cache_file)
        existing = existing[existing["year"] != year]
        if existing.empty:
            combined = new_row
        else:
            combined = pd.concat([existing, new_row], ignore_index=True, sort=False)
    else:
        combined = new_row
    combined.to_parquet(cache_file, index=False)

    return result


def fetch_all_annual_financials(
    corp_codes: list[str],
    years: list[int],
    cache_dir=None,
    force_refresh=False,
    max_per_minute=900,
) -> pd.DataFrame:
    """여러 corp_code의 연간 재무제표를 배치 수집 (분당 호출 제한 준수)."""
    results = []
    calls = 0
    t0 = time.time()
    for code in tqdm(corp_codes, desc="DART 재무제표 수집"):
        for year in years:
            # 분당 호출 제한
            if calls >= max_per_minute:
                elapsed = time.time() - t0
                if elapsed < 60:
                    time.sleep(60 - elapsed)
                calls, t0 = 0, time.time()
            row = fetch_annual_financials(
                code, year, cache_dir=cache_dir, force_refresh=force_refresh
            )
            calls += 1
            if row:
                results.append({"corp_code": code, "year": year, **row})

    return pd.DataFrame(results)


# ══════════════════════════════════════════════════════════════════
# 3. 공시일 기반 사용 가능 시점 (look-ahead bias 방지)
# ══════════════════════════════════════════════════════════════════
def available_from(bsns_year: int, reprt_code: str = RPT_ANNUAL) -> pd.Timestamp:
    """해당 사업연도 재무제표가 실제 사용 가능한 시점.

    사업보고서(연간, 결산 12월)는 다음해 4월 15일부터 사용 가능.
    즉 fiscal year N의 재무제표는 year N+1의 month부터 사용 가능.
    """
    month = _RPT_AVAILABLE_MONTH.get(reprt_code, 4)
    return pd.Timestamp(year=bsns_year + 1, month=int(month), day=15, tz=None)


# ══════════════════════════════════════════════════════════════════
# 4. market_data 병합 (look-ahead bias 방지 포함)
# ══════════════════════════════════════════════════════════════════
def load_dart_financials_cache(cache_dir=None) -> pd.DataFrame:
    """캐시된 모든 종목별 재무제표를 단일 DataFrame으로 로드."""
    cache_base = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    dart_dir = cache_base / "dart_data"
    if not dart_dir.exists():
        return pd.DataFrame()
    parts = []
    for f in dart_dir.glob("*.parquet"):
        try:
            df = pd.read_parquet(f)
            df["corp_code"] = f.stem
            parts.append(df)
        except Exception:
            continue
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True, sort=False)


def merge_dart_financials(market_data: pd.DataFrame, cache_dir=None) -> pd.DataFrame:
    """market_data에 DART 연간 재무제표를 look-ahead bias 없이 병합.

    - 각 (날짜, 티커) 행에 대해 '공시 사용 가능 시점(available_from) ≤ 날짜'인
      가장 최신 사업보고서를 backward merge_asof로 매칭.
    - financial 지표(raw 계정 금액)를 추가 열로 붙인다. metrics.py로 지표 계산.
    - DART 데이터가 없으면 원본 그대로 반환 (카스넬슨 미사용 시 비용 0).
    """
    if market_data.empty:
        return market_data

    fin = load_dart_financials_cache(cache_dir)
    if fin.empty:
        return market_data

    codes = fetch_corp_codes(cache_dir=cache_dir)
    if codes.empty:
        return market_data

    fin = fin.merge(codes[["corp_code", "ticker"]], on="corp_code", how="left")
    fin = fin.dropna(subset=["ticker"]).copy()

    fin["available_from"] = pd.to_datetime(fin["year"].apply(available_from))
    fin = fin[fin["available_from"].notna()]
    if fin.empty:
        return market_data

    fin_cols = [
        "cash",
        "current_assets",
        "total_liabilities",
        "total_equity",
        "borrowings",
        "revenue",
        "operating_income",
        "net_income",
        "interest_paid",
        "depreciation",
        "operating_cf",
        "investing_cf",
        "capex",
    ]
    fin_cols = [c for c in fin_cols if c in fin.columns]

    left = pd.DataFrame(market_data[["date", "code"]])
    left["date_ts"] = pd.to_datetime(left["date"])
    left = left.sort_values("date_ts")

    right = pd.DataFrame(fin[["ticker", "available_from"] + fin_cols])
    right = right.rename(columns={"available_from": "date_ts", "ticker": "code"})
    right = right.sort_values("date_ts")

    merged = pd.merge_asof(
        left,
        right,
        on="date_ts",
        by="code",
        direction="backward",
        allow_exact_matches=True,
    )

    out = market_data.copy().reset_index(drop=True)
    fin_map = pd.DataFrame(merged[fin_cols])
    for c in fin_cols:
        out[c] = fin_map[c].reset_index(drop=True)
    return out
