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


def fetch_rebalancing_data(start_date, end_date, cache_dir=None, force_refresh=False, lag_months=0):
    if lag_months > 0:
        fetch_start = (pd.Timestamp(start_date) - pd.DateOffset(months=lag_months)).strftime("%Y-%m-%d")
    else:
        fetch_start = start_date
    today = pd.Timestamp.now().normalize()
    display_end = min(pd.Timestamp(end_date), today).strftime("%Y-%m-%d")
    print(f"[{fetch_start} ~ {display_end}] 영업일 캘린더 분석 중... (백테스트: {start_date} ~ {display_end})")
    b_days = get_korean_business_days(fetch_start, end_date)
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


def _fetch_kospi_for_ma(config: Config, cache_dir=None) -> pd.DataFrame:
    """Fetch daily KOSPI close prices with extra lookback for MA200 calculation."""
    cache_base = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    cache_base.mkdir(parents=True, exist_ok=True)
    cache_file = cache_base / "kospi_ma.parquet"

    req_start = pd.Timestamp(config.start_date) - pd.DateOffset(days=420)
    req_end = pd.Timestamp(config.end_date)

    if cache_file.exists():
        try:
            df = pd.read_parquet(cache_file)
            df.index = pd.to_datetime(df.index)
            idx_min, idx_max = df.index.min(), df.index.max()
            if not pd.isna(idx_min) and not pd.isna(idx_max) and idx_min <= req_start and idx_max >= min(req_end, pd.Timestamp.now()):
                return df
        except Exception:
            pass

    fetch_start = req_start.strftime("%Y%m%d")
    today = pd.Timestamp.now().normalize()
    fetch_end = min(req_end, today).strftime("%Y%m%d")

    try:
        raw = stock.get_index_ohlcv_by_date(fetch_start, fetch_end, "1001")
        df = raw.reset_index()
        df["date"] = pd.to_datetime(df["날짜"])
        df = df[["date", "종가"]].rename(columns={"종가": "kospi_close"}).set_index("date")
        df.index.name = "date"
        df = df[~df.index.duplicated(keep='last')].sort_index()
        df.to_parquet(cache_file)
        return df
    except Exception:
        return pd.DataFrame()


