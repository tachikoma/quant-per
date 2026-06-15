import pytest
import pandas as pd
from engine import run_backtest
from config import Config


@pytest.fixture
def zero_cost_config():
    return Config(
        start_date="2020-01-01",
        end_date="2020-02-29",
        initial_capital=100_000_000,
        buy_cost=0.0,
        sell_cost=0.0,
        slippage=0.0,
        n_stocks=30,
        per_min=0.01,
        per_max=4.0,
        min_market_cap=50_000_000_000,
        min_trading_val=1_000_000_000,
    )


def _make_month_dates(year, month):
    """Generate synthetic first/last trading days for a given month."""
    cal = pd.bdate_range(f"{year}-{month:02d}-01", f"{year}-{month:02d}-28", freq="B")
    return cal[0], cal[-1]


def _make_stock_rows(dates, codes, close, market_cap=100_000_000_000,
                     trading_val=10_000_000_000, per=2.0, is_preferred=False):
    """Generate rows for the market_data DataFrame."""
    rows = []
    for date in dates:
        for code in codes:
            rows.append({
                "date": date, "code": code, "close": close,
                "market_cap": market_cap, "trading_val": trading_val,
                "per": per, "is_preferred": is_preferred
            })
    return rows


class TestBasicBacktest:
    """Verify basic return calculations."""

    def test_simple_two_month_return(self, zero_cost_config):
        """2 months with zero costs: 10% then -5% price changes."""
        fd_jan, ld_jan = _make_month_dates(2020, 1)
        fd_feb, ld_feb = _make_month_dates(2020, 2)

        codes = ["000010", "000020", "000030"]
        rows = []
        # Jan first_day: close=10000
        rows += _make_stock_rows([fd_jan], codes, 10000)
        # Jan last_day: close=11000 (+10%)
        rows += _make_stock_rows([ld_jan], codes, 11000)
        # Feb first_day: close=10000 (rebalance price)
        rows += _make_stock_rows([fd_feb], codes, 10000)
        # Feb last_day: close=10500 (-4.5% from peak)
        rows += _make_stock_rows([ld_feb], codes, 10500)

        market_data = pd.DataFrame(rows)
        history, metrics = run_backtest(market_data, zero_cost_config)

        assert len(history) == 3  # initial + 2 months
        assert history["Portfolio_Value"].iloc[0] == 100_000_000
        assert history["Total_Return(%)"].iloc[0] == 0.0

        jan_val = history["Portfolio_Value"].iloc[1]
        assert jan_val == pytest.approx(110_000_000, rel=0.02), f"Jan value: {jan_val}"

        feb_val = history["Portfolio_Value"].iloc[2]
        assert feb_val == pytest.approx(105_000_000, rel=0.02), f"Feb value: {feb_val}"

        assert metrics["TOTAL_RETURN_PCT"] == pytest.approx(5.0, rel=0.02)

    def test_no_price_change(self, zero_cost_config):
        """No price movement: portfolio value should stay at initial capital."""
        fd_jan, ld_jan = _make_month_dates(2020, 1)
        fd_feb, ld_feb = _make_month_dates(2020, 2)

        codes = ["000010", "000020"]
        rows = _make_stock_rows([fd_jan, ld_jan, fd_feb, ld_feb], codes, 20000)
        market_data = pd.DataFrame(rows)
        history, metrics = run_backtest(market_data, zero_cost_config)

        assert metrics["FINAL_PORTFOLIO_VALUE"] == pytest.approx(100_000_000, rel=0.001)
        assert metrics["TOTAL_RETURN_PCT"] == pytest.approx(0.0, abs=0.01)
        assert metrics["TOTAL_COST_IMPACT_KRW"] == 0


class TestCashRemainder:
    """Verify remaining cash from partial stock purchases is preserved."""

    def test_cash_preserved_across_months(self):
        """With 0 costs and no price change, cash remainder must not be lost."""
        config = Config(
            start_date="2020-01-01", end_date="2020-02-29",
            initial_capital=1_000_000,
            buy_cost=0.0, sell_cost=0.0, slippage=0.0,
            n_stocks=30, per_min=0.01, per_max=4.0,
            min_market_cap=50_000_000_000, min_trading_val=1_000_000_000,
        )

        fd_jan, ld_jan = _make_month_dates(2020, 1)
        fd_feb, ld_feb = _make_month_dates(2020, 2)

        # 1 stock at 30,000 won → 1,000,000/30,000 = 33 shares, 10,000 won remaining
        rows = _make_stock_rows([fd_jan, ld_jan, fd_feb, ld_feb], ["000010"], 30000)
        market_data = pd.DataFrame(rows)
        history, metrics = run_backtest(market_data, config)

        # Without the fix, portfolio_value would drop to ~990,000 after month 2
        # because the 10,000 won remainder would be lost on re-sell.
        assert history["Portfolio_Value"].iloc[0] == 1_000_000
        assert history["Portfolio_Value"].iloc[1] == pytest.approx(1_000_000, rel=0.001)
        assert history["Portfolio_Value"].iloc[2] == pytest.approx(1_000_000, rel=0.001)
        assert metrics["FINAL_PORTFOLIO_VALUE"] == pytest.approx(1_000_000, rel=0.001)

    def test_cash_with_transaction_costs(self):
        """Cash remainder preserved even with realistic costs (no cash loss)."""
        config = Config(
            start_date="2020-01-01", end_date="2020-02-29",
            initial_capital=1_000_000,
            buy_cost=0.00015, sell_cost=0.0023, slippage=0.002,
            n_stocks=30, per_min=0.01, per_max=4.0,
            min_market_cap=50_000_000_000, min_trading_val=1_000_000_000,
        )
        fd_jan, ld_jan = _make_month_dates(2020, 1)
        fd_feb, ld_feb = _make_month_dates(2020, 2)

        rows = _make_stock_rows([fd_jan, ld_jan, fd_feb, ld_feb], ["000010"], 30000)
        market_data = pd.DataFrame(rows)
        history, metrics = run_backtest(market_data, config)

        val_m2 = history["Portfolio_Value"].iloc[2]

        # Without price change: total cost ≈ initial - final
        expected_final = config.initial_capital - metrics["TOTAL_COST_IMPACT_KRW"]
        assert val_m2 == pytest.approx(expected_final, abs=100), (
            f"Final ({val_m2}) differs from initial minus costs ({expected_final})"
        )
        assert metrics["TOTAL_COST_IMPACT_KRW"] > 0


