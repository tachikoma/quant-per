from pathlib import Path
import pandas as pd
from pykrx import stock
from config import Config

DEFAULT_CACHE_DIR = Path(".cache") / "backtest"


KOSPI_CACHE_FILE = "kospi.parquet"


def fetch_kospi_benchmark(
    start_date: str, end_date: str, cache_dir=None, force_refresh=False
) -> pd.DataFrame:
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
        cached_max = existing.index.max()

        # 캐시가 end까지 덮고 있으면 캐시만 사용 (start 불일치 무관).
        # start가 캐시 시작보다 앞서면 loc 가 그 이후 첫 행부터 반환한다.
        if not pd.isna(cached_max) and cached_max >= end_ts:
            return existing.loc[start_ts:end_ts]

        # fetch 범위: 캐시 이후만. (캐시 안쪽 공휴일 누락은 재조회하지 않음)
        fetch_start = cached_max + pd.Timedelta(days=1)
        if fetch_start >= end_ts:
            return existing.loc[start_ts:end_ts]
        all_dates = pd.bdate_range(start=fetch_start, end=end_ts, freq="B")
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
    df_new = (
        df_new[["date", "종가"]]
        .rename(columns={"종가": "kospi_close"})
        .set_index("date")
    )
    df_new.index.name = "date"

    merged = pd.concat([existing, df_new])
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    merged.to_parquet(cache_file)

    return merged.loc[start_ts:end_ts]


def align_to_dates(kospi: pd.DataFrame, dates: list) -> pd.DataFrame:
    """각 평가일에 해당하는 KOSPI 종가를 반환.

    평가일과 같은 날짜가 있으면 그 값을, 없으면 직전 영업일 값을 사용한다.
    (미래 값은 사용하지 않는다 — 평가일 이후 첫 행을 쓰는 것은
    벤치마크 수익률을 미래로 어긋나게 하는 버그였다.)
    """
    rows = []
    for d in dates:
        ts = pd.Timestamp(d)
        m = kospi[kospi.index <= ts]
        close = m.iloc[-1]["kospi_close"] if not m.empty else None
        rows.append({"date": d, "kospi_close": close})
    return pd.DataFrame(rows)


def benchmark_strategy(
    history_df: pd.DataFrame, config: Config, cache_dir=None, force_refresh=False
):
    kospi = fetch_kospi_benchmark(
        config.start_date,
        config.end_date,
        cache_dir=cache_dir,
        force_refresh=force_refresh,
    )
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
    today = pd.Timestamp.now().normalize().strftime("%Y-%m-%d")
    actual_end = min(config.end_date, today)
    print(f" 테스트 기간      : {config.start_date} ~ {actual_end}")
    print(f" 초기 투자 원금   : {metrics['INITIAL_CAPITAL']:,} 원")
    print(f" 최종 자산 평가액 : {metrics['FINAL_PORTFOLIO_VALUE']:,} 원")
    print(f" 누적 최종 수익률 : {metrics['TOTAL_RETURN_PCT']}%")
    print(f" 연환산 수익률(CAGR): {metrics['CAGR_PCT']}%")
    print(f" 시스템 최대 낙폭 : {metrics['MAX_DRAWDOWN_PCT']}% (MDD)")
    print(f" 총 누적 거래 비용: {metrics['TOTAL_COST_IMPACT_KRW']:,} 원")
    portfolio_desc = f" {config.n_stocks}종목, {config.rebalance_freq}"
    if config.use_multi_factor:
        portfolio_desc = f"PBR 하위 {config.pbr_pctile:.0%}," + portfolio_desc
    print(f" 포트폴리오      :{portfolio_desc}")

    score_parts = ["모멘텀" if config.use_momentum else "PER"]
    if config.use_katsenelson:
        score_parts = ["ROIC", "FCF Yield", "EV/EBITDA", "NCAV"]
    elif config.use_multi_factor:
        score_parts += ["ROE", "배당"]
    if config.use_low_volatility:
        score_parts.append("저변동성")
    print(f" 스코어링       : {'+'.join(score_parts)} 순위 합산")
    if config.use_katsenelson:
        print(
            f" 카스넬슨 필터  : ROIC>={config.min_roic:.0%}, D/E<={config.max_debt_equity:.1f}, "
            f"이자보상>={config.min_interest_coverage:.0f}, EV/EBITDA<={config.max_ev_ebitda:.0f}"
        )
    print(f" 최대 교체율     : {config.max_turnover:.0%}")
    if config.use_momentum:
        print(f" 모멘텀          : {config.momentum_window}개월")
    if config.use_low_volatility:
        print(" 저변동성        : 포함")
    if config.fundamental_lag_months > 0:
        print(f" 재무 시차       : {config.fundamental_lag_months}개월 lag")
    print("-" * 70)

    cols = [
        "Date",
        "Portfolio_Value",
        "Stock_Count",
        "kospi_close",
        "Strategy_Return(%)",
        "KOSPI_Return(%)",
        "Alpha(%)",
    ]
    avail = [c for c in cols if c in history_df.columns]
    print(history_df[avail].tail(12).to_string(index=False))
    print("=" * 70)
