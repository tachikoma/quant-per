import hashlib
import json
from collections import Counter
from unittest.mock import Mock, patch

import pandas as pd
import pytest

import engine
from config import Config


DATES = pd.to_datetime(
    ["2024-01-02", "2024-01-31", "2024-02-01", "2024-02-29"]
)


def _calendar(start, end):
    return pd.bdate_range(start, end)


def _cap(date, market=None):
    codes = ["000010", "000020"] if market == "KOSPI" else ["000110", "000120"]
    frame = pd.DataFrame(
        {
            "종가": [100.0, 200.0],
            "시가총액": [1000.0, 2000.0],
            "거래대금": [100.0, 200.0],
        },
        index=codes,
    )
    frame.index.name = "티커"
    return frame


def _fundamental(date, market=None):
    codes = ["000010", "000020"] if market == "KOSPI" else ["000110", "000120"]
    frame = pd.DataFrame(
        {
            "PER": [1.0, 2.0],
            "PBR": [0.5, 0.6],
            "DIV": [1.0, 2.0],
            "BPS": [100.0, 200.0],
            "EPS": [10.0, 20.0],
        },
        index=codes,
    )
    frame.index.name = "티커"
    return frame


def _api_mocks(monkeypatch):
    cap = Mock(side_effect=_cap)
    fundamental = Mock(side_effect=_fundamental)
    monkeypatch.setattr(engine.stock, "get_market_cap", cap)
    monkeypatch.setattr(engine.stock, "get_market_fundamental", fundamental)
    monkeypatch.setattr(engine, "get_korean_business_days", _calendar)
    return cap, fundamental


def _fetch(tmp_path, start="2024-01-01", end="2024-01-31", monkeypatch=None, **kwargs):
    if monkeypatch is not None:
        _api_mocks(monkeypatch)
    return engine.fetch_rebalancing_data(
        start,
        end,
        cache_dir=tmp_path,
        use_market_data_v2=True,
        **kwargs,
    )


def _manifest_path(tmp_path):
    return tmp_path / "market_data_v2" / "manifest.json"


def _month_path(tmp_path, market="KOSPI", month="2024-01"):
    return tmp_path / "market_data_v2" / market / f"{month}.parquet"


