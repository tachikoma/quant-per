import time
import pandas as pd
import numpy as np
from pykrx import stock
from tqdm import tqdm
from config import get_korean_business_days, Config


pd.set_option('future.no_silent_downcasting', True)


def fetch_rebalancing_data(start_date, end_date):
    print(f"[{start_date} ~ {end_date}] 영업일 캘린더 분석 중...")
    b_days = get_korean_business_days(start_date, end_date)
    df_days = pd.DataFrame(b_days, columns=['date'])
    df_days['year_month'] = df_days['date'].dt.to_period('M')

    first_days = df_days.groupby('year_month').first()['date'].tolist()
    last_days = df_days.groupby('year_month').last()['date'].tolist()
    target_dates = sorted(list(set(first_days + last_days)))

    all_data = []
    print(f"총 {len(target_dates)}개의 리밸런싱 타겟 영업일 데이터를 수집합니다.")
    for dt in tqdm(target_dates, desc="KRX 데이터 다운로드"):
        dt_str = dt.strftime("%Y%m%d")
        df_mcap = stock.get_market_cap(dt_str)
        df_fund = stock.get_market_fundamental(dt_str)

        if df_mcap.empty or df_fund.empty:
            continue

        if (df_mcap['종가'] == 0).all():
            continue

        df_merged = pd.concat([df_mcap, df_fund], axis=1)
        df_merged = df_merged.loc[:, ~df_merged.columns.duplicated()]

        df_merged = df_merged.reset_index()
        df_merged = df_merged.rename(columns={
            '티커': 'code', '종가': 'close', '시가총액': 'market_cap',
            '거래대금': 'trading_val', 'PER': 'per'
        })

        df_merged['date'] = dt
        df_merged['is_preferred'] = ~df_merged['code'].str.endswith('0')

        cols = ['date', 'code', 'close', 'market_cap', 'trading_val', 'per', 'is_preferred']
        all_data.append(df_merged[cols])
        time.sleep(0.3)

    final_df = pd.concat(all_data, ignore_index=True)
    return final_df


def run_backtest(market_data, config: Config):
    initial_capital = config.initial_capital
    buy_cost, sell_cost, slippage = config.buy_cost, config.sell_cost, config.slippage
    n_stocks = config.n_stocks

    market_data['year_month'] = market_data['date'].dt.to_period('M')
    unique_months = sorted(market_data['year_month'].unique())

    portfolio_value = initial_capital
    current_portfolio = []
    history = []
    total_cost_spent = 0

    for current_month in unique_months:
        month_df = market_data[market_data['year_month'] == current_month]
        if month_df.empty:
            continue

        trading_days = sorted(month_df['date'].unique())
        if len(trading_days) < 2:
            continue

        first_day = trading_days[0]
        last_day = trading_days[-1]

        month_cost = 0

        if current_portfolio:
            last_day_df = month_df[month_df['date'] == last_day]
            sell_amount = 0
            for asset in current_portfolio:
                stock_info = last_day_df[last_day_df['code'] == asset['code']]
                if stock_info.empty:
                    exec_sell_price = asset['buy_price'] * 0.1
                else:
                    exec_sell_price = stock_info.iloc[0]['close']
                final_sell_price = exec_sell_price * (1 - slippage)
                net_sell_val = (asset['shares'] * final_sell_price) * (1 - sell_cost)
                month_cost += (asset['shares'] * exec_sell_price) - net_sell_val
                sell_amount += net_sell_val
            portfolio_value = sell_amount
            total_cost_spent += month_cost

        first_day_df = month_df[month_df['date'] == first_day]

        universe = first_day_df[
            (~first_day_df['is_preferred']) &
            (first_day_df['market_cap'] >= config.min_market_cap) &
            (first_day_df['trading_val'] >= config.min_trading_val)
        ].copy()

        universe = universe[
            (universe['per'] >= config.per_min) &
            (universe['per'] <= config.per_max)
        ]

        selected_stocks = universe.sort_values(by='per', ascending=True).head(n_stocks)

        current_portfolio = []
        if len(selected_stocks) > 0:
            target_cash_per_stock = portfolio_value / len(selected_stocks)
            for _, row in selected_stocks.iterrows():
                exec_buy_price = row['close'] * (1 + slippage)
                shares = int(target_cash_per_stock / (exec_buy_price * (1 + buy_cost)))
                if shares > 0:
                    actual_buy_cost = shares * exec_buy_price * (1 + buy_cost)
                    month_cost += actual_buy_cost - (shares * row['close'])
                    current_portfolio.append({
                        'code': row['code'],
                        'shares': shares,
                        'buy_price': row['close']
                    })
            total_cost_spent += month_cost

        total_return = ((portfolio_value - initial_capital) / initial_capital) * 100
        history.append({
            "Date": str(last_day.date()),
            "Portfolio_Value": int(portfolio_value),
            "Total_Return(%)": round(total_return, 2),
            "Stock_Count": len(current_portfolio)
        })

    history_df = pd.DataFrame(history)
    history_df['Peak'] = history_df['Portfolio_Value'].cummax()
    history_df['Drawdown'] = ((history_df['Portfolio_Value'] - history_df['Peak']) / history_df['Peak']) * 100
    mdd = history_df['Drawdown'].min()

    metrics = {
        "INITIAL_CAPITAL": initial_capital,
        "FINAL_PORTFOLIO_VALUE": int(history_df['Portfolio_Value'].iloc[-1]),
        "TOTAL_RETURN_PCT": history_df['Total_Return(%)'].iloc[-1],
        "MAX_DRAWDOWN_PCT": round(mdd, 2),
        "TOTAL_COST_IMPACT_KRW": int(total_cost_spent),
    }

    return history_df, metrics
