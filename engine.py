import json
import time
from pathlib import Path
import pandas as pd
from pykrx import stock
from tqdm import tqdm
from config import get_korean_business_days, Config


pd.set_option('future.no_silent_downcasting', True)

DEFAULT_CACHE_DIR = Path(".cache") / "backtest"


def _load_cache_index(cache_dir: Path) -> dict:
    index_file = cache_dir / "cache_index.json"
    if not index_file.exists():
        return {"market_data": {"months": {}}}
    try:
        return json.loads(index_file.read_text())
    except (json.JSONDecodeError, KeyError):
        return {"market_data": {"months": {}}}


def _save_cache_index(cache_dir: Path, index: dict) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    index_file = cache_dir / "cache_index.json"
    index_file.write_text(json.dumps(index, indent=2, ensure_ascii=False))


def fetch_rebalancing_data(start_date, end_date, cache_dir=None, force_refresh=False):
    print(f"[{start_date} ~ {end_date}] 영업일 캘린더 분석 중...")
    b_days = get_korean_business_days(start_date, end_date)
    df_days = pd.DataFrame(b_days, columns=['date'])
    df_days['year_month'] = df_days['date'].dt.to_period('M')

    first_days = df_days.groupby('year_month').first()['date'].tolist()
    last_days = df_days.groupby('year_month').last()['date'].tolist()
    target_dates = sorted(list(set(first_days + last_days)))

    today = pd.Timestamp.now().normalize()
    target_dates = [d for d in target_dates if d < today]

    if not target_dates:
        print("  데이터를 조회할 수 있는 과거 영업일이 없습니다.")
        return pd.DataFrame()

    df_target = pd.DataFrame(target_dates, columns=['date'])
    df_target['year_month'] = df_target['date'].dt.to_period('M')
    year_months = sorted(df_target['year_month'].unique())

    cache_base = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    market_cache_dir = cache_base / "market_data"

    if force_refresh and market_cache_dir.exists():
        import shutil
        shutil.rmtree(market_cache_dir)
        _save_cache_index(cache_base, {"market_data": {"months": {}}})

    market_cache_dir.mkdir(parents=True, exist_ok=True)
    cache_index = _load_cache_index(cache_base)

    months_to_fetch = []
    months_cached = []

    for ym in year_months:
        ym_str = str(ym)
        if (market_cache_dir / f"{ym_str}.parquet").exists():
            months_cached.append(ym_str)
        else:
            months_to_fetch.append(ym_str)

    if months_to_fetch:
        fetch_ym_set = set(months_to_fetch)
        fetch_dates = [
            d for d in target_dates
            if str(pd.Timestamp(d).to_period('M')) in fetch_ym_set
        ]

        print(f"총 {len(fetch_dates)}개 타겟 영업일 데이터를 수집합니다 (신규/갱신 월: {len(months_to_fetch)}개월)")
        for dt in tqdm(fetch_dates, desc="KRX 데이터 다운로드"):
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
                '거래대금': 'trading_val', 'PER': 'per', 'PBR': 'pbr',
                'DIV': 'div', 'BPS': 'bps', 'EPS': 'eps'
            })
            df_merged['date'] = dt
            df_merged['code'] = df_merged['code'].astype(str)
            df_merged['is_preferred'] = ~df_merged['code'].str.endswith('0')

            cols = ['date', 'code', 'close', 'market_cap', 'trading_val', 'per', 'pbr', 'div', 'bps', 'eps', 'is_preferred']
            df_merged = df_merged[cols]

            ym_str = str(pd.Timestamp(dt).to_period('M'))
            month_file = market_cache_dir / f"{ym_str}.parquet"

            if month_file.exists():
                existing = pd.read_parquet(month_file)
                existing = existing[existing['date'] != dt]
                df_merged = pd.concat([existing, df_merged], ignore_index=True)

            df_merged.to_parquet(month_file, index=False)
            cache_index.setdefault("market_data", {}).setdefault("months", {})[ym_str] = {
                "cached_at": pd.Timestamp.now().isoformat()
            }

            time.sleep(0.3)

        _save_cache_index(cache_base, cache_index)

    all_parts = []
    for ym in year_months:
        ym_str = str(ym)
        cache_file = market_cache_dir / f"{ym_str}.parquet"
        if cache_file.exists():
            try:
                all_parts.append(pd.read_parquet(cache_file))
            except Exception:
                pass

    final_df = pd.concat(all_parts, ignore_index=True) if all_parts else pd.DataFrame()
    return final_df


def clear_cache(cache_dir=None):
    cache_base = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    if cache_base.exists():
        import shutil
        shutil.rmtree(cache_base)
        print(f"캐시 디렉토리 삭제 완료: {cache_base}")
    else:
        print("캐시 디렉토리가 존재하지 않습니다.")