class TestPreferredStockFilter:
    """Verify preferred stocks are excluded from portfolio selection."""

    def test_preferred_excluded_from_selection(self, zero_cost_config):
        """Only common stocks should be selected regardless of PER rank."""
        fd_jan, ld_jan = _make_month_dates(2020, 1)

        rows = [
            # Common stocks (higher PER)
            {"date": fd_jan, "code": "005930", "close": 50000,
             "market_cap": 100_000_000_000, "trading_val": 10_000_000_000,
             "per": 3.0, "is_preferred": False},
            {"date": fd_jan, "code": "000660", "close": 50000,
             "market_cap": 100_000_000_000, "trading_val": 10_000_000_000,
             "per": 3.5, "is_preferred": False},
            # Preferred stocks (lower PER — would be picked first if not filtered)
            {"date": fd_jan, "code": "005935", "close": 50000,
             "market_cap": 100_000_000_000, "trading_val": 10_000_000_000,
             "per": 1.0, "is_preferred": True},
            {"date": fd_jan, "code": "000665", "close": 50000,
             "market_cap": 100_000_000_000, "trading_val": 10_000_000_000,
             "per": 1.2, "is_preferred": True},
            # Last day data for MTM
            {"date": ld_jan, "code": "005930", "close": 50000,
             "market_cap": 100_000_000_000, "trading_val": 10_000_000_000,
             "per": 3.0, "is_preferred": False},
            {"date": ld_jan, "code": "000660", "close": 50000,
             "market_cap": 100_000_000_000, "trading_val": 10_000_000_000,
             "per": 3.5, "is_preferred": False},
        ]

        market_data = pd.DataFrame(rows)
        history, metrics = run_backtest(market_data, zero_cost_config)

        # Only 2 common stocks should be selected
        assert history["Stock_Count"].iloc[1] == 2

    def test_is_preferred_dtype_handling(self, zero_cost_config):
        """is_preferred must work even when code column is non-string type."""
        fd_jan, ld_jan = _make_month_dates(2020, 1)

        data = pd.DataFrame([
            {"date": fd_jan, "code": "005930", "close": 50000,
             "market_cap": 100_000_000_000, "trading_val": 10_000_000_000,
             "per": 2.0, "is_preferred": False},
            {"date": ld_jan, "code": "005930", "close": 50000,
             "market_cap": 100_000_000_000, "trading_val": 10_000_000_000,
             "per": 2.0, "is_preferred": False},
        ])
        # Simulate type coercion by pykrx
        data["code"] = data["code"].astype(int)

        history, metrics = run_backtest(data, zero_cost_config)
        assert history["Stock_Count"].iloc[1] == 1


class TestRebalancingCycle:
    """Verify the first-day rebalancing cycle is correct."""

    def test_same_day_sell_and_buy(self, zero_cost_config):
        """Sell and buy happen at the same first_day close price."""
        fd_jan, ld_jan = _make_month_dates(2020, 1)
        fd_feb, ld_feb = _make_month_dates(2020, 2)

        rows = [
            {"date": fd_jan, "code": "000010", "close": 10000,
             "market_cap": 100_000_000_000, "trading_val": 10_000_000_000,
             "per": 2.0, "is_preferred": False,
             },
            {"date": ld_jan, "code": "000010", "close": 11000,
             "market_cap": 100_000_000_000, "trading_val": 10_000_000_000,
             "per": 2.0, "is_preferred": False,
             },
            # Feb first_day close = 9000 (drop from Jan)
            {"date": fd_feb, "code": "000010", "close": 9000,
             "market_cap": 100_000_000_000, "trading_val": 10_000_000_000,
             "per": 2.0, "is_preferred": False,
             },
            {"date": ld_feb, "code": "000010", "close": 9500,
             "market_cap": 100_000_000_000, "trading_val": 10_000_000_000,
             "per": 2.0, "is_preferred": False,
             },
        ]
        market_data = pd.DataFrame(rows)
        history, metrics = run_backtest(market_data, zero_cost_config)

        # Jan: bought at 10000, MTM at 11000 → ≈10% gain
        jan_val = history["Portfolio_Value"].iloc[1]
        assert jan_val == pytest.approx(110_000_000, rel=0.02)

        assert history["Stock_Count"].iloc[1] == 1
        assert history["Stock_Count"].iloc[2] == 1
        assert len(history) == 3  # initial + Jan + Feb
        # Feb portfolio should differ from Jan due to reprice at first_day
        assert history["Portfolio_Value"].iloc[1] != history["Portfolio_Value"].iloc[2]
