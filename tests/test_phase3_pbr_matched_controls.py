import json

import pandas as pd
import pytest

import engine
import phase3_pbr_matched_controls as phase3


def _fake_v2_metadata():
    return {
        "manifest_sha256": "m" * 64,
        "schema": "market_data_v2",
        "schema_version": 2,
        "scope": ["KOSPI", "KOSDAQ"],
        "coverage": {},
        "inventory_hashes": {},
        "rows": 1,
        "dates": ["2016-01-04"],
        "codes": ["A"],
    }


def _fake_regime_metadata():
    return {
        "path": "kospi_ma.parquet",
        "sha256": "r" * 64,
        "rows": 500,
        "coverage": {"min": "2014-11-01", "max": "2026-06-30"},
        "dates": [],
    }


def _fake_stats(drift=False):
    target_codes = ["B"] if drift else ["A", "B"]
    record = {
        "rebalance_id": "2016-01-04->2016-01-05",
        "signal_date": "2016-01-04",
        "execution_date": "2016-01-05",
        "target_codes": target_codes,
        "retained_codes": ["A"],
        "sell_codes": ["C"],
        "new_codes": ["B"],
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
    event = {
        "rebalance_id": record["rebalance_id"],
        "signal_date": record["signal_date"],
        "execution_date": record["execution_date"],
        "code": "A",
        "market": "KOSPI",
        "side": "BUY",
        "event_type": "ORDER_EXECUTED",
        "classification": "OBSERVED_EXECUTABLE",
        "disposition": "ORDER_EXECUTED",
        "close": 10.0,
        "trading_val": 100.0,
        "requested_krw": 100.0,
        "order_value": 100.0,
    }
    return {"rebalances": [record], "events": [event]}


def _fake_run(data, config, cache_dir=None, track_stats=None, cache_only=False):
    assert cache_only is True
    assert config.execution_mode == "next_close"
    assert cache_dir is not None
    assert track_stats is not None
    track_stats.update(_fake_stats())
    history = pd.DataFrame(
        [{"Date": "2016-01-04", "Portfolio_Value": 100, "Stock_Count": 1}]
    )
    return history, {"FINAL_PORTFOLIO_VALUE": 100, "CAGR_PCT": 0.0}


def _install_fake_execution(monkeypatch, stats_factory=_fake_stats):
    monkeypatch.setattr(
        phase3,
        "validate_authoritative_inputs",
        lambda *_args, **_kwargs: (
            pd.DataFrame({"date": [pd.Timestamp("2016-01-04")], "code": ["A"]}),
            _fake_v2_metadata(),
            _fake_regime_metadata(),
            "commit",
        ),
    )

    def fake_run(data, config, cache_dir=None, track_stats=None, cache_only=False):
        assert track_stats is not None
        track_stats.update(stats_factory())
        return pd.DataFrame([{"Date": "2016-01-04", "Portfolio_Value": 100}]), {}

    monkeypatch.setattr(phase3, "run_backtest", fake_run)


def _inconclusive_stats():
    stats = _fake_stats()
    skipped = dict(stats["events"][0])
    skipped.update(
        {
            "event_type": "ZERO_TRADING_VALUE",
            "disposition": "ORDER_SKIPPED",
            "trading_val": 0.0,
            "order_value": 0.0,
        }
    )
    stats["rebalances"][0]["telemetry_complete"] = False
    stats["events"].insert(0, skipped)
    return stats


def _write_synthetic_v2_and_regime(cache_dir):
    root = cache_dir / "market_data_v2"
    root.mkdir(parents=True)
    dates = pd.to_datetime(["2020-01-01", "2020-01-02", "2020-01-31"])
    inventory = {}
    for market, prefix in (("KOSPI", "K"), ("KOSDAQ", "Q")):
        rows = []
        for index in range(20):
            code = f"{prefix}{index:05d}"
            for date in dates:
                rows.append(
                    {
                        "date": date,
                        "market": market,
                        "code": code,
                        "close": 10.0 + index,
                        "market_cap": 100_000_000_000.0 + index,
                        "trading_val": 10_000_000_000.0,
                        "per": 1.0 + index,
                        "pbr": 1.0 + index,
                        "div": 1.0,
                        "bps": 100.0,
                        "eps": 100.0,
                        "is_preferred": False,
                    }
                )
        frame = pd.DataFrame(rows)
        path = root / market / "2020-01.parquet"
        path.parent.mkdir(parents=True)
        frame.to_parquet(path, index=False)
        inventory[market] = {
            "2020-01": {
                "dates": [date.strftime("%Y-%m-%d") for date in dates],
                "row_count": len(frame),
                "sha256": phase3.sha256_file(path),
            }
        }
    manifest = {
        "schema": "market_data_v2",
        "schema_version": 2,
        "source": "pykrx",
        "pykrx_collection_functions": [
            "stock.get_market_cap",
            "stock.get_market_fundamental",
        ],
        "requested_market_scope": ["KOSPI", "KOSDAQ"],
        "price_basis": "synthetic snapshot",
        "known_corporate_action_limitations": "synthetic test data",
        "inventory": inventory,
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    regime = pd.DataFrame(
        {"kospi_close": 100.0},
        index=pd.date_range("2018-01-01", "2020-02-03", freq="B"),
    )
    regime.index.name = "date"
    regime.to_parquet(cache_dir / "kospi_ma.parquet")


def test_frozen_config_is_explicit_and_allocation_validation_is_strict():
    equal = phase3.build_frozen_config("equal_weight")
    cap = phase3.build_frozen_config("market_cap_weight")
    assert equal.allocation_mode == "equal_weight"
    assert cap.allocation_mode == "market_cap_weight"
    assert equal.use_market_regime is True
    assert equal.pbr_pctile == 0.1
    assert equal.rebalance_freq == "quarterly"
    with pytest.raises(ValueError, match="allocation_mode"):
        phase3.build_frozen_config("equal")


def test_cache_validation_uses_only_v2_loader_and_regime_cache(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    root = cache_dir / "market_data_v2"
    root.mkdir(parents=True)
    manifest = {
        "schema": "market_data_v2",
        "schema_version": 2,
        "source": "pykrx",
        "pykrx_collection_functions": [
            "stock.get_market_cap",
            "stock.get_market_fundamental",
        ],
        "requested_market_scope": ["KOSPI", "KOSDAQ"],
        "price_basis": "snapshot",
        "known_corporate_action_limitations": "none",
        "inventory": {},
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    calls = []

    monkeypatch.setattr(phase3, "assert_clean_git", lambda *_: "commit")
    monkeypatch.setattr(phase3, "validate_market_data_v2_manifest", lambda *_: manifest)

    def loader(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame(
            {"date": [pd.Timestamp("2016-01-04")], "code": ["A"]}
        )

    monkeypatch.setattr(phase3, "load_market_data_v2", loader)
    monkeypatch.setattr(phase3, "_read_regime_cache", lambda *_: _fake_regime_metadata())
    data, v2, regime, commit = phase3.validate_authoritative_inputs(cache_dir, tmp_path)
    assert not data.empty
    assert v2["schema"] == "market_data_v2"
    assert regime["sha256"] == "r" * 64
    assert commit == "commit"
    assert calls[0]["execution_mode"] == "next_close"


def test_dirty_guard_fails_before_output_or_loader(tmp_path, monkeypatch):
    output_root = tmp_path / "results"

    def dirty(*_args, **_kwargs):
        raise RuntimeError("dirty")

    monkeypatch.setattr(phase3, "assert_clean_git", dirty)
    monkeypatch.setattr(
        phase3,
        "load_market_data_v2",
        lambda **_: pytest.fail("loader must not run"),
    )
    with pytest.raises(RuntimeError, match="dirty"):
        phase3.run_phase3(
            cache_dir=tmp_path / "cache",
            output_root=output_root,
            repo_dir=tmp_path,
        )
    assert not output_root.exists()


def test_missing_regime_cache_fails_before_output(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    root = cache_dir / "market_data_v2"
    root.mkdir(parents=True)
    manifest = {
        "inventory": {},
        "schema": "market_data_v2",
        "schema_version": 2,
        "requested_market_scope": ["KOSPI", "KOSDAQ"],
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(phase3, "assert_clean_git", lambda *_: "commit")
    monkeypatch.setattr(phase3, "validate_market_data_v2_manifest", lambda *_: manifest)
    monkeypatch.setattr(
        phase3,
        "load_market_data_v2",
        lambda **_: pd.DataFrame(
            {"date": [pd.Timestamp("2016-01-04")], "code": ["A"]}
        ),
    )
    with pytest.raises(ValueError, match="regime cache is unavailable"):
        phase3.validate_authoritative_inputs(cache_dir, tmp_path)


def test_outputs_and_provenance_are_fingerprinted_and_immutable(tmp_path, monkeypatch):
    monkeypatch.setattr(
        phase3,
        "validate_authoritative_inputs",
        lambda *_args, **_kwargs: (
            pd.DataFrame({"date": [pd.Timestamp("2016-01-04")], "code": ["A"]}),
            _fake_v2_metadata(),
            _fake_regime_metadata(),
            "commit",
        ),
    )
    monkeypatch.setattr(phase3, "run_backtest", _fake_run)
    output_root = tmp_path / "results"
    output_dir = phase3.run_phase3(
        cache_dir=tmp_path / "cache", output_root=output_root, repo_dir=tmp_path
    )
    expected = {
        "summary.csv",
        "history_equal_weight.csv",
        "history_market_cap_weight.csv",
        "rebalance_audit.jsonl",
        "provenance.json",
    }
    assert {path.name for path in output_dir.iterdir()} == expected
    provenance = json.loads((output_dir / "provenance.json").read_text())
    assert provenance["git"] == {"clean": True, "commit": "commit"}
    assert set(provenance["configs"]) == {"equal_weight", "market_cap_weight"}
    assert set(provenance["source_hashes"]) == {
        "engine.py",
        "config.py",
        "phase3_pbr_matched_controls.py",
        "uv.lock",
    }
    assert provenance["cache_only"] == {
        "requested": True,
        "network_entrypoints_used": False,
        "diagnostic_status": "PASS",
    }
    assert provenance["status"] == "PASS"
    assert provenance["integrity"]["overall_status"] == "PASS"
    assert all(
        mode["integrity_status"] == "PASS"
        for mode in provenance["integrity"]["modes"].values()
    )
    for name, digest in provenance["output_hashes"].items():
        assert phase3.sha256_file(output_dir / name) == digest
    marker = output_dir / "marker.txt"
    marker.write_text("preserve")
    with pytest.raises(FileExistsError):
        phase3.run_phase3(
            cache_dir=tmp_path / "cache", output_root=output_root, repo_dir=tmp_path
        )
    assert marker.read_text() == "preserve"


def test_audit_fails_on_selector_drift_before_output(tmp_path, monkeypatch):
    monkeypatch.setattr(
        phase3,
        "validate_authoritative_inputs",
        lambda *_args, **_kwargs: (
            pd.DataFrame({"date": [pd.Timestamp("2016-01-04")], "code": ["A"]}),
            _fake_v2_metadata(),
            _fake_regime_metadata(),
            "commit",
        ),
    )

    def drifting_run(data, config, cache_dir=None, track_stats=None, cache_only=False):
        assert track_stats is not None
        track_stats.update(_fake_stats(drift=config.allocation_mode == "market_cap_weight"))
        return pd.DataFrame(), {}

    monkeypatch.setattr(phase3, "run_backtest", drifting_run)
    output_root = tmp_path / "results"
    with pytest.raises(ValueError, match="identity drifted"):
        phase3.run_phase3(
            cache_dir=tmp_path / "cache", output_root=output_root, repo_dir=tmp_path
        )
    assert not output_root.exists()


def test_fail_integrity_mode_leaves_no_publication(tmp_path, monkeypatch):
    def fail_stats():
        return {"rebalances": [], "events": []}

    _install_fake_execution(monkeypatch, fail_stats)
    output_root = tmp_path / "results"
    with pytest.raises(RuntimeError, match="engine-integrity failure"):
        phase3.run_phase3(
            cache_dir=tmp_path / "cache", output_root=output_root, repo_dir=tmp_path
        )
    assert not output_root.exists()


def test_inconclusive_integrity_is_published_without_pass_claim(tmp_path, monkeypatch):
    _install_fake_execution(monkeypatch, _inconclusive_stats)
    output_dir = phase3.run_phase3(
        cache_dir=tmp_path / "cache",
        output_root=tmp_path / "results",
        repo_dir=tmp_path,
    )
    summary = pd.read_csv(output_dir / "summary.csv")
    provenance = json.loads((output_dir / "provenance.json").read_text())
    assert set(summary["integrity_status"]) == {"INCONCLUSIVE_DATA_LIMITATION"}
    assert provenance["status"] == "INCONCLUSIVE_DATA_LIMITATION"
    assert provenance["authoritative"] is False
    assert provenance["cache_only"]["diagnostic_status"] == (
        "INCONCLUSIVE_DATA_LIMITATION"
    )
    audit_lines = [
        json.loads(line)
        for line in (output_dir / "rebalance_audit.jsonl").read_text().splitlines()
    ]
    assert audit_lines[0]["overall_status"] == "INCONCLUSIVE_DATA_LIMITATION"
    assert audit_lines[0]["modes"]["equal_weight"]["violations"] == []


@pytest.mark.parametrize("failure_point", ["write", "rename"])
def test_staging_cleanup_on_publication_failure(tmp_path, monkeypatch, failure_point):
    _install_fake_execution(monkeypatch)
    if failure_point == "write":
        monkeypatch.setattr(
            phase3,
            "_write_jsonl",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("simulated write failure")
            ),
        )
    else:
        monkeypatch.setattr(
            phase3,
            "_atomic_publish",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("simulated rename failure")
            ),
        )
    output_root = tmp_path / "results"
    with pytest.raises(OSError):
        phase3.run_phase3(
            cache_dir=tmp_path / "cache", output_root=output_root, repo_dir=tmp_path
        )
    assert output_root.exists()
    assert list(output_root.iterdir()) == []


def test_real_cache_only_integration_has_no_network_or_legacy_reads(
    tmp_path, monkeypatch
):
    cache_dir = tmp_path / "cache"
    _write_synthetic_v2_and_regime(cache_dir)
    monkeypatch.setattr(phase3, "START_DATE", "2020-01-01")
    monkeypatch.setattr(phase3, "END_DATE", "2020-01-31")
    monkeypatch.setattr(phase3, "assert_clean_git", lambda *_: "synthetic-commit")
    monkeypatch.setattr(
        engine,
        "get_korean_business_days",
        lambda *_args: pd.to_datetime(
            ["2020-01-01", "2020-01-02", "2020-01-31"]
        ),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("network or legacy fetch touched")

    for name in (
        "get_market_cap",
        "get_market_fundamental",
        "get_index_ohlcv_by_date",
    ):
        monkeypatch.setattr(engine.stock, name, forbidden)
    monkeypatch.setattr(engine, "fetch_rebalancing_data", forbidden)
    output_dir = phase3.run_phase3(
        cache_dir=cache_dir,
        output_root=tmp_path / "results",
        repo_dir=tmp_path,
    )
    assert output_dir.is_dir()
    expected = {
        "summary.csv",
        "history_equal_weight.csv",
        "history_market_cap_weight.csv",
        "rebalance_audit.jsonl",
        "provenance.json",
    }
    assert {path.name for path in output_dir.iterdir()} == expected
    summary = pd.read_csv(output_dir / "summary.csv")
    provenance = json.loads((output_dir / "provenance.json").read_text())
    assert set(summary["integrity_status"]) == {"PASS"}
    assert provenance["status"] == "PASS"
    assert provenance["authoritative"] is True
    assert provenance["v2"]["schema"] == "market_data_v2"
    assert provenance["kospi_regime"]["rows"] > 200
    for name, digest in provenance["output_hashes"].items():
        assert phase3.sha256_file(output_dir / name) == digest