def run_backtest(market_data, config: Config, cache_dir=None):
    initial_capital = config.initial_capital
    buy_cost, sell_cost, slippage = config.buy_cost, config.sell_cost, config.slippage
    n_stocks = config.n_stocks

    market_data = market_data.copy()
    market_data['year_month'] = market_data['date'].dt.to_period('M')
    unique_months = sorted(market_data['year_month'].unique())

    # ── Pre-compute first-day close per month for momentum/volatility ──
    monthly_close = {}
    for ym in unique_months:
        ym_df = market_data[market_data['year_month'] == ym]
        first_date = sorted(ym_df['date'].unique())[0]
        monthly_close[ym] = ym_df[ym_df['date'] == first_date].set_index('code')['close']

    # ── KOSPI 200-day MA market regime ──
    kospi_regime = _fetch_kospi_for_ma(config, cache_dir)
    if not kospi_regime.empty:
        kospi_regime['ma200'] = kospi_regime['kospi_close'].rolling(200, min_periods=200).mean()

    cash = initial_capital
    current_portfolio = []
    history = []
    total_cost_spent = 0
    first_first_day = None

    first_backtest_month = None
    if config.fundamental_lag_months > 0 and len(unique_months) > config.fundamental_lag_months:
        first_backtest_month = unique_months[config.fundamental_lag_months]

    for current_month in unique_months:
        if first_backtest_month is not None and current_month < first_backtest_month:
            continue

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

        first_day_df = month_df[month_df['date'] == first_day]

        # ── Market regime: KOSPI 200-day MA ──
        is_bull = True
        if not kospi_regime.empty:
            first_ts = pd.Timestamp(first_day)
            closest = kospi_regime[kospi_regime.index <= first_ts].tail(1)
            if not closest.empty and closest['ma200'].notna().iloc[0]:
                is_bull = closest['kospi_close'].iloc[0] >= closest['ma200'].iloc[0]

        # ── Bear market → liquidate ──
        if not is_bull and current_portfolio:
            sell_proceeds = 0
            for asset in current_portfolio:
                stock_info = first_day_df[first_day_df['code'] == asset['code']]
                if stock_info.empty:
                    exec_sell_price = asset['buy_price'] * 0.1
                    eff_slippage = slippage
                else:
                    exec_sell_price = stock_info.iloc[0]['close']
                    trade_val = stock_info.iloc[0]['trading_val']
                    gross_sell_value = asset['shares'] * exec_sell_price
                    eff_slippage = min(slippage, (gross_sell_value / trade_val) * 0.5) if trade_val > 0 else slippage
                gross_sell_value = asset['shares'] * exec_sell_price
                net_sell_val = gross_sell_value * (1 - eff_slippage) * (1 - sell_cost)
                month_cost += gross_sell_value - net_sell_val
                sell_proceeds += net_sell_val
            cash += sell_proceeds
            current_portfolio = []
            total_cost_spent += month_cost

        # ── Bull market → determine rebalance schedule ──
        if is_bull:
            month_number = current_month.month
            is_quarter_start = month_number in [1, 4, 7, 10]
            is_rebalance = (
                config.rebalance_freq == 'monthly' or
                not current_portfolio or
                is_quarter_start
            )

            if is_rebalance:
                # ── Lag fundamental data (look-ahead bias 보정) ──
                if config.fundamental_lag_months > 0:
                    lag_period = current_month - config.fundamental_lag_months
                    lagged_df = market_data[market_data['year_month'] == lag_period]
                    if not lagged_df.empty:
                        lag_dates = sorted(lagged_df['date'].unique())
                        lag_fund = lagged_df[lagged_df['date'] == lag_dates[0]][['code', 'per', 'pbr', 'div', 'bps', 'eps']].copy()
                        lag_fund.columns = ['code', 'per_lag', 'pbr_lag', 'div_lag', 'bps_lag', 'eps_lag']
                        first_day_df = first_day_df.merge(lag_fund, on='code', how='left')
                        first_day_df['per'] = first_day_df['per_lag']
                        first_day_df['pbr'] = first_day_df['pbr_lag']
                        first_day_df['div'] = first_day_df['div_lag']
                        first_day_df['bps'] = first_day_df['bps_lag']
                        first_day_df['eps'] = first_day_df['eps_lag']
                        first_day_df = first_day_df.drop(columns=['per_lag', 'pbr_lag', 'div_lag', 'bps_lag', 'eps_lag'])

                # Select target portfolio using first_day data
                universe = first_day_df[
                    (~first_day_df['is_preferred']) &
                    (first_day_df['market_cap'] >= config.min_market_cap) &
                    (first_day_df['trading_val'] >= config.min_trading_val)
                ].copy()

                if config.max_market_cap > 0:
                    universe = universe[universe['market_cap'] <= config.max_market_cap]

                # ── Percentile-based fundamental filters (intersection) ──
                if config.use_multi_factor:
                    for col in ['pbr', 'div', 'bps', 'eps']:
                        if col not in universe.columns:
                            universe[col] = float('nan')
                    universe['roe'] = universe['eps'] / universe['bps']
                    pbr_r = universe['pbr'].rank(pct=True)
                    universe = universe[
                        (pbr_r <= config.pbr_pctile) &
                        (universe['pbr'] >= 0)
                    ].copy()

                # ── Momentum & Volatility (bias-free price-based factors) ──
                if config.use_momentum or config.use_low_volatility:
                    if current_month in monthly_close:
                        past_month = current_month - config.momentum_window
                        if config.use_momentum and past_month in monthly_close:
                            past_prices = monthly_close[past_month]
                            universe = universe.merge(past_prices.rename('price_12m_ago'), on='code', how='left')
                            universe['momentum'] = (universe['close'] / universe['price_12m_ago']) - 1
                            universe = universe[universe['momentum'] > -1].copy()

                        if config.use_low_volatility:
                            vol_prices = []
                            for idx in range(1, config.momentum_window + 1):
                                ym = current_month - idx
                                if ym in monthly_close:
                                    vol_prices.append(monthly_close[ym].rename(f'p_{idx}'))
                            if len(vol_prices) >= 6:
                                vol_df = pd.concat(vol_prices, axis=1)
                                vol_df.columns = [f'p_{i+1}' for i in range(len(vol_prices))]
                                monthly_returns = vol_df.pct_change(axis=1, fill_method=None).iloc[:, 1:]
                                vol_series = monthly_returns.std(axis=1).dropna().rename('volatility')
                                universe = universe.merge(vol_series, on='code', how='left')

                # ── Dynamic scoring ──
                score_components = []

                if config.use_momentum and 'momentum' in universe.columns:
                    universe['rank_momentum'] = universe['momentum'].rank(ascending=False, pct=True)
                    score_components.append('rank_momentum')
                else:
                    universe['rank_per'] = universe['per'].rank(pct=True)
                    score_components.append('rank_per')

                if config.use_multi_factor:
                    universe['rank_roe'] = universe['roe'].rank(ascending=False, pct=True)
                    universe['rank_div'] = universe['div'].fillna(0).rank(ascending=False, pct=True)
                    score_components.extend(['rank_roe', 'rank_div'])

                if config.use_low_volatility and 'volatility' in universe.columns:
                    universe['rank_vol'] = universe['volatility'].rank(pct=True)
                    score_components.append('rank_vol')

                universe['score'] = universe[score_components].sum(axis=1)
                target_stocks = universe.sort_values(by='score').head(n_stocks)

                # Determine which current stocks to keep (partial turnover)
                if current_portfolio and config.max_turnover < 1.0:
                    target_codes = set(target_stocks['code'])
                    if 'score' in target_stocks.columns:
                        target_sort_key = dict(zip(target_stocks['code'], target_stocks['score']))
                        overlap = [
                            (a, target_sort_key[a['code']])
                            for a in current_portfolio
                            if a['code'] in target_codes
                        ]
                        overlap.sort(key=lambda x: x[1])
                    else:
                        target_sort_key = dict(zip(target_stocks['code'], target_stocks['per']))
                        overlap = [
                            (a, target_sort_key[a['code']])
                            for a in current_portfolio
                            if a['code'] in target_codes
                        ]
                        overlap.sort(key=lambda x: x[1])
                    n_keep = n_stocks - max(1, int(n_stocks * config.max_turnover))
                    keep_map = {a['code'] for a, _ in overlap[:n_keep]}
                    to_sell = [a for a in current_portfolio if a['code'] not in keep_map]
                    to_keep = [a for a in current_portfolio if a['code'] in keep_map]
                else:
                    to_sell = current_portfolio[:] if current_portfolio else []
                    to_keep = []

                # Execute sells (volume-based slippage)
                if to_sell:
                    sell_amount = 0
                    for asset in to_sell:
                        stock_info = first_day_df[first_day_df['code'] == asset['code']]
                        if stock_info.empty:
                            exec_sell_price = asset['buy_price'] * 0.1
                            eff_slippage = slippage
                        else:
                            exec_sell_price = stock_info.iloc[0]['close']
                            trade_val = stock_info.iloc[0]['trading_val']
                            gross_sell_value = asset['shares'] * exec_sell_price
                            eff_slippage = min(slippage, (gross_sell_value / trade_val) * 0.5) if trade_val > 0 else slippage
                        gross_sell_value = asset['shares'] * exec_sell_price
                        net_sell_val = gross_sell_value * (1 - eff_slippage) * (1 - sell_cost)
                        month_cost += gross_sell_value - net_sell_val
                        sell_amount += net_sell_val
                    cash += sell_amount

                # Execute buys (volume-based slippage)
                new_portfolio = to_keep[:]
                keep_codes = {a['code'] for a in to_keep}
                n_new = n_stocks - len(to_keep)
                if n_new > 0 and cash > 0:
                    new_to_buy = target_stocks[~target_stocks['code'].isin(keep_codes)].head(n_new)
                    if len(new_to_buy) > 0:
                        target_cash_per_stock = cash / len(new_to_buy)
                        for _, row in new_to_buy.iterrows():
                            trade_val = row['trading_val']
                            req_shares = int(target_cash_per_stock / (row['close'] * (1 + slippage) * (1 + buy_cost)))
                            if req_shares <= 0:
                                continue
                            order_value = req_shares * row['close']
                            eff_slippage = min(slippage, (order_value / trade_val) * 0.5) if trade_val > 0 else slippage
                            exec_buy_price = row['close'] * (1 + eff_slippage)
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
