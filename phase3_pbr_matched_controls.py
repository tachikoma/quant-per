"""Run the frozen PBR matched-allocation controls from verified caches only."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from config import Config
from engine import (
    V2_DEFAULT_MARKET_SCOPE,
    _v2_cache_root,
    load_market_data_v2,
    run_backtest,
    validate_market_data_v2_manifest,
)
from phase2_integrity import classify_phase2_stats, validate_phase2_stats


PROJECT_ROOT = Path(__file__).resolve().parent
START_DATE = "2016-01-01"
END_DATE = "2026-06-30"
CACHE_DIR = PROJECT_ROOT / ".cache" / "backtest"
OUTPUT_ROOT = PROJECT_ROOT / "results" / "phase3_pbr_matched"
ALLOCATION_MODES = ("equal_weight", "market_cap_weight")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise ValueError(f"required artifact is unavailable: {path}") from exc
    return digest.hexdigest()


def build_frozen_config(allocation_mode: str) -> Config:
    """Construct the complete, non-environment-derived PBR control config."""
    return Config(
        start_date=START_DATE,
        end_date=END_DATE,
        initial_capital=10_000_000,
        buy_cost=0.00015,
        sell_cost=0.0023,
        slippage=0.002,
        n_stocks=30,
        min_market_cap=20_000_000_000,
        min_trading_val=1_000_000_000,
        max_market_cap=1_000_000_000_000,
        per_pctile=0.2,
        pbr_pctile=0.1,
        roe_pctile=0.5,
        rebalance_freq="quarterly",
        use_multi_factor=True,
        max_turnover=0.5,
        fundamental_lag_months=0,
        use_momentum=False,
        momentum_window=12,
        use_low_volatility=False,
        exclude_negative_per=True,
        use_market_regime=True,
        ma_window=200,
        kospi_ticker="1001",
        dart_api_key="",
        use_katsenelson=False,
        min_roic=0.10,
        max_debt_equity=1.5,
        min_fcf_yield=0.0,
        min_interest_coverage=2.0,
        max_ev_ebitda=20.0,
        earnings_stability_years=5,
        katsenelson_use_growth=False,
        katsenelson_use_ncav=False,
        execution_mode="next_close",
        allocation_mode=allocation_mode,
    )


def assert_clean_git(repo_dir: Path = PROJECT_ROOT) -> str:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        raise RuntimeError("authoritative Phase 3 run requires a clean git worktree")
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not commit:
        raise RuntimeError("unable to identify the authoritative git commit")
    return commit


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON artifact: {path}") from exc


def _read_regime_cache(cache_dir: Path, config: Config) -> dict:
    path = cache_dir / "kospi_ma.parquet"
    if not path.is_file():
        raise ValueError(f"KOSPI regime cache is unavailable: {path}")
    try:
        frame = pd.read_parquet(path)
    except Exception as exc:
        raise ValueError(f"unable to read KOSPI regime cache: {path}") from exc
    if "date" in frame.columns and "kospi_close" in frame.columns:
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        frame = frame.set_index("date")
    else:
        frame.index = pd.to_datetime(frame.index, errors="coerce")
    if frame.index.isna().any() or "kospi_close" not in frame.columns:
        raise ValueError("KOSPI regime cache has an invalid schema")
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    frame["kospi_close"] = pd.to_numeric(frame["kospi_close"], errors="coerce")
    if (
        frame.empty
        or frame["kospi_close"].isna().any()
        or (frame["kospi_close"] <= 0).any()
    ):
        raise ValueError("KOSPI regime cache has invalid close values")
    required_start = pd.Timestamp(config.start_date) - pd.DateOffset(days=420)
    required_end = pd.Timestamp(config.end_date)
    if frame.index.min() > required_start or frame.index.max() < required_end:
        raise ValueError("KOSPI regime cache has incomplete date coverage")
    moving_average = frame["kospi_close"].rolling(
        config.ma_window, min_periods=config.ma_window
    ).mean()
    for date in (pd.Timestamp(config.start_date), required_end):
        available = moving_average[moving_average.index <= date].tail(1)
        if available.empty or pd.isna(available.iloc[0]):
            raise ValueError("KOSPI regime cache has incomplete MA coverage")
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "rows": int(len(frame)),
        "coverage": {
            "min": str(frame.index.min().date()),
            "max": str(frame.index.max().date()),
        },
        "dates": [str(date.date()) for date in frame.index],
    }


def _v2_metadata(root: Path, manifest: dict, data: pd.DataFrame) -> dict:
    inventory = manifest["inventory"]
    return {
        "manifest_sha256": sha256_file(root / "manifest.json"),
        "schema": manifest["schema"],
        "schema_version": manifest["schema_version"],
        "scope": manifest["requested_market_scope"],
        "coverage": inventory,
        "inventory_hashes": {
            market: {
                month: record["sha256"]
                for month, record in months.items()
            }
            for market, months in inventory.items()
        },
        "rows": int(sum(record["row_count"] for months in inventory.values() for record in months.values())),
        "dates": sorted(
            pd.to_datetime(data["date"]).dt.strftime("%Y-%m-%d").unique().tolist()
        ),
        "codes": sorted(data["code"].astype(str).unique().tolist()),
    }


def validate_authoritative_inputs(
    cache_dir: Path = CACHE_DIR, repo_dir: Path = PROJECT_ROOT
) -> tuple[pd.DataFrame, dict, dict, str]:
    """Validate every input before creating any Phase 3 output directory."""
    commit = assert_clean_git(repo_dir)
    root = _v2_cache_root(cache_dir)
    manifest_path = root / "manifest.json"
    manifest = _read_json(manifest_path)
    validate_market_data_v2_manifest(manifest, V2_DEFAULT_MARKET_SCOPE)
    data = load_market_data_v2(
        cache_dir=cache_dir,
        start_date=START_DATE,
        end_date=END_DATE,
        market_scope=V2_DEFAULT_MARKET_SCOPE,
        execution_mode="next_close",
    )
    v2 = _v2_metadata(root, manifest, data)
    regime = _read_regime_cache(cache_dir, build_frozen_config("equal_weight"))
    return data, v2, regime, commit


def _identity(record: dict) -> dict:
    required = (
        "rebalance_id",
        "signal_date",
        "execution_date",
        "target_codes",
        "retained_codes",
        "sell_codes",
        "new_codes",
    )
    if any(field not in record for field in required):
        raise ValueError("rebalance telemetry lacks Phase 3 selector identity")
    return {
        "rebalance_id": record["rebalance_id"],
        "signal_date": record["signal_date"],
        "execution_date": record["execution_date"],
        "target_codes": sorted(map(str, record["target_codes"])),
        "retained_codes": sorted(map(str, record["retained_codes"])),
        "sell_codes": sorted(map(str, record["sell_codes"])),
        "new_codes": sorted(map(str, record["new_codes"])),
    }


def _audit_selector_identity(stats_by_mode: dict[str, dict]) -> list[dict]:
    if any(not stats_by_mode[mode].get("rebalances") for mode in ALLOCATION_MODES):
        raise ValueError("matched controls produced no rebalance telemetry")
    identities = {
        mode: [_identity(record) for record in stats_by_mode[mode]["rebalances"]]
        for mode in ALLOCATION_MODES
    }
    if identities[ALLOCATION_MODES[0]] != identities[ALLOCATION_MODES[1]]:
        raise ValueError("PBR selector or execution identity drifted between controls")
    return [
        {
            "rebalance_id": equal["rebalance_id"],
            "match": True,
            "equal_weight": equal,
            "market_cap_weight": cap,
        }
        for equal, cap in zip(
            identities["equal_weight"], identities["market_cap_weight"]
        )
    ]


def _mode_integrity(stats: dict) -> tuple[str, list[str]]:
    try:
        violations = validate_phase2_stats(stats)
    except Exception as exc:  # pragma: no cover - defensive diagnostic boundary
        violations = [f"validator_error:{exc}"]
    return classify_phase2_stats(stats), violations


def _overall_integrity(integrity: dict[str, dict]) -> str:
    statuses = {entry["integrity_status"] for entry in integrity.values()}
    if "FAIL_ENGINE_INTEGRITY" in statuses:
        return "FAIL_ENGINE_INTEGRITY"
    if "INCONCLUSIVE_DATA_LIMITATION" in statuses:
        return "INCONCLUSIVE_DATA_LIMITATION"
    return "PASS"


def _audit_with_integrity(
    stats_by_mode: dict[str, dict], integrity: dict[str, dict], overall_status: str
) -> list[dict]:
    rows = [
        {
            "record_type": "integrity",
            "overall_status": overall_status,
            "modes": integrity,
        }
    ]
    rows.extend(_audit_selector_identity(stats_by_mode))
    for row in rows[1:]:
        row["overall_status"] = overall_status
        row["integrity"] = integrity
    return rows


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "unavailable"


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    )


def _atomic_publish(staging_dir: Path, final_dir: Path) -> None:
    if final_dir.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {final_dir}")
    staging_dir.rename(final_dir)


def _validate_staged_publication(
    staging_dir: Path, output_files: tuple[str, ...]
) -> dict:
    provenance_path = staging_dir / "provenance.json"
    if not provenance_path.is_file():
        raise ValueError("staged provenance is missing")
    provenance = _read_json(provenance_path)
    for name in output_files:
        path = staging_dir / name
        if not path.is_file():
            raise ValueError(f"staged output is missing: {name}")
        expected = provenance.get("output_hashes", {}).get(name)
        if expected != sha256_file(path):
            raise ValueError(f"staged output hash mismatch: {name}")
    if provenance.get("output_files") != list(output_files):
        raise ValueError("staged output inventory mismatch")
    return provenance


def run_phase3(
    cache_dir: Path = CACHE_DIR,
    output_root: Path = OUTPUT_ROOT,
    repo_dir: Path = PROJECT_ROOT,
) -> Path:
    data, v2, regime, commit = validate_authoritative_inputs(cache_dir, repo_dir)
    configs = {mode: build_frozen_config(mode) for mode in ALLOCATION_MODES}
    input_fingerprint = {
        "commit": commit,
        "v2_manifest_sha256": v2["manifest_sha256"],
        "regime_sha256": regime["sha256"],
        "configs": {mode: asdict(config) for mode, config in configs.items()},
    }
    fingerprint = hashlib.sha256(
        json.dumps(input_fingerprint, sort_keys=True).encode()
    ).hexdigest()[:16]
    output_dir = Path(output_root) / fingerprint
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {output_dir}")

    histories = {}
    metrics = {}
    stats_by_mode = {}
    integrity = {}
    for mode in ALLOCATION_MODES:
        stats = {}
        history, result = run_backtest(
            data,
            configs[mode],
            cache_dir=cache_dir,
            track_stats=stats,
            cache_only=True,
        )
        histories[mode] = history
        metrics[mode] = result
        stats_by_mode[mode] = stats
        status, violations = _mode_integrity(stats)
        integrity[mode] = {
            "integrity_status": status,
            "violations": violations,
        }
    overall_status = _overall_integrity(integrity)
    if overall_status == "FAIL_ENGINE_INTEGRITY":
        raise RuntimeError(
            "Phase 3 publication blocked by engine-integrity failure: "
            + json.dumps(integrity, sort_keys=True)
        )
    audit = _audit_with_integrity(stats_by_mode, integrity, overall_status)

    source_hashes = {
        name: sha256_file(PROJECT_ROOT / name)
        for name in ("engine.py", "config.py", "phase3_pbr_matched_controls.py", "uv.lock")
    }
    output_files = (
        "summary.csv",
        "history_equal_weight.csv",
        "history_market_cap_weight.csv",
        "rebalance_audit.jsonl",
    )
    provenance = {
        "status": overall_status,
        "authoritative": overall_status == "PASS",
        "integrity": {
            "overall_status": overall_status,
            "modes": integrity,
        },
        "fingerprint": fingerprint,
        "git": {"commit": commit, "clean": True},
        "configs": {mode: asdict(config) for mode, config in configs.items()},
        "source_hashes": source_hashes,
        "v2": v2,
        "kospi_regime": regime,
        "runtime": {
            "python": platform.python_version(),
            "pandas": pd.__version__,
            "pykrx": _package_version("pykrx"),
            "timestamp_utc": pd.Timestamp.now(tz="UTC").isoformat(),
            "command": " ".join(sys.argv),
        },
        "cache_only": {
            "requested": True,
            "network_entrypoints_used": False,
            "diagnostic_status": overall_status,
        },
        "output_hashes": {},
        "output_files": list(output_files),
    }
    staging_dir = None
    try:
        Path(output_root).mkdir(parents=True, exist_ok=True)
        staging_dir = Path(
            tempfile.mkdtemp(
                prefix=f".{fingerprint}.staging-", dir=str(Path(output_root))
            )
        )
        summary = pd.DataFrame(
            [
                {
                    "allocation_mode": mode,
                    "overall_integrity_status": overall_status,
                    "integrity_status": integrity[mode]["integrity_status"],
                    "integrity_violations": json.dumps(
                        integrity[mode]["violations"], sort_keys=True
                    ),
                    "integrity_violation_count": len(integrity[mode]["violations"]),
                    **metrics[mode],
                }
                for mode in ALLOCATION_MODES
            ]
        )
        summary.to_csv(staging_dir / "summary.csv", index=False)
        histories["equal_weight"].to_csv(
            staging_dir / "history_equal_weight.csv", index=False
        )
        histories["market_cap_weight"].to_csv(
            staging_dir / "history_market_cap_weight.csv", index=False
        )
        _write_jsonl(staging_dir / "rebalance_audit.jsonl", audit)
        provenance["output_hashes"] = {
            name: sha256_file(staging_dir / name) for name in output_files
        }
        (staging_dir / "provenance.json").write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2, sort_keys=True)
        )
        _validate_staged_publication(staging_dir, output_files)
        _atomic_publish(staging_dir, output_dir)
        staging_dir = None
        return output_dir
    except Exception:
        if staging_dir is not None:
            shutil.rmtree(staging_dir, ignore_errors=True)
        raise


def main() -> None:
    print(run_phase3())


if __name__ == "__main__":
    main()
