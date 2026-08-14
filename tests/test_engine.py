import pytest
import pandas as pd
import engine
from engine import run_backtest
from config import Config
from phase2_integrity import classify_phase2_stats, validate_phase2_stats


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(
        engine.stock,
        "get_index_ohlcv_by_date",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("network access")
        ),
    )


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


def _make_v2_rows(prices, pers=None, trading_values=None):
    """Build a small two-market-shaped fixture for next-close integration."""
    pers = pers or {}
    trading_values = trading_values or {}
    rows = []
    for date, codes in prices.items():
        for code, close in codes.items():
            rows.append(
                {
                    "date": pd.Timestamp(date),
                    "market": "KOSPI",
                    "code": code,
                    "close": close,
                    "market_cap": 100_000_000_000,
                    "trading_val": trading_values.get((date, code), 10_000_000_000),
                    "per": pers.get((date, code), 1.0),
                    "pbr": 1.0,
                    "div": 1.0,
                    "bps": 100.0,
                    "eps": 100.0,
                    "is_preferred": False,
                }
            )
    return pd.DataFrame(rows)


def _next_close_config(**kwargs):
    values = dict(
        start_date="2020-01-01",
        end_date="2020-02-28",
        initial_capital=100_000,
        buy_cost=0.0,
        sell_cost=0.0,
        slippage=0.0,
        n_stocks=1,
        min_market_cap=1,
        min_trading_val=0,
        use_multi_factor=False,
        use_market_regime=False,
        execution_mode="next_close",
    )
    values.update(kwargs)
    return Config(**values)


def _phase2_calendar(*_args):
    """A small deterministic calendar with explicit T/T+1 pairs."""
    return pd.to_datetime(
        [
            "2020-01-01",
            "2020-01-02",
            "2020-01-31",
            "2020-02-01",
            "2020-02-03",
            "2020-02-28",
        ]
    )


def _phase2_rows(sell_mode="executed"):
    """Two-month v2-shaped data where January selects A/B and February C/D."""
    prices = {
        "2020-01-01": {"A": 10.0, "B": 20.0, "C": 30.0, "D": 40.0},
        "2020-01-02": {"A": 10.0, "B": 20.0, "C": 30.0, "D": 40.0},
        "2020-01-31": {"A": 11.0, "B": 21.0, "C": 31.0, "D": 41.0},
        "2020-02-01": {"A": 12.0, "B": 22.0, "C": 32.0, "D": 42.0},
        "2020-02-03": {"A": 30.0, "B": 40.0, "C": 50.0, "D": 60.0},
        "2020-02-28": {"A": 31.0, "B": 41.0, "C": 51.0, "D": 61.0},
    }
    rows = []
    for date, closes in prices.items():
        for code, close in closes.items():
            if sell_mode == "absent" and date == "2020-02-03" and code == "A":
                continue
            per = {
                "A": 1.0,
                "B": 2.0,
                "C": 3.0,
                "D": 4.0,
            }[code]
            if date in {"2020-02-01", "2020-02-03", "2020-02-28"}:
                per = {"A": 4.0, "B": 3.0, "C": 1.0, "D": 2.0}[code]
            trading_val = 10000.0
            if date == "2020-02-03" and code == "A":
                trading_val = 0.0 if sell_mode == "zero" else 700.0
            rows.append(
                {
                    "date": pd.Timestamp(date),
                    "market": "KOSPI",
                    "code": code,
                    "close": close,
                    "market_cap": 1_000_000_000.0,
                    "trading_val": trading_val,
                    "per": per,
                    "pbr": 1.0,
                    "div": 1.0,
                    "bps": 100.0,
                    "eps": 100.0,
                    "is_preferred": False,
                }
            )
    return pd.DataFrame(rows)


