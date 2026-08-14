"""Cache-only Phase 2 execution-integrity diagnostic."""

import json
from pathlib import Path

import pandas as pd

from config import Config
from engine import (
    _v2_cache_root,
    load_market_data_v2,
    run_backtest,
    validate_market_data_v2_manifest,
)


def validate_phase2_stats(stats: dict) -> list[str]:
    violations = []
    rebalances = stats.get("rebalances", [])
    events = stats.get("events", [])
    if not rebalances:
        violations.append("empty_rebalances")
    if not events:
        violations.append("empty_events")
    links = {record.get("rebalance_id") for record in rebalances}
    executed_events = 0
    for event in events:
        if event.get("rebalance_id") not in links:
            violations.append("unlinked_event")
        required = {
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
        if not required.issubset(event):
            violations.append("incomplete_event")
        is_end_horizon = (
            event.get("event_type") == "REBALANCE_SKIPPED_END_HORIZON"
            and event.get("disposition") == "REBALANCE_SKIPPED"
        )
        if not event.get("rebalance_id") or not event.get("signal_date") or not event.get(
            "execution_date"
        ):
            violations.append("incomplete_event_values")
        if event.get("disposition") in {
            "ORDER_EXECUTED",
            "ORDER_SKIPPED",
            "VALUATION_INCOMPLETE",
        } and (not event.get("market") or not event.get("side")):
            violations.append("incomplete_event_values")
        if event.get("disposition") in {
            "ORDER_EXECUTED",
            "ORDER_SKIPPED",
            "VALUATION_INCOMPLETE",
        } and event.get("market") in {None, "", "UNKNOWN"}:
            violations.append("incomplete_event_values")
        if is_end_horizon:
            if event.get("execution_date") != "END_HORIZON":
                violations.append("invalid_end_horizon_event")
            if event.get("code") != "PORTFOLIO":
                violations.append("invalid_end_horizon_event")
            if event.get("market") != "ALL" or event.get("side") != "REBALANCE":
                violations.append("invalid_end_horizon_event")
            if event.get("requested_krw") != 0.0 or event.get("order_value") != 0.0:
                violations.append("invalid_end_horizon_event")
            continue
        if event.get("disposition") == "ORDER_EXECUTED":
            executed_events += 1
            if not event.get("market") or not event.get("side"):
                violations.append("incomplete_event_values")
            if event.get("order_value", 0) <= 0:
                violations.append("zero_volume_execution")
            if any(
                event.get(field) is None
                or not isinstance(event.get(field), (int, float))
                or event.get(field) <= 0
                for field in ("close", "trading_val", "requested_krw")
            ):
                violations.append("incomplete_event_values")
            try:
                if pd.Timestamp(event["execution_date"]) <= pd.Timestamp(
                    event["signal_date"]
                ):
                    violations.append("t1_mismatch")
            except (KeyError, TypeError, ValueError):
                violations.append("incomplete_event_values")
        elif event.get("disposition") == "ORDER_SKIPPED":
            if not event.get("market") or not event.get("side"):
                violations.append("incomplete_skip_values")
            if event.get("event_type") == "ZERO_TRADING_VALUE":
                if event.get("close") is None or event.get("trading_val") != 0:
                    violations.append("incomplete_zero_trade_skip")
            elif event.get("event_type") == "ABSENT_EXECUTION_ROW":
                if event.get("close") is not None or event.get("trading_val") is not None:
                    violations.append("invalid_absent_skip")
            if (
                event.get("requested_krw") is None
                or not isinstance(event.get("requested_krw"), (int, float))
                or event.get("requested_krw") < 0
            ):
                violations.append("incomplete_skip_values")
        elif event.get("disposition") == "VALUATION_INCOMPLETE":
            if event.get("side") != "VALUATION":
                violations.append("incomplete_valuation_event")
            if event.get("close") is None or event.get("close") <= 0:
                violations.append("incomplete_valuation_event")
    for record in stats.get("rebalances", []):
        required = {
            "signal_date",
            "execution_date",
            "rebalance_id",
            "mode",
            "nav_pre",
            "nav_post",
            "cash_pre",
            "cash_post",
            "pre_weights",
            "target_weights",
            "post_weights",
            "hhi_pre",
            "hhi_target",
            "hhi_post",
            "one_way_turnover_krw",
            "one_way_turnover_pct",
            "l1_target_error",
            "telemetry_complete",
            "pre_values",
            "target_values",
            "post_values",
        }
        if not required.issubset(record):
            violations.append("incomplete_rebalance")
            continue
        if record["cash_post"] < 0:
            violations.append("negative_cash")
        is_end_horizon = record.get("execution_date") == "END_HORIZON"
        if is_end_horizon and record.get("mode") != "next_close":
            violations.append("invalid_end_horizon_rebalance")
        if abs(sum(record["target_values"].values()) - record["nav_pre"]) > 1e-8:
            violations.append("target_nav_formula")
        if abs(sum(record["pre_weights"].values()) - 1.0) > 1e-8:
            violations.append("pre_weight_sum")
        if abs(sum(record["target_weights"].values()) - 1.0) > 1e-8:
            violations.append("target_weight_sum")
        if abs(sum(record["post_weights"].values()) - 1.0) > 1e-8:
            violations.append("post_weight_sum")
        for label, values, total, weights in (
            ("pre", record["pre_values"], record["nav_pre"], record["pre_weights"]),
            ("target", record["target_values"], record["nav_pre"], record["target_weights"]),
            ("post", record["post_values"], record["nav_post"], record["post_weights"]),
        ):
            if abs(total - sum(values.values())) > 1e-8:
                violations.append(f"{label}_nav_formula")
            expected = {
                code: value / total for code, value in values.items() if value
            }
            if "CASH" not in expected:
                expected["CASH"] = 0.0
            if any(
                abs(weights.get(code, 0.0) - expected.get(code, 0.0)) > 1e-8
                for code in set(weights) | set(expected)
            ):
                violations.append(f"{label}_weight_formula")
            hhi_key = f"hhi_{label}"
            expected_hhi = sum(weight * weight for weight in expected.values())
            if hhi_key in record and abs(record[hhi_key] - expected_hhi) > 1e-8:
                violations.append(f"{label}_hhi_formula")
        if is_end_horizon:
            horizon_events = [
                event
                for event in events
                if event.get("rebalance_id") == record.get("rebalance_id")
                and event.get("event_type") == "REBALANCE_SKIPPED_END_HORIZON"
                and event.get("disposition") == "REBALANCE_SKIPPED"
            ]
            if len(horizon_events) != 1:
                violations.append("missing_end_horizon_event")
            for left, right in (
                (record["pre_values"], record["target_values"]),
                (record["target_values"], record["post_values"]),
            ):
                if set(left) != set(right) or any(
                    abs(left[code] - right[code]) > 1e-8 for code in left
                ):
                    violations.append("end_horizon_state_change")
        expected_turnover = 0.5 * sum(
            abs(record["post_values"].get(code, 0.0) - record["pre_values"].get(code, 0.0))
            for code in set(record["pre_values"]) | set(record["post_values"])
        )
        if abs(record["one_way_turnover_krw"] - expected_turnover) > 1e-8:
            violations.append("turnover_formula")
        if abs(record["one_way_turnover_pct"] - expected_turnover / record["nav_pre"]) > 1e-8:
            violations.append("turnover_pct_formula")
        expected_l1 = sum(
            abs(record["post_weights"].get(code, 0.0) - record["target_weights"].get(code, 0.0))
            for code in set(record["post_weights"]) | set(record["target_weights"])
        )
        if abs(record["l1_target_error"] - expected_l1) > 1e-8:
            violations.append("l1_formula")
        if not is_end_horizon:
            try:
                if pd.Timestamp(record["execution_date"]) <= pd.Timestamp(
                    record["signal_date"]
                ):
                    violations.append("t1_mismatch")
            except (KeyError, TypeError, ValueError):
                violations.append("incomplete_rebalance")
        if (
            not record.get("telemetry_complete", False)
            and not is_end_horizon
            and not any(
            event.get("rebalance_id") == record.get("rebalance_id")
            and event.get("disposition") in {"ORDER_SKIPPED", "VALUATION_INCOMPLETE"}
            for event in events
            )
        ):
            violations.append("unrecorded_skip")
    if rebalances and executed_events == 0 and not any(
        event.get("disposition") == "REBALANCE_SKIPPED" for event in events
    ):
        violations.append("no_executed_orders")
    return violations


def classify_phase2_stats(stats: dict) -> str:
    try:
        violations = validate_phase2_stats(stats)
    except Exception as exc:
        violations = [f"validator_error:{exc}"]
    if violations:
        return "FAIL_ENGINE_INTEGRITY"
    if any(event.get("disposition") in {"ORDER_SKIPPED", "VALUATION_INCOMPLETE"} for event in stats.get("events", [])):
        return "INCONCLUSIVE_DATA_LIMITATION"
    if any(
        event.get("disposition") == "REBALANCE_SKIPPED"
        and event.get("event_type") == "REBALANCE_SKIPPED_END_HORIZON"
        for event in stats.get("events", [])
    ):
        return "INCONCLUSIVE_DATA_LIMITATION"
    return "PASS"


def main():
    config = Config(
        start_date="2016-01-01",
        end_date="2026-06-30",
        initial_capital=10_000_000,
        buy_cost=0.00015,
        sell_cost=0.0023,
        slippage=0.002,
        n_stocks=30,
        min_market_cap=20_000_000_000,
        min_trading_val=1_000_000_000,
        max_turnover=0.5,
        rebalance_freq="quarterly",
        use_multi_factor=True,
        use_katsenelson=False,
        pbr_pctile=0.1,
        per_pctile=0.2,
        roe_pctile=0.5,
        max_market_cap=1_000_000_000_000,
        fundamental_lag_months=0,
        use_momentum=False,
        use_low_volatility=False,
        exclude_negative_per=True,
        use_market_regime=True,
        execution_mode="next_close",
    )
    cache_dir = Path(".cache/backtest")
    root = _v2_cache_root(cache_dir)
    stats = {"rebalances": [], "events": []}
    error = None
    try:
        manifest = json.loads((root / "manifest.json").read_text())
        validate_market_data_v2_manifest(manifest)
        data = load_market_data_v2(
            cache_dir=cache_dir,
            start_date=config.start_date,
            end_date=config.end_date,
            execution_mode="next_close",
        )
        _history, _metrics = run_backtest(
            data, config, cache_dir=cache_dir, track_stats=stats, cache_only=True
        )
    except Exception as exc:
        error = str(exc)
    try:
        violations = validate_phase2_stats(stats)
    except Exception as exc:
        violations = [f"validator_error:{exc}"]
        error = error or str(exc)
    if error:
        violations.append(f"diagnostic_error:{error}")
    events = stats.get("events", [])
    status = "FAIL_ENGINE_INTEGRITY" if error else classify_phase2_stats(stats)
    Path("results").mkdir(exist_ok=True)
    pd.DataFrame(stats.get("rebalances", [])).to_csv(
        "results/phase2_integrity_rebalances.csv", index=False
    )
    pd.DataFrame(events).to_csv("results/phase2_integrity_events.csv", index=False)
    Path("results/phase2_integrity_summary.json").write_text(
        json.dumps(
            {"status": status, "violations": violations, "event_count": len(events)},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
