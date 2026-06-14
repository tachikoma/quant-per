import pandas as pd
import numpy as np
from pykrx import stock
from config import Config


def fetch_kospi_benchmark(start_date: str, end_date: str) -> pd.DataFrame:
    raw = stock.get_index_ohlcv_by_date(
        start_date.replace("-", ""),
        end_date.replace("-", ""),
        "1001"
    )
    df = raw.reset_index()
    df["date"] = pd.to_datetime(df["날짜"])
    return df[["date", "종가"]].rename(columns={"종가": "kospi_close"}).set_index("date")


def align_to_dates(kospi: pd.DataFrame, dates: list) -> pd.DataFrame:
    rows = []
    for d in dates:
        m = kospi[kospi.index >= pd.Timestamp(d)]
        close = m.iloc[0]["kospi_close"] if not m.empty else None
        rows.append({"date": d, "kospi_close": close})
    return pd.DataFrame(rows)


def benchmark_strategy(history_df: pd.DataFrame, config: Config):
    kospi = fetch_kospi_benchmark(config.start_date, config.end_date)
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
    print(f" 시스템 최대 낙폭 : {metrics['MAX_DRAWDOWN_PCT']}% (MDD)")
    print(f" 총 누적 거래 비용: {metrics['TOTAL_COST_IMPACT_KRW']:,} 원")
    print("-" * 70)

    cols = ["Date", "Portfolio_Value", "Stock_Count", "kospi_close",
            "Strategy_Return(%)", "KOSPI_Return(%)", "Alpha(%)"]
    avail = [c for c in cols if c in history_df.columns]
    print(history_df[avail].tail(12).to_string(index=False))
    print("=" * 70)