def _allocation_rows(skip_code=None):
    rows = []
    dates = {
        "2020-01-01": {"A": (100.0, 1.0), "B": (100.0, 3.0)},
        "2020-01-02": {"A": (10.0, 999.0), "B": (20.0, 1.0)},
        "2020-01-31": {"A": (11.0, 1.0), "B": (21.0, 3.0)},
    }
    for date, values in dates.items():
        for code, (close, market_cap) in values.items():
            rows.append(
                {
                    "date": pd.Timestamp(date),
                    "market": "KOSPI",
                    "code": code,
                    "close": close,
                    "market_cap": market_cap,
                    "trading_val": 0.0 if code == skip_code and date == "2020-01-02" else 10_000.0,
                    "per": 1.0 if code == "A" else 2.0,
                    "pbr": 1.0,
                    "div": 1.0,
                    "bps": 100.0,
                    "eps": 100.0,
                    "is_preferred": False,
                }
            )
    return pd.DataFrame(rows)


def _turnover_allocation_rows():
    rows = []
    dates = {
        "2020-01-01": {"A": 1.0, "B": 2.0, "C": 3.0},
        "2020-01-02": {"A": 1.0, "B": 2.0, "C": 3.0},
        "2020-01-31": {"A": 1.0, "B": 2.0, "C": 3.0},
        "2020-02-01": {"A": 3.0, "B": 1.0, "C": 2.0},
        "2020-02-03": {"A": 3.0, "B": 1.0, "C": 2.0},
        "2020-02-28": {"A": 3.0, "B": 1.0, "C": 2.0},
    }
    caps = {"A": 1.0, "B": 3.0, "C": 2.0}
    for date, pers in dates.items():
        for code, per in pers.items():
            rows.append(
                {
                    "date": pd.Timestamp(date),
                    "market": "KOSPI",
                    "code": code,
                    "close": 10.0,
                    "market_cap": caps[code],
                    "trading_val": 10_000.0,
                    "per": per,
                    "pbr": 1.0,
                    "div": 1.0,
                    "bps": 100.0,
                    "eps": 100.0,
                    "is_preferred": False,
                }
            )
    return pd.DataFrame(rows)


def _allocation_config(**kwargs):
    values = dict(
        start_date="2020-01-01",
        end_date="2020-01-31",
        initial_capital=1_000,
        buy_cost=0.0,
        sell_cost=0.0,
        slippage=0.0,
        n_stocks=2,
        min_market_cap=1,
        min_trading_val=0,
        use_multi_factor=False,
        use_market_regime=False,
        execution_mode="next_close",
    )
    values.update(kwargs)
    return Config(**values)


def _run_phase2(monkeypatch, data, config, stats=None, regime=None):
    monkeypatch.setattr(engine, "get_korean_business_days", _phase2_calendar)
    if regime is None:
        monkeypatch.setattr(engine, "_fetch_kospi_for_ma", lambda *args: pd.DataFrame())
    else:
        monkeypatch.setattr(engine, "_fetch_kospi_for_ma", lambda *args: regime)
    return run_backtest(data, config, track_stats=stats)


REQUIRED_EVENT_FIELDS = {
    "rebalance_id",
    "signal_date",
    "execution_date",
    "code",
    "market",
    "side",
    "event_type",
    "classification",
    "disposition",
    "close",
    "trading_val",
    "requested_krw",
    "order_value",
}


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


