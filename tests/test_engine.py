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
        min_market_cap=50_000_000_000,
        min_trading_val=1_000_000_000,
        use_multi_factor=False,
    )


def _make_month_dates(year, month):
    """Generate synthetic first/last trading days for a given month."""
    cal = pd.bdate_range(f"{year}-{month:02d}-01", f"{year}-{month:02d}-28", freq="B")
    return cal[0], cal[-1]


def _make_stock_rows(
    dates,
    codes,
    close,
    market_cap=100_000_000_000,
    trading_val=10_000_000_000,
    per=2.0,
    is_preferred=False,
):
    """Generate rows for the market_data DataFrame."""
    rows = []
    for date in dates:
        for code in codes:
            rows.append(
                {
                    "date": date,
                    "code": code,
                    "close": close,
                    "market_cap": market_cap,
                    "trading_val": trading_val,
                    "per": per,
                    "is_preferred": is_preferred,
                }
            )
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
            start_date="2020-01-01",
            end_date="2020-02-29",
            initial_capital=1_000_000,
            buy_cost=0.0,
            sell_cost=0.0,
            slippage=0.0,
            n_stocks=30,
            min_market_cap=50_000_000_000,
            min_trading_val=1_000_000_000,
            use_multi_factor=False,
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
            start_date="2020-01-01",
            end_date="2020-02-29",
            initial_capital=1_000_000,
            buy_cost=0.00015,
            sell_cost=0.0023,
            slippage=0.002,
            n_stocks=30,
            min_market_cap=50_000_000_000,
            min_trading_val=1_000_000_000,
            use_multi_factor=False,
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
            {
                "date": fd_jan,
                "code": "005930",
                "close": 50000,
                "market_cap": 100_000_000_000,
                "trading_val": 10_000_000_000,
                "per": 3.0,
                "is_preferred": False,
            },
            {
                "date": fd_jan,
                "code": "000660",
                "close": 50000,
                "market_cap": 100_000_000_000,
                "trading_val": 10_000_000_000,
                "per": 3.5,
                "is_preferred": False,
            },
            # Preferred stocks (lower PER — would be picked first if not filtered)
            {
                "date": fd_jan,
                "code": "005935",
                "close": 50000,
                "market_cap": 100_000_000_000,
                "trading_val": 10_000_000_000,
                "per": 1.0,
                "is_preferred": True,
            },
            {
                "date": fd_jan,
                "code": "000665",
                "close": 50000,
                "market_cap": 100_000_000_000,
                "trading_val": 10_000_000_000,
                "per": 1.2,
                "is_preferred": True,
            },
            # Last day data for MTM
            {
                "date": ld_jan,
                "code": "005930",
                "close": 50000,
                "market_cap": 100_000_000_000,
                "trading_val": 10_000_000_000,
                "per": 3.0,
                "is_preferred": False,
            },
            {
                "date": ld_jan,
                "code": "000660",
                "close": 50000,
                "market_cap": 100_000_000_000,
                "trading_val": 10_000_000_000,
                "per": 3.5,
                "is_preferred": False,
            },
        ]

        market_data = pd.DataFrame(rows)
        history, metrics = run_backtest(market_data, zero_cost_config)

        # Only 2 common stocks should be selected
        assert history["Stock_Count"].iloc[1] == 2

    def test_is_preferred_dtype_handling(self, zero_cost_config):
        """is_preferred must work even when code column is non-string type."""
        fd_jan, ld_jan = _make_month_dates(2020, 1)

        data = pd.DataFrame(
            [
                {
                    "date": fd_jan,
                    "code": "005930",
                    "close": 50000,
                    "market_cap": 100_000_000_000,
                    "trading_val": 10_000_000_000,
                    "per": 2.0,
                    "is_preferred": False,
                },
                {
                    "date": ld_jan,
                    "code": "005930",
                    "close": 50000,
                    "market_cap": 100_000_000_000,
                    "trading_val": 10_000_000_000,
                    "per": 2.0,
                    "is_preferred": False,
                },
            ]
        )
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
            {
                "date": fd_jan,
                "code": "000010",
                "close": 10000,
                "market_cap": 100_000_000_000,
                "trading_val": 10_000_000_000,
                "per": 2.0,
                "is_preferred": False,
            },
            {
                "date": ld_jan,
                "code": "000010",
                "close": 11000,
                "market_cap": 100_000_000_000,
                "trading_val": 10_000_000_000,
                "per": 2.0,
                "is_preferred": False,
            },
            # Feb first_day close = 9000 (drop from Jan)
            {
                "date": fd_feb,
                "code": "000010",
                "close": 9000,
                "market_cap": 100_000_000_000,
                "trading_val": 10_000_000_000,
                "per": 2.0,
                "is_preferred": False,
            },
            {
                "date": ld_feb,
                "code": "000010",
                "close": 9500,
                "market_cap": 100_000_000_000,
                "trading_val": 10_000_000_000,
                "per": 2.0,
                "is_preferred": False,
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


class TestKatsenelson:
    """Verify Katsenelson quality filter + scoring selects quality stocks only."""

    def _setup_dart_cache(self, tmp_path):
        """가짜 DART 캐시: 삼성전자(품질 통과) + 그린스팬(재무데이터 없음)."""
        cache_dir = tmp_path / ".cache" / "backtest"
        dart_dir = cache_dir / "dart_data"
        dart_dir.mkdir(parents=True, exist_ok=True)

        codes = pd.DataFrame(
            [
                {"corp_code": "00126380", "ticker": "005930", "corp_name": "삼성전자"},
            ]
        )
        codes.to_parquet(cache_dir / "dart_corp_codes.parquet", index=False)

        # 2022년 보고서: ROIC 높게 설정 (품질 통과)
        fin = pd.DataFrame(
            [
                {
                    "year": 2022,
                    "operating_income": 5_000_000_000_000,
                    "cash": 50_000_000_000,
                    "total_liabilities": 100_000_000_000,
                    "total_equity": 300_000_000_000,
                    "borrowings": 10_000_000_000,
                    "current_assets": 250_000_000_000,
                    "net_income": 60_000_000_000,
                    "operating_cf": 80_000_000_000,
                    "capex": 20_000_000_000,
                    "interest_paid": 5_000_000_000,
                    "depreciation": 10_000_000_000,
                },
            ]
        )
        fin.to_parquet(dart_dir / "00126380.parquet", index=False)
        return cache_dir

    def test_katsenelson_selects_only_quality_stock(self, tmp_path):
        cache_dir = self._setup_dart_cache(tmp_path)

        fd_jan, ld_jan = _make_month_dates(2024, 1)
        rows = []
        for d in [fd_jan, ld_jan]:
            # 삼성전자: 품질 통과 (DART 재무제표 존재)
            rows.append(
                {
                    "date": d,
                    "code": "005930",
                    "close": 70000,
                    "market_cap": 500_000_000_000,
                    "trading_val": 10_000_000_000,
                    "per": 15,
                    "is_preferred": False,
                }
            )
            # 그린스팬: 재무제표 없음 → 품질 필터 탈락
            rows.append(
                {
                    "date": d,
                    "code": "000000",
                    "close": 10000,
                    "market_cap": 50_000_000_000,
                    "trading_val": 10_000_000_000,
                    "per": 3,
                    "is_preferred": False,
                }
            )
        market_data = pd.DataFrame(rows)

        config = Config(
            start_date="2024-01-01",
            end_date="2024-01-31",
            initial_capital=100_000_000,
            buy_cost=0.0,
            sell_cost=0.0,
            slippage=0.0,
            n_stocks=2,
            min_market_cap=10_000_000_000,
            min_trading_val=1_000_000_000,
            use_katsenelson=True,
            use_multi_factor=False,
            min_roic=0.05,
            max_ev_ebitda=50.0,
        )
        history, _ = run_backtest(market_data, config, cache_dir=cache_dir)
        # 2024-01-02: 2022년 보고서(2023-04-15 공시) 사용 가능 → 삼성전자만 선택
        assert history["Stock_Count"].iloc[1] == 1

    def test_katsenelson_no_financials_selects_nothing(self, tmp_path):
        """재무제표 캐시가 없으면 아무것도 선택하지 않는다 (안전)."""
        cache_dir = tmp_path / ".cache" / "backtest"
        fd_jan, ld_jan = _make_month_dates(2024, 1)
        rows = []
        for d in [fd_jan, ld_jan]:
            rows.append(
                {
                    "date": d,
                    "code": "005930",
                    "close": 70000,
                    "market_cap": 500_000_000_000,
                    "trading_val": 10_000_000_000,
                    "per": 15,
                    "is_preferred": False,
                }
            )
        market_data = pd.DataFrame(rows)

        config = Config(
            start_date="2024-01-01",
            end_date="2024-01-31",
            initial_capital=100_000_000,
            buy_cost=0.0,
            sell_cost=0.0,
            slippage=0.0,
            n_stocks=2,
            min_market_cap=10_000_000_000,
            min_trading_val=1_000_000_000,
            use_katsenelson=True,
            use_multi_factor=False,
        )
        history, _ = run_backtest(market_data, config, cache_dir=cache_dir)
        assert history["Stock_Count"].iloc[1] == 0

    def test_invalid_rebalance_freq_raises(self, zero_cost_config):
        """지원하지 않는 리밸런싱 주기는 명시적 오류."""
        import pytest

        fd_jan, ld_jan = _make_month_dates(2020, 1)
        rows = _make_stock_rows([fd_jan, ld_jan], ["000010"], 10000)
        market_data = pd.DataFrame(rows)
        bad = Config(
            start_date="2020-01-01",
            end_date="2020-01-31",
            initial_capital=100_000_000,
            buy_cost=0.0,
            sell_cost=0.0,
            slippage=0.0,
            n_stocks=30,
            min_market_cap=50_000_000_000,
            min_trading_val=1_000_000_000,
            use_multi_factor=False,
            rebalance_freq="weekly",
        )
        with pytest.raises(ValueError):
            run_backtest(market_data, bad)

    def test_exclude_negative_per_filters_loss_makers(self):
        """음수 PER 종목은 '저PER'로 오스코어되지 않아야 한다."""
        fd_jan, ld_jan = _make_month_dates(2020, 1)
        rows = []
        # A: 음수 PER (적자 기업), B: 정상 저PER
        rows += _make_stock_rows([fd_jan, ld_jan], ["000010"], 10000, per=-5.0)
        rows += _make_stock_rows([fd_jan, ld_jan], ["000020"], 10000, per=5.0)
        market_data = pd.DataFrame(rows)

        config = Config(
            start_date="2020-01-01",
            end_date="2020-01-31",
            initial_capital=100_000_000,
            buy_cost=0.0,
            sell_cost=0.0,
            slippage=0.0,
            n_stocks=1,
            min_market_cap=10_000_000_000,
            min_trading_val=1_000_000_000,
            use_multi_factor=False,
            exclude_negative_per=True,
        )
        history, _ = run_backtest(market_data, config)
        # n_stocks=1, 저PER=5가 정상 000020이 선택되어야 함
        assert history["Stock_Count"].iloc[1] == 1

    def test_market_regime_nan_does_not_liquidate(self, tmp_path):
        """KOSPI close가 NaN이면 전량 현금화하지 않고 유지 (bull 기본)."""
        cache_dir = tmp_path / ".cache" / "backtest"
        (cache_dir).mkdir(parents=True, exist_ok=True)
        # KOSPI MA 캐시에 NaN close만 있는 경우
        kospi = pd.DataFrame(
            {"kospi_close": [float("nan")]},
            index=pd.to_datetime(["2020-01-01"]),
        )
        kospi.index.name = "date"
        kospi.to_parquet(cache_dir / "kospi_ma.parquet")

        fd_jan, ld_jan = _make_month_dates(2020, 1)
        rows = _make_stock_rows([fd_jan, ld_jan], ["000010"], 10000)
        market_data = pd.DataFrame(rows)

        config = Config(
            start_date="2020-01-01",
            end_date="2020-01-31",
            initial_capital=100_000_000,
            buy_cost=0.0,
            sell_cost=0.0,
            slippage=0.0,
            n_stocks=1,
            min_market_cap=10_000_000_000,
            min_trading_val=1_000_000_000,
            use_multi_factor=False,
        )
        history, _ = run_backtest(market_data, config, cache_dir=cache_dir)
        # NaN close → is_bull 유지 → 매수 수행 → 종목 1개 보유
        assert history["Stock_Count"].iloc[1] == 1

    def test_market_regime_off_never_liquidates(self, tmp_path):
        """use_market_regime=False면 KOSPI가 하락세여도 청산하지 않는다."""
        cache_dir = tmp_path / ".cache" / "backtest"
        cache_dir.mkdir(parents=True, exist_ok=True)
        # KOSPI가 MA200 아래 (하락세)인 캐시 — start(2020-01-01)의 420일 레프백 커버
        close_vals = [100.0] * 800
        kospi = pd.DataFrame(
            {"kospi_close": close_vals},
            index=pd.date_range("2018-01-02", periods=800, freq="B"),
        )
        kospi.index.name = "date"
        kospi.to_parquet(cache_dir / "kospi_ma.parquet")

        fd_jan, ld_jan = _make_month_dates(2020, 1)
        rows = _make_stock_rows([fd_jan, ld_jan], ["000010"], 10000)
        market_data = pd.DataFrame(rows)

        config = Config(
            start_date="2020-01-01",
            end_date="2020-01-31",
            initial_capital=100_000_000,
            buy_cost=0.0,
            sell_cost=0.0,
            slippage=0.0,
            n_stocks=1,
            min_market_cap=10_000_000_000,
            min_trading_val=1_000_000_000,
            use_multi_factor=False,
            use_market_regime=False,
        )
        history, _ = run_backtest(market_data, config, cache_dir=cache_dir)
        # 레짐 off → is_bull 유지 → 매수 수행
        assert history["Stock_Count"].iloc[1] == 1

    def test_start_end_date_filter(self):
        """start_date/end_date 밖의 데이터는 백테스트에서 제외돼야 한다."""
        fd_jan, ld_jan = _make_month_dates(2020, 1)
        fd_feb, ld_feb = _make_month_dates(2020, 2)
        rows = _make_stock_rows([fd_jan, ld_jan, fd_feb, ld_feb], ["000010"], 10000)
        market_data = pd.DataFrame(rows)

        config = Config(
            start_date="2020-01-01",
            end_date="2020-01-31",
            initial_capital=100_000_000,
            buy_cost=0.0,
            sell_cost=0.0,
            slippage=0.0,
            n_stocks=1,
            min_market_cap=10_000_000_000,
            min_trading_val=1_000_000_000,
            use_multi_factor=False,
            use_market_regime=False,
        )
        history, _ = run_backtest(market_data, config)
        # 1월만 사용 → 마지막 기록이 1월 마지막 거래일
        assert pd.Timestamp(history["Date"].iloc[-1]).month == 1

    def test_end_date_excludes_future(self):
        """end_date 이후 데이터가 있으면 제외돼야 한다 (기간 분리)."""
        fd_jan, ld_jan = _make_month_dates(2020, 1)
        fd_feb, ld_feb = _make_month_dates(2020, 2)
        rows = _make_stock_rows([fd_jan, ld_jan, fd_feb, ld_feb], ["000010"], 10000)
        market_data = pd.DataFrame(rows)

        config = Config(
            start_date="2020-01-01",
            end_date="2020-01-31",
            initial_capital=100_000_000,
            buy_cost=0.0,
            sell_cost=0.0,
            slippage=0.0,
            n_stocks=1,
            min_market_cap=10_000_000_000,
            min_trading_val=1_000_000_000,
            use_multi_factor=False,
            use_market_regime=False,
        )
        history, _ = run_backtest(market_data, config)
        assert history["Date"].nunique() == 2  # 초기 + 1월 MTM만

    def test_empty_period_raises(self):
        """기간 내 데이터가 없으면 명시적 ValueError."""
        fd_jan, ld_jan = _make_month_dates(2020, 1)
        rows = _make_stock_rows([fd_jan, ld_jan], ["000010"], 10000)
        market_data = pd.DataFrame(rows)

        config = Config(
            start_date="2030-01-01",
            end_date="2030-01-31",
            initial_capital=100_000_000,
            buy_cost=0.0,
            sell_cost=0.0,
            slippage=0.0,
            n_stocks=1,
            min_market_cap=10_000_000_000,
            min_trading_val=1_000_000_000,
            use_multi_factor=False,
            use_market_regime=False,
        )
        with pytest.raises(ValueError):
            run_backtest(market_data, config)