def _rewrite_inventory(tmp_path, market, month, frame):
    path = _month_path(tmp_path, market, month)
    frame.to_parquet(path, index=False)
    manifest_file = _manifest_path(tmp_path)
    manifest = json.loads(manifest_file.read_text())
    dates = sorted(pd.to_datetime(frame["date"]).dt.strftime("%Y-%m-%d").unique())
    manifest["inventory"][market][month] = {
        "dates": dates,
        "row_count": len(frame),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    manifest_file.write_text(json.dumps(manifest))


def test_explicit_market_join_and_cache_hit_do_not_call_pykrx(tmp_path, monkeypatch):
    cap, fundamental = _api_mocks(monkeypatch)

    first = _fetch(tmp_path, monkeypatch=None)
    assert set(first["market"]) == {"KOSPI", "KOSDAQ"}
    expected_calls = Counter(
        {
            ("20240101", "KOSPI"): 1,
            ("20240101", "KOSDAQ"): 1,
            ("20240131", "KOSPI"): 1,
            ("20240131", "KOSDAQ"): 1,
        }
    )
    assert Counter(
        (call.args[0], call.kwargs["market"]) for call in cap.call_args_list
    ) == expected_calls
    assert Counter(
        (call.args[0], call.kwargs["market"])
        for call in fundamental.call_args_list
    ) == expected_calls

    cap.reset_mock()
    fundamental.reset_mock()
    second = _fetch(tmp_path, monkeypatch=None)
    loaded = engine.load_market_data_v2(
        tmp_path, "2024-01-01", "2024-01-31"
    )
    assert len(second) == len(first) == len(loaded)
    cap.assert_not_called()
    fundamental.assert_not_called()


def test_next_close_cache_contains_signal_exact_t1_and_last_date(tmp_path, monkeypatch):
    cap, fundamental = _api_mocks(monkeypatch)
    _fetch(
        tmp_path,
        "2024-01-01",
        "2024-02-29",
        monkeypatch=None,
        execution_mode="next_close",
    )
    for market in ("KOSPI", "KOSDAQ"):
        frame = pd.concat(
            [pd.read_parquet(_month_path(tmp_path, market, "2024-01")),
             pd.read_parquet(_month_path(tmp_path, market, "2024-02"))]
        )
        assert {pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-02"),
                pd.Timestamp("2024-01-31"), pd.Timestamp("2024-02-01"),
                pd.Timestamp("2024-02-29")} <= set(pd.to_datetime(frame["date"]))
    cap.reset_mock()
    fundamental.reset_mock()
    loaded = engine.load_market_data_v2(
        tmp_path, "2024-01-01", "2024-02-29", execution_mode="next_close"
    )
    assert not loaded.empty
    cap.assert_not_called()
    fundamental.assert_not_called()


def test_same_close_target_plan_is_unchanged_and_next_close_crosses_month_boundary(
    monkeypatch,
):
    monkeypatch.setattr(engine, "get_korean_business_days", _calendar)
    same = engine._v2_target_dates("2024-01-01", "2024-01-31")
    next_dates = engine._v2_target_dates(
        "2024-01-01", "2024-01-31", execution_mode="next_close"
    )
    assert same == (pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-31"))
    assert pd.Timestamp("2024-01-02") in next_dates
    assert pd.Timestamp("2024-01-31") in next_dates


def test_market_cap_left_join_discards_fundamental_only_ticker():
    cap = _cap("20240102", "KOSPI")
    fund = _fundamental("20240102", "KOSPI")
    fund.loc["000020", :] = [3.0, 0.7, 3.0, 300.0, 30.0]
    fund.loc["000030", :] = [4.0, 0.8, 4.0, 400.0, 40.0]
    merged = engine._merge_v2_market_snapshot(cap, fund, "2024-01-02", "KOSPI")
    assert set(merged["code"]) == {"000010", "000020"}
    assert list(merged.columns) == list(engine.V2_COLUMNS)


def test_partial_month_and_month_boundary_fetch_only_missing_target_dates(
    tmp_path, monkeypatch
):
    cap, fundamental = _api_mocks(monkeypatch)
    _fetch(tmp_path, "2024-01-01", "2024-02-15", monkeypatch=None)
    cap.reset_mock()
    fundamental.reset_mock()

    _fetch(tmp_path, "2024-01-15", "2024-02-29", monkeypatch=None)
    requested = Counter(
        (call.args[0], call.kwargs["market"]) for call in cap.call_args_list
    )
    assert requested == Counter(
        {
            ("20240115", "KOSPI"): 1,
            ("20240115", "KOSDAQ"): 1,
            ("20240229", "KOSPI"): 1,
            ("20240229", "KOSDAQ"): 1,
        }
    )
    assert len(fundamental.call_args_list) == 4


def test_zero_trading_value_is_stored_loaded_and_backtest_compatible(
    tmp_path, monkeypatch
):
    def zero_cap(date, market=None):
        frame = _cap(date, market)
        if market == "KOSPI":
            frame.loc["000010", "거래대금"] = 0.0
        return frame

    monkeypatch.setattr(engine.stock, "get_market_cap", Mock(side_effect=zero_cap))
    monkeypatch.setattr(
        engine.stock, "get_market_fundamental", Mock(side_effect=_fundamental)
    )
    monkeypatch.setattr(engine, "get_korean_business_days", _calendar)
    data = _fetch(tmp_path, monkeypatch=None)
    assert (data.loc[(data["market"] == "KOSPI") & (data["code"] == "000010"), "trading_val"] == 0).all()

    loaded = engine.load_market_data_v2(tmp_path, "2024-01-01", "2024-01-31")
    assert (
        loaded.loc[
            (loaded["market"] == "KOSPI") & (loaded["code"] == "000010"),
            "trading_val",
        ]
        == 0
    ).all()
    config = Config(
        start_date="2024-01-01",
        end_date="2024-01-31",
        initial_capital=100_000,
        buy_cost=0.0,
        sell_cost=0.0,
        slippage=0.0,
        n_stocks=1,
        min_market_cap=1,
        min_trading_val=0,
        use_multi_factor=False,
        use_market_regime=False,
    )
    monkeypatch.setattr(engine, "_fetch_kospi_for_ma", lambda *args: pd.DataFrame())
    history, _metrics = engine.run_backtest(loaded, config, cache_dir=tmp_path)
    assert not history.empty


def test_target_dates_filter_today_before_month_boundaries(monkeypatch):
    monkeypatch.setattr(engine, "_v2_today", lambda: pd.Timestamp("2024-02-15"))

    calendar_dates = pd.to_datetime(
        [
            "2024-02-01",
            "2024-02-14",
            "2024-02-15",
            "2024-02-16",
            "2024-03-04",
            "2024-03-29",
        ]
    )
    monkeypatch.setattr(
        engine,
        "get_korean_business_days",
        lambda start, end: calendar_dates[
            (calendar_dates >= pd.Timestamp(start))
            & (calendar_dates <= pd.Timestamp(end))
        ],
    )
    assert engine._v2_target_dates("2024-02-01", "2024-02-29") == (
        pd.Timestamp("2024-02-01"),
        pd.Timestamp("2024-02-14"),
    )
    assert engine._v2_target_dates("2024-03-01", "2024-03-31") == ()


def test_end_today_and_in_progress_month_use_last_available_date(monkeypatch):
    monkeypatch.setattr(engine, "_v2_today", lambda: pd.Timestamp("2024-02-15"))
    monkeypatch.setattr(
        engine,
        "get_korean_business_days",
        lambda _start, _end: pd.to_datetime(
            ["2024-02-01", "2024-02-14", "2024-02-15"]
        ),
    )
    assert engine._v2_target_dates("2024-02-01", "2024-02-15") == (
        pd.Timestamp("2024-02-01"),
        pd.Timestamp("2024-02-14"),
    )


@pytest.mark.parametrize("tamper", ["missing_column", "duplicate", "wrong_month"])
def test_loader_rejects_schema_or_month_tampering(tmp_path, monkeypatch, tamper):
    _fetch(tmp_path, monkeypatch=monkeypatch)
    path = _month_path(tmp_path)
    frame = pd.read_parquet(path)
    if tamper == "missing_column":
        frame = frame.drop(columns=["eps"])
    elif tamper == "duplicate":
        frame = pd.concat([frame, frame.iloc[[0]]], ignore_index=True)
    else:
        frame.loc[0, "date"] = pd.Timestamp("2024-02-01")
    _rewrite_inventory(tmp_path, "KOSPI", "2024-01", frame)
    with pytest.raises(ValueError):
        engine.load_market_data_v2(tmp_path, "2024-01-01", "2024-01-31")


@pytest.mark.parametrize("date_tamper", ["timezone", "intraday"])
def test_loader_rejects_non_date_only_parquet(tmp_path, monkeypatch, date_tamper):
    _fetch(tmp_path, monkeypatch=monkeypatch)
    path = _month_path(tmp_path)
    frame = pd.read_parquet(path)
    if date_tamper == "timezone":
        frame["date"] = pd.to_datetime(frame["date"]).dt.tz_localize("Asia/Seoul")
    else:
        frame.loc[0, "date"] = pd.Timestamp("2024-01-01 12:00:00")
    _rewrite_inventory(tmp_path, "KOSPI", "2024-01", frame)
    with pytest.raises(ValueError, match="date"):
        engine.load_market_data_v2(tmp_path, "2024-01-01", "2024-01-31")


def test_loader_rejects_numeric_string_fundamental(tmp_path, monkeypatch):
    _fetch(tmp_path, monkeypatch=monkeypatch)
    path = _month_path(tmp_path)
    frame = pd.read_parquet(path)
    frame["per"] = frame["per"].map(lambda value: str(value))
    _rewrite_inventory(tmp_path, "KOSPI", "2024-01", frame)
    with pytest.raises(ValueError, match="numeric dtype"):
        engine.load_market_data_v2(tmp_path, "2024-01-01", "2024-01-31")


def test_loader_rejects_cross_market_duplicate_code(tmp_path, monkeypatch):
    _fetch(tmp_path, monkeypatch=monkeypatch)
    path = _month_path(tmp_path, "KOSDAQ")
    frame = pd.read_parquet(path)
    frame["code"] = frame["code"].replace({"000110": "000010"})
    _rewrite_inventory(tmp_path, "KOSDAQ", "2024-01", frame)
    with pytest.raises(ValueError, match="date, code"):
        engine.load_market_data_v2(tmp_path, "2024-01-01", "2024-01-31")


def test_loader_rejects_requested_cross_market_date_set_mismatch(
    tmp_path, monkeypatch
):
    _fetch(tmp_path, monkeypatch=monkeypatch)
    path = _month_path(tmp_path, "KOSPI")
    frame = pd.read_parquet(path)
    extra = frame.iloc[[0]].copy()
    extra["date"] = pd.Timestamp("2024-01-15")
    frame = pd.concat([frame, extra], ignore_index=True)
    _rewrite_inventory(tmp_path, "KOSPI", "2024-01", frame)
    with pytest.raises(ValueError, match="inconsistent date coverage"):
        engine.load_market_data_v2(tmp_path, "2024-01-01", "2024-01-31")


def test_loader_rejects_missing_target_date_and_tampered_hash(tmp_path, monkeypatch):
    _fetch(tmp_path, monkeypatch=monkeypatch)
    path = _month_path(tmp_path, "KOSDAQ")
    frame = pd.read_parquet(path).iloc[[0]].reset_index(drop=True)
    _rewrite_inventory(tmp_path, "KOSDAQ", "2024-01", frame)
    with pytest.raises(ValueError, match="missing target dates"):
        engine.load_market_data_v2(tmp_path, "2024-01-01", "2024-01-31")

    manifest_file = _manifest_path(tmp_path)
    manifest = json.loads(manifest_file.read_text())
    manifest["inventory"]["KOSPI"]["2024-01"]["sha256"] = "f" * 64
    manifest_file.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="hash mismatch"):
        engine.load_market_data_v2(tmp_path, "2024-01-01", "2024-01-31")


def test_api_failure_and_replace_failure_preserve_snapshot_and_legacy(
    tmp_path, monkeypatch
):
    _fetch(tmp_path, "2024-01-01", "2024-01-15", monkeypatch=monkeypatch)
    v2_root = tmp_path / "market_data_v2"
    before = {
        path: path.read_bytes()
        for path in v2_root.rglob("*")
        if path.is_file()
    }
    legacy = tmp_path / "market_data" / "legacy-sentinel.bin"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy bytes")

    def fail_api(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(engine.stock, "get_market_cap", fail_api)
    with pytest.raises(RuntimeError):
        _fetch(tmp_path, "2024-01-01", "2024-01-31", monkeypatch=None)
    assert {path: path.read_bytes() for path in v2_root.rglob("*") if path.is_file()} == before
    assert legacy.read_bytes() == b"legacy bytes"

    cap, fundamental = _api_mocks(monkeypatch)
    real_replace = engine.os.replace
    count = 0

    def fail_second(source, destination):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("replacement failed")
        return real_replace(source, destination)

    monkeypatch.setattr(engine.os, "replace", fail_second)
    with pytest.raises(OSError, match="replacement failed"):
        _fetch(tmp_path, "2024-01-01", "2024-01-31", monkeypatch=None)
    assert {path: path.read_bytes() for path in v2_root.rglob("*") if path.is_file()} == before
    assert legacy.read_bytes() == b"legacy bytes"


def test_force_refresh_rebuilds_corrupted_requested_artifact(tmp_path, monkeypatch):
    _fetch(tmp_path, monkeypatch=monkeypatch)
    path = _month_path(tmp_path, "KOSPI")
    corrupted = pd.read_parquet(path)
    corrupted.loc[0, "close"] = 9999
    corrupted.to_parquet(path, index=False)
    cap, fundamental = _api_mocks(monkeypatch)

    refreshed = _fetch(tmp_path, monkeypatch=None, force_refresh=True)
    assert refreshed["close"].max() == 200.0
    expected_calls = Counter(
        {
            ("20240101", "KOSPI"): 1,
            ("20240101", "KOSDAQ"): 1,
            ("20240131", "KOSPI"): 1,
            ("20240131", "KOSDAQ"): 1,
        }
    )
    assert Counter(
        (call.args[0], call.kwargs["market"]) for call in cap.call_args_list
    ) == expected_calls
    assert Counter(
        (call.args[0], call.kwargs["market"])
        for call in fundamental.call_args_list
    ) == expected_calls
    engine.load_market_data_v2(tmp_path, "2024-01-01", "2024-01-31")


def test_force_refresh_preserves_valid_requested_month_outside_dates(
    tmp_path, monkeypatch
):
    _fetch(tmp_path, monkeypatch=monkeypatch)
    path = _month_path(tmp_path, "KOSPI")
    frame = pd.read_parquet(path)
    frame.loc[(frame["date"] == pd.Timestamp("2024-01-01")) & (frame["code"] == "000010"), "close"] = 777.0
    _rewrite_inventory(tmp_path, "KOSPI", "2024-01", frame)
    cap, fundamental = _api_mocks(monkeypatch)

    _fetch(
        tmp_path,
        "2024-01-15",
        "2024-01-31",
        monkeypatch=None,
        force_refresh=True,
    )
    refreshed_file = pd.read_parquet(path)
    assert set(pd.to_datetime(refreshed_file["date"])) == {
        pd.Timestamp("2024-01-01"),
        pd.Timestamp("2024-01-15"),
        pd.Timestamp("2024-01-31"),
    }
    assert refreshed_file.loc[
        (refreshed_file["date"] == pd.Timestamp("2024-01-01"))
        & (refreshed_file["code"] == "000010"),
        "close",
    ].iloc[0] == 777.0
    expected_calls = Counter(
        {
            ("20240115", "KOSPI"): 1,
            ("20240115", "KOSDAQ"): 1,
            ("20240131", "KOSPI"): 1,
            ("20240131", "KOSDAQ"): 1,
        }
    )
    assert Counter(
        (call.args[0], call.kwargs["market"]) for call in cap.call_args_list
    ) == expected_calls
    assert Counter(
        (call.args[0], call.kwargs["market"])
        for call in fundamental.call_args_list
    ) == expected_calls


def test_force_partial_corrupt_month_rebuilds_all_markets_without_asymmetric_extras(
    tmp_path, monkeypatch
):
    _fetch(tmp_path, monkeypatch=monkeypatch)
    kosdaq_path = _month_path(tmp_path, "KOSDAQ")
    kosdaq = pd.read_parquet(kosdaq_path)
    extra = kosdaq.iloc[[0]].copy()
    extra["date"] = pd.Timestamp("2024-01-15")
    _rewrite_inventory(
        tmp_path,
        "KOSDAQ",
        "2024-01",
        pd.concat([kosdaq, extra], ignore_index=True),
    )

    # Leave the KOSPI hash stale: this requested artifact is corrupt, while
    # KOSDAQ remains a valid artifact with an extra out-of-request date.
    kospi_path = _month_path(tmp_path, "KOSPI")
    kospi = pd.read_parquet(kospi_path)
    kospi.loc[0, "close"] = 9999.0
    kospi.to_parquet(kospi_path, index=False)
    cap, fundamental = _api_mocks(monkeypatch)

    _fetch(
        tmp_path,
        "2024-01-15",
        "2024-01-31",
        monkeypatch=None,
        force_refresh=True,
    )
    expected_dates = {pd.Timestamp("2024-01-15"), pd.Timestamp("2024-01-31")}
    for market in ("KOSPI", "KOSDAQ"):
        refreshed = pd.read_parquet(_month_path(tmp_path, market))
        assert set(pd.to_datetime(refreshed["date"])) == expected_dates

    # The complete snapshot, not only the requested range, must remain valid.
    full = engine.load_market_data_v2(tmp_path)
    assert set(pd.to_datetime(full["date"])) == expected_dates
    expected_calls = Counter(
        {
            ("20240115", "KOSPI"): 1,
            ("20240115", "KOSDAQ"): 1,
            ("20240131", "KOSPI"): 1,
            ("20240131", "KOSDAQ"): 1,
        }
    )
    assert Counter(
        (call.args[0], call.kwargs["market"]) for call in cap.call_args_list
    ) == expected_calls
    assert Counter(
        (call.args[0], call.kwargs["market"])
        for call in fundamental.call_args_list
    ) == expected_calls


def test_invalid_core_row_fails_collection_without_changing_cache(
    tmp_path, monkeypatch
):
    _fetch(tmp_path, monkeypatch=monkeypatch)
    v2_root = tmp_path / "market_data_v2"
    before = {
        path: path.read_bytes()
        for path in v2_root.rglob("*")
        if path.is_file()
    }

    def invalid_cap(date, market=None):
        frame = _cap(date, market)
        frame.loc["000020" if market == "KOSPI" else "000120", "close"] = 0.0
        return frame

    monkeypatch.setattr(engine.stock, "get_market_cap", Mock(side_effect=invalid_cap))
    monkeypatch.setattr(
        engine.stock, "get_market_fundamental", Mock(side_effect=_fundamental)
    )
    with pytest.raises(RuntimeError, match="pykrx collection failed"):
        _fetch(
            tmp_path,
            "2024-01-15",
            "2024-01-31",
            monkeypatch=None,
        )
    assert {
        path: path.read_bytes()
        for path in v2_root.rglob("*")
        if path.is_file()
    } == before


def test_legacy_default_path_is_untouched(tmp_path, monkeypatch):
    legacy = tmp_path / "market_data" / "2024-01.parquet"
    legacy.parent.mkdir(parents=True)
    pd.DataFrame({"date": [pd.Timestamp("2024-01-02")], "code": ["000010"]}).to_parquet(
        legacy, index=False
    )
    before = legacy.read_bytes()
    monkeypatch.setattr(engine, "get_korean_business_days", _calendar)
    with patch.object(engine.stock, "get_market_cap") as cap, patch.object(
        engine.stock, "get_market_fundamental"
    ) as fundamental:
        result = engine.fetch_rebalancing_data(
            "2024-01-01", "2024-01-31", cache_dir=tmp_path
        )
    assert not result.empty
    assert legacy.read_bytes() == before
    assert not (tmp_path / "market_data_v2").exists()
    cap.assert_not_called()
    fundamental.assert_not_called()


def test_valid_v2_frame_is_compatible_with_run_backtest(tmp_path, monkeypatch):
    data = _fetch(tmp_path, monkeypatch=monkeypatch)
    config = Config(
        start_date="2024-01-01",
        end_date="2024-01-31",
        initial_capital=100_000,
        buy_cost=0.0,
        sell_cost=0.0,
        slippage=0.0,
        n_stocks=1,
        min_market_cap=1,
        min_trading_val=1,
        use_multi_factor=False,
        use_market_regime=False,
    )
    monkeypatch.setattr(engine, "_fetch_kospi_for_ma", lambda *args: pd.DataFrame())
    history, _metrics = engine.run_backtest(data, config, cache_dir=tmp_path)
    assert not history.empty


def test_valid_v2_cache_next_close_propagates_exact_market_to_engine_event(
    tmp_path, monkeypatch
):
    cap, fundamental = _api_mocks(monkeypatch)
    _fetch(
        tmp_path,
        "2024-01-01",
        "2024-01-31",
        monkeypatch=None,
        execution_mode="next_close",
    )
    cap.reset_mock()
    fundamental.reset_mock()
    loaded = engine.load_market_data_v2(
        tmp_path,
        "2024-01-01",
        "2024-01-31",
        execution_mode="next_close",
    )
    config = Config(
        start_date="2024-01-01",
        end_date="2024-01-31",
        initial_capital=1000,
        buy_cost=0.0,
        sell_cost=0.0,
        slippage=0.0,
        n_stocks=1,
        min_market_cap=1,
        min_trading_val=1,
        use_multi_factor=False,
        use_market_regime=False,
        execution_mode="next_close",
    )
    monkeypatch.setattr(engine, "_fetch_kospi_for_ma", lambda *args: pd.DataFrame())
    stats = {}
    engine.run_backtest(loaded, config, cache_dir=tmp_path, track_stats=stats)

    executions = [
        event
        for event in stats["events"]
        if event["disposition"] == "ORDER_EXECUTED" and event["side"] == "BUY"
    ]
    assert len(executions) == 1
    event = executions[0]
    expected = loaded[
        (loaded["date"] == pd.Timestamp("2024-01-02"))
        & (loaded["code"] == event["code"])
        & (loaded["market"] == event["market"])
    ].iloc[0]
    assert event["market"] in {"KOSPI", "KOSDAQ"}
    assert event["execution_date"] == "2024-01-02"
    assert event["close"] == expected["close"]
    assert event["trading_val"] == expected["trading_val"]
    cap.assert_not_called()
    fundamental.assert_not_called()
