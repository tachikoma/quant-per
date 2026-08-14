import json

import pandas as pd
import pytest

import engine
import phase2_integrity
from config import Config
from phase2_integrity import classify_phase2_stats, main, validate_phase2_stats


def _record():
    return {
        "rebalance_id": "2024-01-02->2024-01-03",
        "signal_date": "2024-01-02",
        "execution_date": "2024-01-03",
        "mode": "next_close",
        "nav_pre": 100.0,
        "nav_post": 100.0,
        "cash_pre": 100.0,
        "cash_post": 0.0,
        "pre_weights": {"CASH": 1.0},
        "target_weights": {"A": 1.0, "CASH": 0.0},
        "post_weights": {"A": 1.0, "CASH": 0.0},
        "hhi_pre": 1.0,
        "hhi_target": 1.0,
        "hhi_post": 1.0,
        "one_way_turnover_krw": 100.0,
        "one_way_turnover_pct": 1.0,
        "l1_target_error": 0.0,
        "telemetry_complete": True,
        "pre_values": {"CASH": 100.0},
        "target_values": {"A": 100.0, "CASH": 0.0},
        "post_values": {"A": 100.0, "CASH": 0.0},
    }


def _event(disposition="ORDER_EXECUTED"):
    return {
        "rebalance_id": "2024-01-02->2024-01-03",
        "signal_date": "2024-01-02",
        "execution_date": "2024-01-03",
        "code": "A",
        "market": "KOSPI",
        "side": "BUY",
        "event_type": "ORDER_EXECUTED",
        "classification": "OBSERVED",
        "disposition": disposition,
        "close": 10.0,
        "trading_val": 100.0,
        "requested_krw": 100.0,
        "order_value": 100.0,
    }


def test_validator_pass_and_empty_stats_fail():
    stats = {"rebalances": [_record()], "events": [_event()]}
    assert validate_phase2_stats(stats) == []
    assert classify_phase2_stats(stats) == "PASS"
    assert "empty_rebalances" in validate_phase2_stats({"rebalances": [], "events": []})


def test_validator_inconclusive_data_limitation():
    stats = {"rebalances": [_record()], "events": [_event("ORDER_SKIPPED")]}
    stats["events"].append(_event("ORDER_EXECUTED"))
    assert validate_phase2_stats(stats) == []
    assert classify_phase2_stats(stats) == "INCONCLUSIVE_DATA_LIMITATION"


def test_validator_detects_unlinked_event_and_zero_execution():
    stats = {"rebalances": [_record()], "events": [_event()]}
    stats["events"][0]["rebalance_id"] = "other"
    assert "unlinked_event" in validate_phase2_stats(stats)
    stats = {"rebalances": [_record()], "events": [_event()]}
    stats["events"][0]["order_value"] = 0
    assert "zero_volume_execution" in validate_phase2_stats(stats)


def test_validator_detects_weight_and_timing_corruption():
    stats = {"rebalances": [_record()], "events": [_event()]}
    stats["rebalances"][0]["post_weights"]["CASH"] = 0.5
    assert "post_weight_sum" in validate_phase2_stats(stats)
    stats = {"rebalances": [_record()], "events": [_event()]}
    stats["rebalances"][0]["execution_date"] = "2024-01-02"
    assert "t1_mismatch" in validate_phase2_stats(stats)


def test_diagnostic_error_writes_fail_summary_without_network(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "phase2_integrity._v2_cache_root", lambda _: tmp_path / "missing"
    )
    monkeypatch.setattr(
        "phase2_integrity.load_market_data_v2",
        lambda **_: (_ for _ in ()).throw(AssertionError("network")),
    )
    main()
    summary = json.loads(
        (tmp_path / "results/phase2_integrity_summary.json").read_text()
    )
    assert summary["status"] == "FAIL_ENGINE_INTEGRITY"