def run_backtest(market_data, config: Config):
    initial_capital = config.initial_capital
    buy_cost, sell_cost, slippage = config.buy_cost, config.sell_cost, config.slippage
    n_stocks = config.n_stocks

    market_data = market_data.copy()
    market_data['year_month'] = market_data['date'].dt.to_period('M')
    unique_months = sorted(market_data['year_month'].unique())

    cash = initial_capital
    current_portfolio = []
    history = []
    total_cost_spent = 0
    first_first_day = None

    for current_month in unique_months:
        month_df = market_data[market_data['year_month'] == current_month]
        if month_df.empty:
            continue

        trading_days = sorted(month_df['date'].unique())
        if len(trading_days) < 2:
            continue

        first_day = trading_days[0]
        last_day = trading_days[-1]

        if first_first_day is None:
            first_first_day = first_day

        month_cost = 0

        # ── Determine whether to rebalance this month ──
        month_number = current_month.month
        is_quarter_start = month_number in [1, 4, 7, 10]
        is_rebalance = (
            config.rebalance_freq == 'monthly' or
            not current_portfolio or
            is_quarter_start
        )

        if is_rebalance:
            # ── FIRST DAY: rebalance (sell old + buy new) ──
            first_day_df = month_df[month_df['date'] == first_day]

            # Sell existing holdings at first_day close
            if current_portfolio:
                sell_amount = 0
                for asset in current_portfolio:
                    stock_info = first_day_df[first_day_df['code'] == asset['code']]
                    if stock_info.empty:
                        exec_sell_price = asset['buy_price'] * 0.1
                    else:
                        exec_sell_price = stock_info.iloc[0]['close']
                    gross_sell_value = asset['shares'] * exec_sell_price
                    net_sell_val = gross_sell_value * (1 - slippage) * (1 - sell_cost)
                    month_cost += gross_sell_value - net_sell_val
                    sell_amount += net_sell_val
                cash += sell_amount

            # Select new portfolio using first_day data
            universe = first_day_df[
                (~first_day_df['is_preferred']) &
                (first_day_df['market_cap'] >= config.min_market_cap) &
                (first_day_df['trading_val'] >= config.min_trading_val)
            ].copy()

            universe = universe[
                (universe['per'] >= config.per_min) &
                (universe['per'] <= config.per_max)
            ]

            if config.use_multi_factor:
                for col in ['pbr', 'div', 'bps', 'eps']:
                    if col not in universe.columns:
                        universe[col] = float('nan')
                universe['roe'] = universe['eps'] / universe['bps']
                universe = universe[
                    (universe['pbr'] >= 0) &
                    (universe['pbr'] <= config.pbr_max) &
                    (universe['roe'] >= config.roe_min)
                ].copy()
                universe['rank_per'] = universe['per'].rank(pct=True)
                universe['rank_pbr'] = universe['pbr'].rank(pct=True)
                universe['rank_roe'] = universe['roe'].rank(ascending=False, pct=True)
                universe['rank_div'] = universe['div'].fillna(0).rank(ascending=False, pct=True)
                universe['score'] = (
                    universe['rank_per'] + universe['rank_pbr'] +
                    universe['rank_roe'] + universe['rank_div']
                )
                selected_stocks = universe.sort_values(by='score').head(n_stocks)
            else:
                selected_stocks = universe.sort_values(by='per', ascending=True).head(n_stocks)

            # Buy new portfolio at first_day close
            new_portfolio = []
            if len(selected_stocks) > 0 and cash > 0:
                target_cash_per_stock = cash / len(selected_stocks)
                for _, row in selected_stocks.iterrows():
                    exec_buy_price = row['close'] * (1 + slippage)
                    shares = int(target_cash_per_stock / (exec_buy_price * (1 + buy_cost)))
                    if shares > 0:
                        actual_cost = shares * exec_buy_price * (1 + buy_cost)
                        month_cost += actual_cost - (shares * row['close'])
                        cash -= actual_cost
                        new_portfolio.append({
                            'code': row['code'],
                            'shares': shares,
                            'buy_price': row['close']
                        })

            current_portfolio = new_portfolio
            total_cost_spent += month_cost

        # ── LAST DAY: mark-to-market for reporting ──
        last_day_df = month_df[month_df['date'] == last_day]
        holdings_value = 0
        for asset in current_portfolio:
            stock_info = last_day_df[last_day_df['code'] == asset['code']]
            if not stock_info.empty:
                holdings_value += asset['shares'] * stock_info.iloc[0]['close']
            else:
                holdings_value += asset['shares'] * asset['buy_price'] * 0.1

        portfolio_value = cash + holdings_value
        total_return = ((portfolio_value - initial_capital) / initial_capital) * 100

        history.append({
            "Date": str(last_day.date()),
            "Portfolio_Value": int(portfolio_value),
            "Total_Return(%)": round(total_return, 2),
            "Stock_Count": len(current_portfolio)
        })

    # Add initial state row at the beginning
    if first_first_day is not None:
        history.insert(0, {
            "Date": str(first_first_day.date()),
            "Portfolio_Value": initial_capital,
            "Total_Return(%)": 0.0,
            "Stock_Count": 0
        })

    history_df = pd.DataFrame(history)
    history_df['Peak'] = history_df['Portfolio_Value'].cummax()
    history_df['Drawdown'] = ((history_df['Portfolio_Value'] - history_df['Peak']) / history_df['Peak']) * 100
    mdd = history_df['Drawdown'].min()

    first_date = history_df['Date'].iloc[0]
    last_date = history_df['Date'].iloc[-1]
    years = (pd.Timestamp(last_date) - pd.Timestamp(first_date)).days / 365.25
    final_value = int(history_df['Portfolio_Value'].iloc[-1])
    cagr = ((final_value / initial_capital) ** (1 / years) - 1) * 100 if years > 0 else 0.0

    metrics = {
        "INITIAL_CAPITAL": initial_capital,
        "FINAL_PORTFOLIO_VALUE": final_value,
        "TOTAL_RETURN_PCT": history_df['Total_Return(%)'].iloc[-1],
        "CAGR_PCT": round(cagr, 2),
        "MAX_DRAWDOWN_PCT": round(mdd, 2),
        "TOTAL_COST_IMPACT_KRW": int(total_cost_spent),
    }

    return history_df, metrics
