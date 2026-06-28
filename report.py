from pathlib import Path
import pandas as pd
import numpy as np
from pykrx import stock
from config import Config

DEFAULT_CACHE_DIR = Path(".cache") / "backtest"


KOSPI_CACHE_FILE = "kospi.parquet"


def fetch_kospi_benchmark(start_date: str, end_date: str, cache_dir=None, force_refresh=False) -> pd.DataFrame:
    cache_base = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    cache_base.mkdir(parents=True, exist_ok=True)
    cache_file = cache_base / KOSPI_CACHE_FILE

    existing = pd.DataFrame()
    if not force_refresh and cache_file.exists():
        try:
            existing = pd.read_parquet(cache_file)
            if not existing.empty and existing.index.name != "date":
                existing = existing.set_index("date")
        except Exception:
            existing = pd.DataFrame()

    start_ts = pd.Timestamp(start_date)
    end_ts = pd.Timestamp(end_date)

    if not existing.empty:
        existing = existing[~existing.index.duplicated(keep="last")]
        cached_min = existing.index.min()
        cached_max = existing.index.max()

        if cached_min <= start_ts and cached_max >= end_ts:
            return existing.loc[start_ts:end_ts]

        # Find missing ranges
        all_dates = pd.bdate_range(start=start_ts, end=end_ts, freq="B")
        cached_dates = existing.index.unique()
        missing = all_dates[~all_dates.isin(cached_dates)]
    else:
        missing = pd.bdate_range(start=start_ts, end=end_ts, freq="B")

    if missing.empty:
        return existing.loc[start_ts:end_ts]

    fetch_start = missing.min().strftime("%Y%m%d")
    fetch_end = missing.max().strftime("%Y%m%d")

    raw = stock.get_index_ohlcv_by_date(fetch_start, fetch_end, "1001")
    df_new = raw.reset_index()
    df_new["date"] = pd.to_datetime(df_new["날짜"])
    df_new = df_new[["date", "종가"]].rename(columns={"종가": "kospi_close"}).set_index("date")
    df_new.index.name = "date"

    merged = pd.concat([existing, df_new])
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    merged.to_parquet(cache_file)

    return merged.loc[start_ts:end_ts]


def align_to_dates(kospi: pd.DataFrame, dates: list) -> pd.DataFrame:
    rows = []
    for d in dates:
        m = kospi[kospi.index >= pd.Timestamp(d)]
        close = m.iloc[0]["kospi_close"] if not m.empty else None
        rows.append({"date": d, "kospi_close": close})
    return pd.DataFrame(rows)


def benchmark_strategy(history_df: pd.DataFrame, config: Config, cache_dir=None, force_refresh=False):
    kospi = fetch_kospi_benchmark(config.start_date, config.end_date, cache_dir=cache_dir, force_refresh=force_refresh)
    dates = pd.to_datetime(history_df["Date"])
    k_vals = align_to_dates(kospi, dates)

    history = history_df.copy()
    history["kospi_close"] = k_vals["kospi_close"].values
    k0 = history["kospi_close"].iloc[0]

    history["Strategy_Return(%)"] = history["Total_Return(%)"]
    history["KOSPI_Return(%)"] = ((history["kospi_close"] / k0) - 1) * 100
    history["Alpha(%)"] = history["Strategy_Return(%)"] - history["KOSPI_Return(%)"]

    return history


def print_report(history_df: pd.DataFrame, metrics: dict, config: Config):
    print("=" * 70)
    print("                       백테스트 결과 리포트")
    print("=" * 70)
    print(f" 테스트 기간      : {config.start_date} ~ {config.end_date}")
    print(f" 초기 투자 원금   : {metrics['INITIAL_CAPITAL']:,} 원")
    print(f" 최종 자산 평가액 : {metrics['FINAL_PORTFOLIO_VALUE']:,} 원")
    print(f" 누적 최종 수익률 : {metrics['TOTAL_RETURN_PCT']}%")
    print(f" 연환산 수익률(CAGR): {metrics['CAGR_PCT']}%")
    print(f" 시스템 최대 낙폭 : {metrics['MAX_DRAWDOWN_PCT']}% (MDD)")
    print(f" 총 누적 거래 비용: {metrics['TOTAL_COST_IMPACT_KRW']:,} 원")
    print("-" * 70)

    cols = ["Date", "Portfolio_Value", "Stock_Count", "kospi_close",
            "Strategy_Return(%)", "KOSPI_Return(%)", "Alpha(%)"]
    avail = [c for c in cols if c in history_df.columns]
    print(history_df[avail].tail(12).to_string(index=False))
    print("=" * 70)
