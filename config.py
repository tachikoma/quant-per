import os
from dataclasses import dataclass
from dotenv import load_dotenv
import pandas as pd
from pandas.tseries.offsets import CustomBusinessDay

load_dotenv()


def get_last_business_day() -> str:
    today = pd.Timestamp.now().normalize()
    for i in range(14):
        d = today - pd.Timedelta(days=i)
        if d.weekday() >= 5:
            continue
        # Check Korean holidays
        yr = d.year
        date_str = d.strftime("%Y-%m-%d")
        is_holiday = False
        for hset in KOREA_LUNAR_HOLIDAYS.values():
            for y in [yr - 1, yr, yr + 1]:
                if date_str in hset.get(y, []):
                    is_holiday = True
                    break
            if is_holiday:
                break
        if is_holiday:
            continue
        fixed_holidays = [
            (1, 1), (3, 1), (5, 1), (5, 5),
            (6, 6), (8, 15), (10, 3), (10, 9), (12, 25),
        ]
        for m, day in fixed_holidays:
            h = pd.Timestamp(yr, m, day)
            if h.weekday() == 6:
                h += pd.Timedelta(days=1)
            elif h.weekday() == 5 and m == 5 and day == 5:
                h += pd.Timedelta(days=2)
            if h.date() == d.date():
                is_holiday = True
                break
        if not is_holiday:
            return date_str
    return today.strftime("%Y-%m-%d")

KOREA_LUNAR_HOLIDAYS = {
    "seollal": {
        2018: ["2018-02-15", "2018-02-16"],
        2019: ["2019-02-04", "2019-02-05", "2019-02-06"],
        2020: ["2020-01-24", "2020-01-27"],
        2021: ["2021-02-11", "2021-02-12"],
        2022: ["2022-01-31", "2022-02-01", "2022-02-02"],
        2023: ["2023-01-23", "2023-01-24"],
        2024: ["2024-02-09", "2024-02-12"],
        2025: ["2025-01-28", "2025-01-29", "2025-01-30"],
        2026: ["2026-02-16", "2026-02-17", "2026-02-18"],
    },
    "chuseok": {
        2018: ["2018-09-24", "2018-09-25", "2018-09-26"],
        2019: ["2019-09-12", "2019-09-13"],
        2020: ["2020-09-30", "2020-10-01", "2020-10-02"],
        2021: ["2021-09-20", "2021-09-21", "2021-09-22"],
        2022: ["2022-09-09", "2022-09-12"],
        2023: ["2023-09-28", "2023-09-29"],
        2024: ["2024-09-16", "2024-09-17", "2024-09-18"],
        2025: ["2025-10-06", "2025-10-07"],
        2026: ["2026-09-25", "2026-09-28"],
    },
    "buddha": {
        2018: ["2018-05-22"],
        2019: ["2019-05-13"],
        2020: ["2020-04-30"],
        2021: ["2021-05-19"],
        2022: ["2022-05-09"],
        2023: ["2023-05-29"],
        2024: ["2024-05-15"],
        2025: ["2025-05-05"],
        2026: ["2026-05-25"],
    },
}


def get_korean_business_days(start_date, end_date):
    start_yr = pd.Timestamp(start_date).year
    end_yr = pd.Timestamp(end_date).year
    holiday_set = set()
    fixed = [(1, 1, "신정"), (3, 1, "삼일절"), (5, 1, "근로자의날"),
             (5, 5, "어린이날"), (6, 6, "현충일"), (8, 15, "광복절"),
             (10, 3, "개천절"), (10, 9, "한글날"), (12, 25, "크리스마스")]
    for year in range(start_yr, end_yr + 1):
        for month, day, _ in fixed:
            d = pd.Timestamp(year, month, day)
            if d.weekday() == 6:
                alt = d + pd.Timedelta(days=1)
                holiday_set.add(alt.strftime("%Y-%m-%d"))
            elif d.weekday() == 5 and month == 5 and day == 5:
                alt = d + pd.Timedelta(days=2)
                holiday_set.add(alt.strftime("%Y-%m-%d"))
            holiday_set.add(d.strftime("%Y-%m-%d"))
        for key in ["seollal", "chuseok", "buddha"]:
            for h in KOREA_LUNAR_HOLIDAYS.get(key, {}).get(year, []):
                holiday_set.add(h)
    return pd.date_range(
        start=start_date, end=end_date,
        freq=CustomBusinessDay(holidays=sorted(holiday_set))
    )


@dataclass
class Config:
    start_date: str
    end_date: str
    initial_capital: int
    buy_cost: float
    sell_cost: float
    slippage: float
    n_stocks: int
    per_min: float
    per_max: float
    min_market_cap: int
    min_trading_val: int
    rebalance_freq: str = "monthly"  # "monthly" or "quarterly"
    kospi_ticker: str = "1001"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            start_date=os.getenv("BACKTEST_START_DATE", "2018-01-01"),
            end_date=os.getenv("BACKTEST_END_DATE") or get_last_business_day(),
            initial_capital=int(os.getenv("INITIAL_CAPITAL", "100000000")),
            buy_cost=float(os.getenv("BUY_COST", "0.00015")),
            sell_cost=float(os.getenv("SELL_COST", "0.0023")),
            slippage=float(os.getenv("SLIPPAGE", "0.002")),
            n_stocks=int(os.getenv("N_STOCKS", "30")),
            per_min=float(os.getenv("PER_MIN", "0.01")),
            per_max=float(os.getenv("PER_MAX", "4.00")),
            min_market_cap=int(os.getenv("MIN_MARKET_CAP", "50000000000")),
            min_trading_val=int(os.getenv("MIN_TRADING_VAL", "1000000000")),
            rebalance_freq=os.getenv("REBALANCE_FREQ", "monthly"),
        )