def test_main_success_is_cache_only_and_writes_pass_summary(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "market_data_v2"
    root.mkdir()
    (root / "manifest.json").write_text("{}")

    monkeypatch.setattr(phase2_integrity, "_v2_cache_root", lambda _: root)
    monkeypatch.setattr(
        phase2_integrity,
        "validate_market_data_v2_manifest",
        lambda manifest: manifest,
    )
    monkeypatch.setattr(
        phase2_integrity,
        "load_market_data_v2",
        lambda **_: pd.DataFrame({"date": [pd.Timestamp("2024-01-02")]}),
    )

    def fake_run(data, config, cache_dir=None, track_stats=None, cache_only=False):
        assert not data.empty
        assert config.execution_mode == "next_close"
        assert config.use_market_regime is True
        assert cache_only is True
        track_stats["rebalances"] = [_record()]
        track_stats["events"] = [_event()]
        return pd.DataFrame(), {}

    monkeypatch.setattr(phase2_integrity, "run_backtest", fake_run)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("network access")

    monkeypatch.setattr(engine, "_fetch_kospi_for_ma", forbidden)
    for name in (
        "get_market_cap",
        "get_market_fundamental",
        "get_index_ohlcv_by_date",
    ):
        monkeypatch.setattr(engine.stock, name, forbidden)

    main()
    summary = json.loads(
        (tmp_path / "results/phase2_integrity_summary.json").read_text()
    )
    assert summary["status"] == "PASS"
    assert summary["violations"] == []
    assert summary["event_count"] == 1


def test_main_missing_cached_regime_writes_fail_summary(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "market_data_v2"
    root.mkdir()
    (root / "manifest.json").write_text("{}")
    monkeypatch.setattr(phase2_integrity, "_v2_cache_root", lambda _: root)
    monkeypatch.setattr(
        phase2_integrity,
        "validate_market_data_v2_manifest",
        lambda manifest: manifest,
    )
    monkeypatch.setattr(
        phase2_integrity,
        "load_market_data_v2",
        lambda **_: pd.DataFrame({"date": [pd.Timestamp("2024-01-02")]}),
    )

    def fail_without_regime(*_args, **_kwargs):
        raise ValueError("cache-only KOSPI regime unavailable")

    monkeypatch.setattr(phase2_integrity, "run_backtest", fail_without_regime)
    main()
    summary = json.loads(
        (tmp_path / "results/phase2_integrity_summary.json").read_text()
    )
    assert summary["status"] == "FAIL_ENGINE_INTEGRITY"
    assert any("cache-only KOSPI regime unavailable" in item for item in summary["violations"])


def _actual_horizon_stats(monkeypatch):
    monkeypatch.setattr(
        engine,
        "get_korean_business_days",
        lambda *_: pd.to_datetime(
            ["2020-01-01", "2020-01-02", "2020-01-31", "2020-02-01", "2020-02-03"]
        ),
    )
    rows = []
    for date, close in (
        ("2020-01-01", 10.0),
        ("2020-01-02", 11.0),
        ("2020-01-31", 12.0),
        ("2020-02-01", 13.0),
    ):
        rows.append(
            {
                "date": pd.Timestamp(date),
                "market": "KOSPI",
                "code": "A",
                "close": close,
                "market_cap": 1_000_000_000.0,
                "trading_val": 10_000.0,
                "per": 1.0,
                "pbr": 1.0,
                "div": 1.0,
                "bps": 100.0,
                "eps": 100.0,
                "is_preferred": False,
            }
        )
    stats = {}
    config = Config(
        start_date="2020-01-01",
        end_date="2020-02-01",
        initial_capital=1000,
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
    monkeypatch.setattr(engine, "_fetch_kospi_for_ma", lambda *_: pd.DataFrame())
    engine.run_backtest(pd.DataFrame(rows), config, track_stats=stats)
    return stats


def test_actual_engine_horizon_output_is_inconclusive_not_unlinked_or_fail(
    monkeypatch,
):
    stats = _actual_horizon_stats(monkeypatch)
    horizon = next(
        record for record in stats["rebalances"] if record["execution_date"] == "END_HORIZON"
    )
    assert horizon["pre_values"]["A"] > 0
    assert any(
        event["event_type"] == "REBALANCE_SKIPPED_END_HORIZON"
        and event["rebalance_id"] == horizon["rebalance_id"]
        for event in stats["events"]
    )
    assert "unlinked_event" not in validate_phase2_stats(stats)
    assert classify_phase2_stats(stats) == "INCONCLUSIVE_DATA_LIMITATION"


@pytest.mark.parametrize("field, value", [("target_values", {"A": 90.0, "CASH": 10.0}), ("target_weights", {"A": 0.9, "CASH": 0.1})])
def test_target_value_or_weight_formula_mismatch_is_fail(field, value):
    stats = {"rebalances": [_record()], "events": [_event()]}
    stats["rebalances"][0][field] = value
    assert validate_phase2_stats(stats)
    assert classify_phase2_stats(stats) == "FAIL_ENGINE_INTEGRITY"