class TestPhase2NextCloseIntegration:
    def test_t1_price_is_used_and_events_are_complete(self, monkeypatch):
        dates = {
            "2020-01-02": {"A": 10.0, "B": 20.0},
            "2020-01-03": {"A": 100.0, "B": 200.0},
            "2020-01-31": {"A": 110.0, "B": 210.0},
            "2020-02-03": {"A": 300.0, "B": 30.0},
            "2020-02-04": {"A": 400.0, "B": 40.0},
            "2020-02-28": {"A": 400.0, "B": 40.0},
        }
        monkeypatch.setattr(engine, "_fetch_kospi_for_ma", lambda *args: pd.DataFrame())
        stats = {}
        history, _ = run_backtest(
            _make_v2_rows(dates), _next_close_config(), track_stats=stats
        )
        assert history["Portfolio_Value"].iloc[1] == 110_000
        assert any(
            event["side"] == "BUY"
            and event["disposition"] == "ORDER_EXECUTED"
            and event["close"] == 100.0
            for event in stats["events"]
        )
        required = {
            "signal_date", "execution_date", "code", "market", "side",
            "event_type", "classification", "disposition", "close",
            "trading_val", "requested_krw", "order_value", "rebalance_id",
        }
        assert all(required <= set(event) for event in stats["events"])

    def test_exact_t1_absence_and_end_horizon_are_deterministic(self, monkeypatch):
        monkeypatch.setattr(engine, "_fetch_kospi_for_ma", lambda *args: pd.DataFrame())
        later_only = _make_v2_rows(
            {
                "2020-01-02": {"A": 10.0},
                "2020-01-31": {"A": 11.0},
                "2020-02-04": {"A": 40.0},
            }
        )
        with pytest.raises(ValueError, match=r"exact T\+1"):
            run_backtest(later_only, _next_close_config(end_date="2020-02-28"))

        horizon = _make_v2_rows({"2020-01-02": {"A": 10.0}, "2020-01-03": {"A": 20.0}})
        stats = {}
        run_backtest(horizon, _next_close_config(end_date="2020-01-02"), track_stats=stats)
        assert any(event["event_type"] == "REBALANCE_SKIPPED_END_HORIZON" for event in stats["events"])

    def test_missing_and_zero_volume_buy_are_skipped_without_fallback(self, monkeypatch):
        monkeypatch.setattr(engine, "_fetch_kospi_for_ma", lambda *args: pd.DataFrame())
        data = _make_v2_rows(
            {"2020-01-02": {"A": 10.0, "B": 20.0}, "2020-01-03": {"A": 100.0}, "2020-01-31": {"A": 100.0}},
            trading_values={("2020-01-03", "A"): 0.0},
        )
        stats = {}
        run_backtest(data, _next_close_config(n_stocks=2), track_stats=stats)
        assert any(event["event_type"] == "ABSENT_EXECUTION_ROW" for event in stats["events"])
        assert any(event["event_type"] == "ZERO_TRADING_VALUE" for event in stats["events"])

    def test_default_and_explicit_same_close_match(self, monkeypatch):
        monkeypatch.setattr(engine, "_fetch_kospi_for_ma", lambda *args: pd.DataFrame())
        fd, ld = _make_month_dates(2020, 1)
        data = pd.DataFrame(_make_stock_rows([fd, ld], ["000010"], 10000))
        default = Config(
            start_date="2020-01-01", end_date="2020-01-31", initial_capital=100000,
            buy_cost=0, sell_cost=0, slippage=0, n_stocks=1,
            min_market_cap=1, min_trading_val=1, use_multi_factor=False,
        )
        explicit = Config(**{**default.__dict__, "execution_mode": "same_close"})
        left, lm = run_backtest(data, default)
        right, rm = run_backtest(data, explicit)
        assert left.equals(right)
        assert lm == rm

    def test_normal_next_close_sells_have_t1_market_and_amount_provenance(
        self, monkeypatch
    ):
        stats = {}
        _run_phase2(
            monkeypatch,
            _phase2_rows(),
            _next_close_config(initial_capital=1_000, n_stocks=2),
            stats,
        )

        sells = {
            event["code"]: event
            for event in stats["events"]
            if event["side"] == "SELL"
            and event["disposition"] == "ORDER_EXECUTED"
        }
        assert set(sells) == {"A", "B"}
        assert {
            code: (event["execution_date"], event["close"], event["trading_val"])
            for code, event in sells.items()
        } == {
            "A": ("2020-02-03", 30.0, 700.0),
            "B": ("2020-02-03", 40.0, 10_000.0),
        }
        assert all(event["market"] == "KOSPI" for event in sells.values())
        assert all(event["side"] == "SELL" for event in sells.values())
        assert all(event["order_value"] == event["requested_krw"] for event in sells.values())
        assert sells["A"]["order_value"] == pytest.approx(50 * 30.0)
        assert sells["B"]["order_value"] == pytest.approx(25 * 40.0)
        assert all(REQUIRED_EVENT_FIELDS <= set(event) for event in stats["events"])

    def test_bear_liquidation_sells_emit_successful_t1_events(self, monkeypatch):
        regime = pd.DataFrame(
            {"kospi_close": [100.0, 50.0]},
            index=pd.to_datetime(["2020-01-01", "2020-02-01"]),
        )
        regime.index.name = "date"
        stats = {}
        _run_phase2(
            monkeypatch,
            _phase2_rows(),
            _next_close_config(
                initial_capital=1_000,
                n_stocks=2,
                use_market_regime=True,
                ma_window=2,
            ),
            stats,
            regime=regime,
        )

        sells = [
            event
            for event in stats["events"]
            if event["side"] == "SELL"
            and event["disposition"] == "ORDER_EXECUTED"
            and event["signal_date"] == "2020-02-01"
        ]
        assert {event["code"] for event in sells} == {"A", "B"}
        assert {
            event["code"]: (event["market"], event["execution_date"], event["close"], event["trading_val"], event["order_value"])
            for event in sells
        } == {
            "A": ("KOSPI", "2020-02-03", 30.0, 700.0, 1_500.0),
            "B": ("KOSPI", "2020-02-03", 40.0, 10_000.0, 1_000.0),
        }
        assert all(REQUIRED_EVENT_FIELDS <= set(event) for event in sells)

    @pytest.mark.parametrize("sell_mode", ["absent", "zero"])
    def test_sell_skip_is_complete_and_preserves_holding_without_cash_proceeds(
        self, monkeypatch, sell_mode
    ):
        stats = {}
        history, _ = _run_phase2(
            monkeypatch,
            _phase2_rows(sell_mode),
            _next_close_config(initial_capital=1_000, n_stocks=1),
            stats,
        )
        skipped = next(
            event
            for event in stats["events"]
            if event["code"] == "A"
            and event["side"] == "SELL"
            and event["disposition"] == "ORDER_SKIPPED"
        )
        assert REQUIRED_EVENT_FIELDS <= set(skipped)
        assert skipped["rebalance_id"] == "2020-02-01->2020-02-03"
        assert skipped["execution_date"] == "2020-02-03"
        assert skipped["market"] == "KOSPI"
        assert skipped["side"] == "SELL"
        assert skipped["order_value"] == 0.0
        if sell_mode == "absent":
            assert skipped["event_type"] == "ABSENT_EXECUTION_ROW"
            assert skipped["close"] is None
            assert skipped["trading_val"] is None
        else:
            assert skipped["event_type"] == "ZERO_TRADING_VALUE"
            assert skipped["close"] == 30.0
            assert skipped["trading_val"] == 0.0
        feb_record = next(
            record
            for record in stats["rebalances"]
            if record["signal_date"] == "2020-02-01"
        )
        assert feb_record["cash_post"] == pytest.approx(feb_record["cash_pre"])
        assert "A" in feb_record["post_values"]
        assert history["Stock_Count"].iloc[-1] == 1
        assert history["Stock_Count"].iloc[-1] <= 1
        assert classify_phase2_stats(stats) == "INCONCLUSIVE_DATA_LIMITATION"

    @pytest.mark.parametrize("sell_mode", ["absent", "zero"])
    def test_bear_sell_skip_preserves_position_cash_and_has_market_fields(
        self, monkeypatch, sell_mode
    ):
        regime = pd.DataFrame(
            {"kospi_close": [100.0, 50.0]},
            index=pd.to_datetime(["2020-01-01", "2020-02-01"]),
        )
        regime.index.name = "date"
        stats = {}
        history, _ = _run_phase2(
            monkeypatch,
            _phase2_rows(sell_mode),
            _next_close_config(
                initial_capital=1_000,
                n_stocks=1,
                use_market_regime=True,
                ma_window=2,
            ),
            stats,
            regime=regime,
        )
        skipped = next(
            event
            for event in stats["events"]
            if event["code"] == "A"
            and event["side"] == "SELL"
            and event["disposition"] == "ORDER_SKIPPED"
        )
        assert REQUIRED_EVENT_FIELDS <= set(skipped)
        assert skipped["market"] == "KOSPI"
        assert skipped["side"] == "SELL"
        assert skipped["order_value"] == 0.0
        feb_record = next(
            record
            for record in stats["rebalances"]
            if record["signal_date"] == "2020-02-01"
        )
        assert feb_record["cash_post"] == pytest.approx(feb_record["cash_pre"])
        assert "A" in feb_record["post_values"]
        assert set(feb_record["target_values"]) == {"CASH"}
        assert feb_record["target_values"]["CASH"] == pytest.approx(
            feb_record["nav_pre"]
        )
        assert feb_record["l1_target_error"] > 0
        assert history["Stock_Count"].iloc[-1] == 1
        if sell_mode == "zero":
            assert feb_record["post_values"]["A"] == pytest.approx(100 * 30.0)

    def test_skipped_partial_sell_is_retained_without_duplicate_or_over_limit(self, monkeypatch):
        rows = []
        prices = {
            "2020-01-01": {code: 10.0 for code in "ABCD"},
            "2020-01-02": {code: 10.0 for code in "ABCD"},
            "2020-01-31": {code: 10.0 for code in "ABCD"},
            "2020-02-01": {code: 10.0 for code in "ABCD"},
            "2020-02-03": {code: 10.0 for code in "ABD"},
            "2020-02-28": {code: 10.0 for code in "ABCD"},
        }
        for date, closes in prices.items():
            for code, close in closes.items():
                rows.append(
                    {
                        "date": pd.Timestamp(date),
                        "market": "KOSPI",
                        "code": code,
                        "close": close,
                        "market_cap": 1_000_000_000.0,
                        "trading_val": 10_000.0,
                        "per": {"A": 1.0, "B": 2.0, "C": 3.0, "D": 4.0}[code]
                        if date < "2020-02-01"
                        else {"A": 1.0, "B": 2.0, "C": 4.0, "D": 3.0}[code],
                        "pbr": 1.0,
                        "div": 1.0,
                        "bps": 100.0,
                        "eps": 100.0,
                        "is_preferred": False,
                    }
                )
        stats = {}
        history, _ = _run_phase2(
            monkeypatch,
            pd.DataFrame(rows),
            _next_close_config(
                initial_capital=300,
                n_stocks=3,
                max_turnover=0.5,
            ),
            stats,
        )
        skipped = next(
            event
            for event in stats["events"]
            if event["code"] == "C"
            and event["side"] == "SELL"
            and event["disposition"] == "ORDER_SKIPPED"
        )
        assert skipped["event_type"] == "ABSENT_EXECUTION_ROW"
        feb_record = next(
            record
            for record in stats["rebalances"]
            if record["signal_date"] == "2020-02-01"
        )
        post_codes = set(feb_record["post_values"]) - {"CASH"}
        assert "C" in post_codes
        assert len(post_codes) == len(set(post_codes)) == 3
        assert history["Stock_Count"].iloc[-1] == 3
        assert history["Stock_Count"].iloc[-1] <= 3

    def test_horizon_telemetry_values_existing_holdings_and_history_are_valid(
        self, monkeypatch
    ):
        stats = {}
        history, _ = _run_phase2(
            monkeypatch,
            _phase2_rows(),
            _next_close_config(initial_capital=1_000, end_date="2020-02-01"),
            stats,
        )
        horizon = next(
            record
            for record in stats["rebalances"]
            if record["execution_date"] == "END_HORIZON"
        )
        assert horizon["mode"] == "next_close"
        assert horizon["signal_date"] == "2020-02-01"
        assert horizon["nav_pre"] == pytest.approx(sum(horizon["pre_values"].values()))
        assert horizon["pre_values"]["A"] > 0
        assert horizon["post_values"]["A"] == horizon["pre_values"]["A"]
        assert not horizon["telemetry_complete"]
        assert classify_phase2_stats(stats) == "INCONCLUSIVE_DATA_LIMITATION"
        dates = pd.to_datetime(history["Date"])
        assert dates.is_monotonic_increasing
        assert dates.max() == pd.Timestamp("2020-02-01")
        assert history["Portfolio_Value"].iloc[-1] == pytest.approx(1_200.0)

    def test_actual_engine_stats_validate_and_telemetry_formulas_are_independent(
        self, monkeypatch
    ):
        stats = {}
        _run_phase2(
            monkeypatch,
            _phase2_rows(),
            _next_close_config(initial_capital=1_000, n_stocks=2),
            stats,
        )
        assert validate_phase2_stats(stats) == []
        assert classify_phase2_stats(stats) == "PASS"

        for record in stats["rebalances"]:
            nav_pre = record["nav_pre"]
            nav_post = record["nav_post"]
            assert sum(record["target_values"].values()) == pytest.approx(nav_pre)
            for values, total, weights, hhi in (
                (
                    record["target_values"],
                    nav_pre,
                    record["target_weights"],
                    None,
                ),
                (record["pre_values"], nav_pre, record["pre_weights"], record["hhi_pre"]),
                (record["post_values"], nav_post, record["post_weights"], record["hhi_post"]),
            ):
                expected_weights = {code: value / total for code, value in values.items() if value}
                if "CASH" not in expected_weights:
                    expected_weights["CASH"] = 0.0
                assert weights == pytest.approx(expected_weights)
                if hhi is not None:
                    assert hhi == pytest.approx(
                        sum(weight * weight for weight in expected_weights.values())
                    )
            expected_turnover = 0.5 * sum(
                abs(
                    record["post_values"].get(code, 0.0)
                    - record["pre_values"].get(code, 0.0)
                )
                for code in set(record["pre_values"]) | set(record["post_values"])
            )
            assert record["one_way_turnover_krw"] == pytest.approx(expected_turnover)
            assert record["one_way_turnover_pct"] == pytest.approx(
                expected_turnover / nav_pre
            )
            expected_l1 = sum(
                abs(
                    record["post_weights"].get(code, 0.0)
                    - record["target_weights"].get(code, 0.0)
                )
                for code in set(record["post_weights"]) | set(record["target_weights"])
            )
            assert record["l1_target_error"] == pytest.approx(expected_l1)

    def test_quarterly_missing_valuation_links_to_previous_rebalance(
        self, monkeypatch
    ):
        data = _phase2_rows()
        data = data[
            ~(
                (data["date"] == pd.Timestamp("2020-02-28"))
                & (data["code"] == "A")
            )
        ]
        stats = {}
        _run_phase2(
            monkeypatch,
            data,
            _next_close_config(
                initial_capital=1_000,
                n_stocks=1,
                rebalance_freq="quarterly",
            ),
            stats,
        )
        valuation = next(
            event
            for event in stats["events"]
            if event["disposition"] == "VALUATION_INCOMPLETE"
        )
        assert valuation["rebalance_id"] == "2020-01-01->2020-01-02"
        assert valuation["rebalance_id"] in {
            record["rebalance_id"] for record in stats["rebalances"]
        }
        assert classify_phase2_stats(stats) == "INCONCLUSIVE_DATA_LIMITATION"

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


class TestPhase3MatchedAllocation:
    def _run(self, monkeypatch, data, allocation_mode="equal_weight", **kwargs):
        monkeypatch.setattr(engine, "get_korean_business_days", _phase2_calendar)
        monkeypatch.setattr(engine, "_fetch_kospi_for_ma", lambda *args: pd.DataFrame())
        stats = {}
        history, metrics = run_backtest(
            data,
            _allocation_config(allocation_mode=allocation_mode, **kwargs),
            track_stats=stats,
        )
        return history, metrics, stats

    def test_default_and_explicit_equal_weight_are_identical(self, monkeypatch):
        data = _allocation_rows()
        monkeypatch.setattr(engine, "get_korean_business_days", _phase2_calendar)
        monkeypatch.setattr(engine, "_fetch_kospi_for_ma", lambda *args: pd.DataFrame())
        default_stats, explicit_stats = {}, {}
        default_history, default_metrics = run_backtest(
            data,
            _allocation_config(),
            track_stats=default_stats,
        )
        explicit_history, explicit_metrics = run_backtest(
            data,
            _allocation_config(allocation_mode="equal_weight"),
            track_stats=explicit_stats,
        )
        assert default_history.equals(explicit_history)
        assert default_metrics == explicit_metrics
        assert default_stats == explicit_stats

    def test_market_cap_budget_uses_signal_caps_not_execution_caps(self, monkeypatch):
        _, _, equal_stats = self._run(monkeypatch, _allocation_rows(), "equal_weight")
        _, _, cap_stats = self._run(monkeypatch, _allocation_rows(), "market_cap_weight")
        equal_buys = {
            event["code"]: event
            for event in equal_stats["events"]
            if event["side"] == "BUY"
        }
        cap_buys = {
            event["code"]: event
            for event in cap_stats["events"]
            if event["side"] == "BUY"
        }
        assert {code: event["requested_krw"] for code, event in equal_buys.items()} == {
            "A": pytest.approx(500.0),
            "B": pytest.approx(500.0),
        }
        assert {code: event["requested_krw"] for code, event in cap_buys.items()} == {
            "A": pytest.approx(250.0),
            "B": pytest.approx(750.0),
        }
        cap_record = cap_stats["rebalances"][0]
        assert cap_record["target_weights"]["A"] == pytest.approx(0.25)
        assert cap_record["target_weights"]["B"] == pytest.approx(0.75)
        assert cap_buys["A"]["close"] == 10.0
        assert cap_buys["B"]["close"] == 20.0

    def test_allocation_modes_have_identical_selector_identity(self, monkeypatch):
        _, _, equal_stats = self._run(monkeypatch, _allocation_rows(), "equal_weight")
        _, _, cap_stats = self._run(monkeypatch, _allocation_rows(), "market_cap_weight")
        fields = (
            "signal_date",
            "execution_date",
            "target_codes",
            "retained_codes",
            "sell_codes",
            "new_codes",
        )
        assert [
            tuple(record[field] for field in fields)
            for record in equal_stats["rebalances"]
        ] == [
            tuple(record[field] for field in fields)
            for record in cap_stats["rebalances"]
        ]

    def test_partial_turnover_retains_existing_shares_and_only_budgets_new_codes(
        self, monkeypatch
    ):
        history, _, stats = self._run(
            monkeypatch,
            _turnover_allocation_rows(),
            "market_cap_weight",
            end_date="2020-02-28",
            n_stocks=2,
            max_turnover=0.5,
        )
        feb = next(
            record for record in stats["rebalances"] if record["signal_date"] == "2020-02-01"
        )
        assert feb["retained_codes"] == ["B"]
        assert feb["sell_codes"] == ["A"]
        assert feb["new_codes"] == ["C"]
        assert feb["pre_values"]["B"] == pytest.approx(feb["post_values"]["B"])
        assert not any(
            event["code"] == "B"
            and event["side"] == "BUY"
            and event["rebalance_id"] == feb["rebalance_id"]
            for event in stats["events"]
        )
        assert history["Stock_Count"].iloc[-1] == 2

    def test_skipped_buy_keeps_other_budget_and_cash_unspent(self, monkeypatch):
        _, _, stats = self._run(
            monkeypatch,
            _allocation_rows(skip_code="A"),
            "equal_weight",
        )
        skipped = next(
            event
            for event in stats["events"]
            if event["code"] == "A" and event["disposition"] == "ORDER_SKIPPED"
        )
        bought = next(
            event
            for event in stats["events"]
            if event["code"] == "B" and event["disposition"] == "ORDER_EXECUTED"
        )
        record = stats["rebalances"][0]
        assert skipped["event_type"] == "ZERO_TRADING_VALUE"
        assert bought["requested_krw"] == pytest.approx(500.0)
        assert record["cash_post"] == pytest.approx(500.0)

    @pytest.mark.parametrize("invalid_cap", [0.0, -1.0, float("inf"), "not-a-cap"])
    def test_invalid_signal_market_cap_fails_closed(self, monkeypatch, invalid_cap):
        data = _allocation_rows()
        if isinstance(invalid_cap, str):
            data["market_cap"] = data["market_cap"].astype(object)
        data.loc[
            (data["date"] == pd.Timestamp("2020-01-01"))
            & (data["code"] == "B"),
            "market_cap",
        ] = invalid_cap
        monkeypatch.setattr(engine, "get_korean_business_days", _phase2_calendar)
        monkeypatch.setattr(engine, "_fetch_kospi_for_ma", lambda *args: pd.DataFrame())
        with pytest.raises(ValueError, match="finite positive"):
            run_backtest(
                data,
                _allocation_config(
                    allocation_mode="market_cap_weight", min_market_cap=-2
                ),
            )


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

    def test_market_regime_nan_does_not_liquidate(self, tmp_path, monkeypatch):
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
        monkeypatch.setattr(
            engine.stock,
            "get_index_ohlcv_by_date",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("network access")
            ),
        )

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

    def test_cache_only_regime_uses_sufficient_cache_without_network(
        self, tmp_path, monkeypatch
    ):
        cache_dir = tmp_path / ".cache" / "backtest"
        cache_dir.mkdir(parents=True, exist_ok=True)
        kospi = pd.DataFrame(
            {"kospi_close": 100.0},
            index=pd.date_range("2018-01-02", periods=1_000, freq="B"),
        )
        kospi.index.name = "date"
        kospi.to_parquet(cache_dir / "kospi_ma.parquet")

        fd_jan, ld_jan = _make_month_dates(2020, 1)
        rows = _make_stock_rows([fd_jan, ld_jan], ["000010"], 10000)
        config = Config(
            start_date="2020-01-01",
            end_date="2020-01-31",
            initial_capital=100_000,
            buy_cost=0.0,
            sell_cost=0.0,
            slippage=0.0,
            n_stocks=1,
            min_market_cap=1,
            min_trading_val=0,
            use_multi_factor=False,
            use_market_regime=True,
        )

        def forbidden(*_args, **_kwargs):
            raise AssertionError("network access")

        monkeypatch.setattr(engine.stock, "get_index_ohlcv_by_date", forbidden)
        history, _ = run_backtest(
            pd.DataFrame(rows), config, cache_dir=cache_dir, cache_only=True
        )
        assert history["Stock_Count"].iloc[1] == 1

    def test_cache_only_regime_fails_closed_when_cache_is_missing(
        self, tmp_path, monkeypatch
    ):
        fd_jan, ld_jan = _make_month_dates(2020, 1)
        rows = _make_stock_rows([fd_jan, ld_jan], ["000010"], 10000)
        config = Config(
            start_date="2020-01-01",
            end_date="2020-01-31",
            initial_capital=100_000,
            buy_cost=0.0,
            sell_cost=0.0,
            slippage=0.0,
            n_stocks=1,
            min_market_cap=1,
            min_trading_val=0,
            use_multi_factor=False,
            use_market_regime=True,
        )

        def forbidden(*_args, **_kwargs):
            raise AssertionError("network access")

        monkeypatch.setattr(engine.stock, "get_index_ohlcv_by_date", forbidden)
        with pytest.raises(ValueError, match="cache-only KOSPI regime unavailable"):
            run_backtest(
                pd.DataFrame(rows),
                config,
                cache_dir=tmp_path / "missing",
                cache_only=True,
            )

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
