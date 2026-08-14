import json
import hashlib
import math
import os
import shutil
import tempfile
import time
from pathlib import Path
import pandas as pd
from pykrx import stock
from tqdm import tqdm
from config import get_korean_business_days, Config
from dart_data import merge_dart_financials
from metrics import build_financial_metrics


pd.set_option("future.no_silent_downcasting", True)

DEFAULT_CACHE_DIR = Path(".cache") / "backtest"
V2_SCHEMA_VERSION = 2
V2_SCHEMA_NAME = "market_data_v2"
V2_DEFAULT_MARKET_SCOPE = ("KOSPI", "KOSDAQ")
V2_SUPPORTED_MARKETS = frozenset(V2_DEFAULT_MARKET_SCOPE)
V2_COLUMNS = (
    "date",
    "market",
    "code",
    "close",
    "market_cap",
    "trading_val",
    "per",
    "pbr",
    "div",
    "bps",
    "eps",
    "is_preferred",
)
V2_REQUIRED_COLUMNS = frozenset(V2_COLUMNS)
V2_CORE_NUMERIC_COLUMNS = ("close", "market_cap", "trading_val")
V2_FUNDAMENTAL_NUMERIC_COLUMNS = ("per", "pbr", "div", "bps", "eps")
V2_NUMERIC_COLUMNS = V2_CORE_NUMERIC_COLUMNS + V2_FUNDAMENTAL_NUMERIC_COLUMNS
V2_COLLECTION_FUNCTIONS = (
    "stock.get_market_cap",
    "stock.get_market_fundamental",
)
V2_PRICE_BASIS = "pykrx get_market_cap 종가 스냅샷 (기업행동 조정 이벤트 원장 아님)"
V2_CORPORATE_ACTION_LIMITATION = (
    "pykrx 스냅샷에는 기업행동 이벤트 원장이 없습니다. DIV/DPS만으로 현금배당 "
    "시점이나 조정가격을 추정하지 않습니다."
)


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


def _normalise_market_scope(market_scope) -> tuple[str, ...]:
    """Validate and normalize the explicitly requested v2 market scope."""
    if market_scope is None:
        market_scope = V2_DEFAULT_MARKET_SCOPE
    if isinstance(market_scope, str):
        market_scope = (market_scope,)
    try:
        normalized = tuple(str(market).upper() for market in market_scope)
    except TypeError as exc:
        raise ValueError("market_scope must be an iterable of market names") from exc

    if not normalized:
        raise ValueError("market_scope must contain at least one market")
    if len(set(normalized)) != len(normalized):
        raise ValueError("market_scope must not contain duplicate markets")
    unsupported = set(normalized) - V2_SUPPORTED_MARKETS
    if unsupported:
        raise ValueError(f"지원하지 않는 v2 market: {sorted(unsupported)}")
    return normalized


def _v2_today() -> pd.Timestamp:
    """Return the current KST calendar date as a naive midnight timestamp."""
    return pd.Timestamp.now(tz="Asia/Seoul").normalize().tz_localize(None)


def _v2_date_only_series(values, label="date") -> pd.Series:
    """Parse date-only values without normalizing away invalid time/zone data."""
    parsed = []
    for value in values:
        if value is None or pd.isna(value):
            parsed.append(pd.NaT)
            continue
        try:
            timestamp = pd.Timestamp(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"v2 {label} contains invalid dates") from exc
        if pd.isna(timestamp):
            parsed.append(pd.NaT)
            continue
        if timestamp.tz is not None:
            raise ValueError(f"v2 {label} must be timezone-naive")
        if timestamp != timestamp.normalize():
            raise ValueError(f"v2 {label} must be midnight date-only values")
        parsed.append(timestamp)
    return pd.Series(parsed, index=values.index, dtype="datetime64[ns]")


def _parse_v2_dates(start_date, end_date, lag_months=0):
    if (start_date is None) != (end_date is None):
        raise ValueError("start_date and end_date must be supplied together")
    if start_date is None:
        return None, None
    start_ts = _v2_date_only_series(pd.Series([start_date]), "start_date").iloc[0]
    end_ts = _v2_date_only_series(pd.Series([end_date]), "end_date").iloc[0]
    if pd.isna(start_ts) or pd.isna(end_ts):
        raise ValueError("start_date and end_date must be valid dates")
    if start_ts > end_ts:
        raise ValueError("start_date must not be after end_date")
    if lag_months < 0:
        raise ValueError("lag_months must not be negative")
    fetch_start = start_ts - pd.DateOffset(months=lag_months)
    return fetch_start, end_ts


def _v2_target_dates(
    start_date, end_date, lag_months=0, execution_mode="same_close"
) -> tuple[pd.Timestamp, ...]:
    """Return first/last available business dates for the v2 request.

    This is the single coverage rule used by both the writer and the loader.
    The current day is excluded because pykrx's end-of-day snapshot can be
    incomplete while a trading session is in progress.
    """
    plan = _v2_signal_execution_plan(
        start_date, end_date, lag_months, execution_mode
    )
    dates = set()
    for item in plan.values():
        dates.update((item["signal_date"], item["last_date"]))
        if item["execution_date"] is not None:
            dates.add(item["execution_date"])
    return tuple(sorted(dates))


def _v2_signal_execution_plan(
    start_date, end_date, lag_months=0, execution_mode="same_close"
) -> dict:
    """Return the one canonical Korean-calendar signal/T+1 plan.

    The planner deliberately uses the calendar's immediate successor, not the
    next row found in a parquet.  A successor beyond the evaluation horizon is
    represented as ``None`` so callers can apply the same no-trade policy.
    """
    if execution_mode not in ("same_close", "next_close"):
        raise ValueError("execution_mode must be exactly 'same_close' or 'next_close'")
    fetch_start, fetch_end = _parse_v2_dates(start_date, end_date, lag_months)
    if fetch_start is None:
        return {}
    calendar_end = fetch_end + pd.Timedelta(days=10)
    business_days = get_korean_business_days(
        fetch_start.strftime("%Y-%m-%d"), calendar_end.strftime("%Y-%m-%d")
    )
    dates = pd.to_datetime(pd.Series(business_days), errors="coerce").dt.normalize()
    dates = sorted(
        date for date in dates.dropna().drop_duplicates().tolist() if date < _v2_today()
    )
    signal_dates = [date for date in dates if date <= fetch_end]
    if not signal_dates:
        return {}
    by_month = {}
    for date in signal_dates:
        by_month.setdefault(date.to_period("M"), []).append(date)
    plan = {}
    for month, month_dates in by_month.items():
        signal_date = min(month_dates)
        last_date = max(month_dates)
        execution_date = signal_date
        if execution_mode == "next_close":
            exact_successors = [date for date in dates if date > signal_date]
            exact_successor = exact_successors[0] if exact_successors else None
            execution_date = (
                exact_successor
                if exact_successor is not None and exact_successor <= fetch_end
                else None
            )
        plan[month] = {
            "signal_date": signal_date,
            "last_date": last_date,
            "execution_date": execution_date,
        }
    return plan


def _v2_months(target_dates) -> tuple[str, ...]:
    return tuple(
        sorted({str(pd.Timestamp(date).to_period("M")) for date in target_dates})
    )


def validate_market_data_v2_manifest(
    manifest: dict, market_scope=None
) -> dict:
    """Validate v2 provenance and manifest inventory metadata."""
    if not isinstance(manifest, dict):
        raise ValueError("v2 manifest must be a JSON object")

    required_keys = {
        "schema",
        "schema_version",
        "source",
        "pykrx_collection_functions",
        "requested_market_scope",
        "price_basis",
        "known_corporate_action_limitations",
        "inventory",
    }
    missing = required_keys - set(manifest)
    if missing:
        raise ValueError(f"v2 manifest missing required keys: {sorted(missing)}")
    if manifest["schema"] != V2_SCHEMA_NAME:
        raise ValueError(f"unexpected v2 schema name: {manifest['schema']!r}")
    if manifest["schema_version"] != V2_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported v2 schema_version: {manifest['schema_version']!r}"
        )
    if manifest["source"] != "pykrx":
        raise ValueError(f"unexpected v2 data source: {manifest['source']!r}")

    functions = manifest["pykrx_collection_functions"]
    try:
        functions_valid = (
            isinstance(functions, (list, tuple))
            and len(functions) == len(set(functions))
            and set(functions) == set(V2_COLLECTION_FUNCTIONS)
        )
    except TypeError:
        functions_valid = False
    if not functions_valid:
        raise ValueError("v2 manifest collection functions are invalid")

    manifest_scope = _normalise_market_scope(manifest["requested_market_scope"])
    requested_scope = (
        _normalise_market_scope(market_scope)
        if market_scope is not None
        else manifest_scope
    )
    if manifest_scope != requested_scope:
        raise ValueError(
            "v2 manifest market scope mismatch: "
            f"manifest={manifest_scope}, requested={requested_scope}"
        )
    for key in ("price_basis", "known_corporate_action_limitations"):
        if not isinstance(manifest[key], str) or not manifest[key].strip():
            raise ValueError(f"v2 manifest field must be a non-empty string: {key}")

    inventory = manifest["inventory"]
    if not isinstance(inventory, dict):
        raise ValueError("v2 manifest inventory must be an object")
    inventory_markets = set(inventory)
    if inventory_markets != set(manifest_scope):
        raise ValueError(
            "v2 manifest inventory market mismatch: "
            f"expected={sorted(manifest_scope)}, actual={sorted(inventory_markets)}"
        )
    for market in manifest_scope:
        months = inventory[market]
        if not isinstance(months, dict):
            raise ValueError(f"v2 inventory for {market} must be an object")
        for month, record in months.items():
            try:
                period = pd.Period(month, freq="M")
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid v2 inventory month: {month!r}") from exc
            if str(period) != month:
                raise ValueError(f"invalid v2 inventory month: {month!r}")
            if not isinstance(record, dict):
                raise ValueError(f"invalid v2 inventory record: {market}/{month}")
            if set(record) != {"dates", "row_count", "sha256"}:
                raise ValueError(f"invalid v2 inventory keys: {market}/{month}")
            dates = record["dates"]
            if not isinstance(dates, list) or not dates:
                raise ValueError(f"v2 inventory has no dates: {market}/{month}")
            try:
                parsed = [pd.Timestamp(date).normalize() for date in dates]
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid v2 inventory dates: {market}/{month}") from exc
            if dates != [date.strftime("%Y-%m-%d") for date in sorted(set(parsed))]:
                raise ValueError(f"v2 inventory dates are not canonical: {market}/{month}")
            if any(date.to_period("M") != period for date in parsed):
                raise ValueError(f"v2 inventory date/month mismatch: {market}/{month}")
            if (
                not isinstance(record["row_count"], int)
                or isinstance(record["row_count"], bool)
                or record["row_count"] <= 0
            ):
                raise ValueError(f"invalid v2 inventory row_count: {market}/{month}")
            digest = record["sha256"]
            if not isinstance(digest, str) or len(digest) != 64:
                raise ValueError(f"invalid v2 inventory sha256: {market}/{month}")
            try:
                int(digest, 16)
            except ValueError as exc:
                raise ValueError(f"invalid v2 inventory sha256: {market}/{month}") from exc

    return manifest


def _validate_v2_frame(
    df: pd.DataFrame, required_markets=None, expected_month=None
) -> None:
    """Validate the strict twelve-column v2 parquet schema."""
    if not isinstance(df, pd.DataFrame):
        raise ValueError("v2 market data must be a DataFrame")
    missing = set(V2_COLUMNS) - set(df.columns)
    extra = set(df.columns) - set(V2_COLUMNS)
    if missing:
        raise ValueError(f"v2 market data missing required columns: {sorted(missing)}")
    if extra:
        raise ValueError(f"v2 market data has unexpected columns: {sorted(extra)}")
    if df.empty:
        raise ValueError("v2 market data must not be empty")

    parsed_dates = _v2_date_only_series(df["date"])
    if parsed_dates.isna().any():
        raise ValueError("v2 market data contains invalid dates")
    if expected_month is not None and not (
        parsed_dates.dt.to_period("M") == expected_month
    ).all():
        raise ValueError(f"v2 market data row/month mismatch: {expected_month}")
    if df["market"].isna().any() or df["code"].isna().any():
        raise ValueError("v2 market data contains null market/code values")
    if not df["market"].isin(V2_SUPPORTED_MARKETS).all():
        raise ValueError("v2 market data contains an unsupported market")
    codes = df["code"].astype(str).str.strip()
    if codes.eq("").any() or codes.str.lower().eq("nan").any():
        raise ValueError("v2 market data contains an empty ticker")
    if not pd.api.types.is_bool_dtype(df["is_preferred"]):
        raise ValueError("v2 is_preferred must have boolean dtype")
    if df["is_preferred"].isna().any():
        raise ValueError("v2 is_preferred must not be null")
    for column in V2_NUMERIC_COLUMNS:
        if pd.api.types.is_bool_dtype(df[column]) or not pd.api.types.is_numeric_dtype(
            df[column]
        ):
            raise ValueError(f"v2 {column} must have numeric dtype")
    for column in V2_CORE_NUMERIC_COLUMNS:
        values = pd.to_numeric(df[column], errors="coerce")
        if values.isna().any() or not values.map(pd.api.types.is_number).all():
            raise ValueError(f"v2 {column} contains missing/non-numeric values")
    for column in ("close", "market_cap"):
        if (df[column] <= 0).any():
            raise ValueError(f"v2 {column} must be positive")
    if (df["trading_val"] < 0).any():
        raise ValueError("v2 trading_val must be non-negative")
    for column in V2_NUMERIC_COLUMNS:
        values = pd.to_numeric(df[column], errors="coerce")
        if values.isin([float("inf"), float("-inf")]).any():
            raise ValueError(f"v2 {column} contains infinite values")

    actual_markets = set(df["market"])
    if required_markets is not None:
        expected_markets = set(_normalise_market_scope(required_markets))
        if actual_markets != expected_markets:
            raise ValueError(
                "v2 parquet market mismatch: "
                f"expected={sorted(expected_markets)}, actual={sorted(actual_markets)}"
            )

    keys = pd.DataFrame(
        {
            "date": parsed_dates.dt.normalize(),
            "market": df["market"].astype(str),
            "code": codes,
        }
    )
    if keys.duplicated(subset=["date", "market", "code"]).any():
        raise ValueError("v2 market data contains duplicate (date, market, code) rows")


def validate_market_data_v2(
    df: pd.DataFrame,
    manifest: dict,
    market_scope=None,
    start_date=None,
    end_date=None,
) -> pd.DataFrame:
    """Validate a complete v2 snapshot and return it unchanged."""
    normalized_manifest = validate_market_data_v2_manifest(manifest, market_scope)
    expected_markets = _normalise_market_scope(
        normalized_manifest["requested_market_scope"]
    )
    _validate_v2_frame(df, required_markets=expected_markets)
    keys = pd.DataFrame(
        {
            "date": _v2_date_only_series(df["date"]),
            "market": df["market"].astype(str),
            "code": df["code"].astype(str).str.strip(),
        }
    )
    if start_date is not None or end_date is not None:
        range_start, range_end = _parse_v2_dates(start_date, end_date)
        keys = keys[(keys["date"] >= range_start) & (keys["date"] <= range_end)]
        if keys.empty:
            raise ValueError("v2 validation range contains no rows")
    if keys.duplicated(subset=["date", "code"]).any():
        raise ValueError("v2 data contains duplicate (date, code) across markets")
    date_sets = [
        set(keys.loc[keys["market"] == market, "date"])
        for market in expected_markets
    ]
    if date_sets and any(date_set != date_sets[0] for date_set in date_sets[1:]):
        raise ValueError("v2 markets have inconsistent date coverage")
    return df


def _v2_cache_root(cache_dir=None) -> Path:
    cache_base = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    if cache_base.name == "market_data_v2":
        return cache_base
    return cache_base / "market_data_v2"


def _read_v2_manifest(v2_root: Path, market_scope=None) -> dict:
    manifest_file = v2_root / "manifest.json"
    if not manifest_file.exists():
        raise ValueError(f"v2 manifest not found: {manifest_file}")
    try:
        manifest = json.loads(manifest_file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid v2 manifest: {manifest_file}") from exc
    return validate_market_data_v2_manifest(manifest, market_scope)


def _v2_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_v2_parquet(path: Path, market: str, month: str) -> pd.DataFrame:
    try:
        part = pd.read_parquet(path)
    except Exception as exc:
        raise ValueError(f"unable to read v2 parquet: {path}") from exc
    _validate_v2_frame(part, required_markets=(market,), expected_month=month)
    return part


def _v2_inventory_record(path: Path, part: pd.DataFrame) -> dict:
    dates = sorted(
        _v2_date_only_series(part["date"])
        .dt.strftime("%Y-%m-%d")
        .unique()
    )
    return {
        "dates": dates,
        "row_count": int(len(part)),
        "sha256": _v2_sha256(path),
    }


def _verify_v2_inventory(
    v2_root: Path, manifest: dict
) -> dict[tuple[str, str], pd.DataFrame]:
    """Verify every manifest-listed artifact and return the trusted frames."""
    validate_market_data_v2_manifest(manifest)
    inventory = manifest["inventory"]
    parts = {}
    for market, months in inventory.items():
        market_dir = v2_root / market
        for month, expected in months.items():
            path = market_dir / f"{month}.parquet"
            if not path.is_file():
                raise ValueError(f"v2 inventory artifact is missing: {path}")
            if _v2_sha256(path) != expected["sha256"]:
                raise ValueError(f"v2 inventory hash mismatch: {path}")
            part = _read_v2_parquet(path, market, month)
            actual = _v2_inventory_record(path, part)
            if actual["dates"] != expected["dates"]:
                raise ValueError(f"v2 inventory date coverage mismatch: {path}")
            if actual["row_count"] != expected["row_count"]:
                raise ValueError(f"v2 inventory row count mismatch: {path}")
            parts[(market, month)] = part

        if market_dir.exists():
            listed = {f"{month}.parquet" for month in months}
            extras = {
                path.name
                for path in market_dir.glob("*.parquet")
                if path.name not in listed
            }
            if extras:
                raise ValueError(
                    f"v2 parquet files are not listed in the manifest: {market}: "
                    f"{sorted(extras)}"
                )
    return parts


def _try_read_verified_v2_artifact(
    v2_root: Path, market: str, month: str, expected: dict
) -> pd.DataFrame | None:
    """Return a trusted artifact, or None without using a failed artifact."""
    path = v2_root / market / f"{month}.parquet"
    if not path.is_file() or _v2_sha256(path) != expected["sha256"]:
        return None
    try:
        part = _read_v2_parquet(path, market, month)
    except ValueError:
        return None
    actual = _v2_inventory_record(path, part)
    if actual["dates"] != expected["dates"] or actual["row_count"] != expected["row_count"]:
        return None
    return part


def _v2_expected_by_month(target_dates) -> dict[str, set[pd.Timestamp]]:
    expected = {}
    for date in target_dates:
        month = str(pd.Timestamp(date).to_period("M"))
        expected.setdefault(month, set()).add(pd.Timestamp(date).normalize())
    return expected


def load_market_data_v2(
    cache_dir=None,
    start_date=None,
    end_date=None,
    market_scope=V2_DEFAULT_MARKET_SCOPE,
    lag_months=0,
    execution_mode="same_close",
) -> pd.DataFrame:
    """Load and validate versioned market data without consulting pykrx."""
    expected_scope = _normalise_market_scope(market_scope)
    v2_root = _v2_cache_root(cache_dir)
    manifest = _read_v2_manifest(v2_root, expected_scope)
    trusted_parts = _verify_v2_inventory(v2_root, manifest)

    requested_start = (
        None if start_date is None else pd.Timestamp(start_date).normalize()
    )
    start_ts, end_ts = _parse_v2_dates(start_date, end_date, lag_months)
    if start_ts is None:
        if not trusted_parts:
            raise ValueError("v2 data has no parquet files")
        coverage = {
            market: {
                month: tuple(record["dates"])
                for month, record in manifest["inventory"][market].items()
            }
            for market in expected_scope
        }
        if any(coverage[market] != coverage[expected_scope[0]] for market in expected_scope):
            raise ValueError("v2 markets have inconsistent date coverage")
        requested_keys = sorted(trusted_parts)
        expected_by_month = None
    else:
        target_dates = _v2_target_dates(
            requested_start, end_ts, lag_months, execution_mode=execution_mode
        )
        if not target_dates:
            raise ValueError("v2 request has no available target dates")
        requested_months = _v2_months(target_dates)
        expected_by_month = _v2_expected_by_month(target_dates)
        requested_keys = []
        for market in expected_scope:
            for month in requested_months:
                key = (market, month)
                if key not in trusted_parts:
                    raise ValueError(f"v2 data missing required market/month: {key}")
                actual_dates = set(_v2_date_only_series(trusted_parts[key]["date"]))
                missing_dates = expected_by_month[month] - actual_dates
                if missing_dates:
                    raise ValueError(
                        f"v2 data missing target dates for {market}/{month}: "
                        f"{sorted(missing_dates)}"
                    )
                requested_keys.append(key)

    all_parts = [trusted_parts[key] for key in requested_keys]
    result = pd.concat(all_parts, ignore_index=True)
    if start_ts is not None:
        result = result.copy()
        result["date"] = _v2_date_only_series(result["date"])
        result = result[
            (result["date"] >= start_ts) & (result["date"] <= end_ts)
        ].reset_index(drop=True)
        validate_market_data_v2(
            result,
            manifest,
            expected_scope,
        )
    else:
        validate_market_data_v2(result, manifest, expected_scope)
    return result


def fetch_rebalancing_data(
    start_date,
    end_date,
    cache_dir=None,
    force_refresh=False,
    lag_months=0,
    use_market_data_v2=False,
    market_scope=V2_DEFAULT_MARKET_SCOPE,
    execution_mode="same_close",
):
    if execution_mode not in ("same_close", "next_close"):
        raise ValueError("execution_mode must be exactly 'same_close' or 'next_close'")
    if execution_mode == "next_close" and not use_market_data_v2:
        raise ValueError("next_close requires use_market_data_v2=True")
    if use_market_data_v2:
        return _fetch_rebalancing_data_v2(
            start_date,
            end_date,
            cache_dir=cache_dir,
            force_refresh=force_refresh,
            lag_months=lag_months,
            market_scope=market_scope,
            execution_mode=execution_mode,
        )

    if lag_months > 0:
        fetch_start = (
            pd.Timestamp(start_date) - pd.DateOffset(months=lag_months)
        ).strftime("%Y-%m-%d")
    else:
        fetch_start = start_date
    today = pd.Timestamp.now().normalize()
    display_end = min(pd.Timestamp(end_date), today).strftime("%Y-%m-%d")
    print(
        f"[{fetch_start} ~ {display_end}] 영업일 캘린더 분석 중... (백테스트: {start_date} ~ {display_end})"
    )
    b_days = get_korean_business_days(fetch_start, end_date)
    df_days = pd.DataFrame(b_days, columns=["date"])
    df_days["year_month"] = df_days["date"].dt.to_period("M")

    first_days = df_days.groupby("year_month").first()["date"].tolist()
    last_days = df_days.groupby("year_month").last()["date"].tolist()
    target_dates = sorted(list(set(first_days + last_days)))

    today = pd.Timestamp.now().normalize()
    target_dates = [d for d in target_dates if d < today]

    if not target_dates:
        print("  데이터를 조회할 수 있는 과거 영업일이 없습니다.")
        return pd.DataFrame()

    df_target = pd.DataFrame(target_dates, columns=["date"])
    df_target["year_month"] = df_target["date"].dt.to_period("M")
    year_months = sorted(df_target["year_month"].unique())

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
            d
            for d in target_dates
            if str(pd.Timestamp(d).to_period("M")) in fetch_ym_set
        ]

        print(
            f"총 {len(fetch_dates)}개 타겟 영업일 데이터를 수집합니다 (신규/갱신 월: {len(months_to_fetch)}개월)"
        )
        for dt in tqdm(fetch_dates, desc="KRX 데이터 다운로드"):
            dt_str = dt.strftime("%Y%m%d")
            df_mcap = stock.get_market_cap(dt_str)
            df_fund = stock.get_market_fundamental(dt_str)

            if df_mcap.empty or df_fund.empty:
                continue
            if (df_mcap["종가"] == 0).all():
                continue

            df_merged = pd.concat([df_mcap, df_fund], axis=1)
            df_merged = df_merged.loc[:, ~df_merged.columns.duplicated()]
            df_merged = df_merged.reset_index()
            df_merged = df_merged.rename(
                columns={
                    "티커": "code",
                    "종가": "close",
                    "시가총액": "market_cap",
                    "거래대금": "trading_val",
                    "PER": "per",
                    "PBR": "pbr",
                    "DIV": "div",
                    "BPS": "bps",
                    "EPS": "eps",
                }
            )
            df_merged["date"] = dt
            df_merged["code"] = df_merged["code"].astype(str)
            df_merged["is_preferred"] = ~df_merged["code"].str.endswith("0")

            cols = [
                "date",
                "code",
                "close",
                "market_cap",
                "trading_val",
                "per",
                "pbr",
                "div",
                "bps",
                "eps",
                "is_preferred",
            ]
            df_merged = df_merged[cols]

            ym_str = str(pd.Timestamp(dt).to_period("M"))
            month_file = market_cache_dir / f"{ym_str}.parquet"

            if month_file.exists():
                existing = pd.read_parquet(month_file)
                existing = existing[existing["date"] != dt]
                df_merged = pd.concat([existing, df_merged], ignore_index=True)

            df_merged.to_parquet(month_file, index=False)
            cache_index.setdefault("market_data", {}).setdefault("months", {})[
                ym_str
            ] = {"cached_at": pd.Timestamp.now().isoformat()}

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


def _merge_v2_market_snapshot(
    df_mcap: pd.DataFrame, df_fund: pd.DataFrame, dt, market: str
) -> pd.DataFrame:
    """Merge one date's two pykrx responses within one market only."""
    market = _normalise_market_scope((market,))[0]
    if not isinstance(df_mcap, pd.DataFrame) or not isinstance(df_fund, pd.DataFrame):
        raise ValueError(f"v2 pykrx responses must be DataFrames: {market} {dt}")
    if df_mcap.empty or df_fund.empty:
        raise ValueError(f"v2 empty pykrx response: {market} {dt}")

    def with_code(frame: pd.DataFrame, label: str) -> pd.DataFrame:
        result = frame.copy()
        if "티커" in result.columns:
            result = result.rename(columns={"티커": "code"})
        elif "code" not in result.columns:
            result = result.reset_index()
            for ticker_column in ("티커", "종목코드", "index"):
                if ticker_column in result.columns:
                    result = result.rename(columns={ticker_column: "code"})
                    break
        if "code" not in result.columns:
            raise ValueError(f"v2 {label} response has no ticker index: {market} {dt}")
        result["code"] = result["code"].astype(str).str.strip()
        if result["code"].eq("").any() or result["code"].str.lower().eq("nan").any():
            raise ValueError(f"v2 {label} response has an empty ticker: {market} {dt}")
        if result["code"].duplicated().any():
            raise ValueError(f"v2 {label} response has duplicate tickers: {market} {dt}")
        return result

    cap = with_code(df_mcap, "market-cap")
    fundamental = with_code(df_fund, "fundamental")
    cap = cap.rename(
        columns={"종가": "close", "시가총액": "market_cap", "거래대금": "trading_val"}
    )
    fundamental = fundamental.rename(
        columns={
            "PER": "per",
            "PBR": "pbr",
            "DIV": "div",
            "BPS": "bps",
            "EPS": "eps",
        }
    )
    missing_cap = set(V2_CORE_NUMERIC_COLUMNS) - set(cap.columns)
    missing_fund = set(V2_FUNDAMENTAL_NUMERIC_COLUMNS) - set(fundamental.columns)
    if missing_cap:
        raise ValueError(f"v2 market-cap response missing columns: {sorted(missing_cap)}")
    if missing_fund:
        raise ValueError(
            f"v2 fundamental response missing columns: {sorted(missing_fund)}"
        )

    cap = cap[["code", *V2_CORE_NUMERIC_COLUMNS]].copy()
    fundamental = fundamental[["code", *V2_FUNDAMENTAL_NUMERIC_COLUMNS]].copy()
    for column in V2_CORE_NUMERIC_COLUMNS:
        cap[column] = pd.to_numeric(cap[column], errors="coerce")
    core_columns = list(V2_CORE_NUMERIC_COLUMNS)
    invalid_core = ~cap[core_columns].notna().all(axis=1)
    invalid_core |= ~cap[["close", "market_cap"]].gt(0).all(axis=1)
    invalid_core |= ~cap["trading_val"].ge(0)
    if invalid_core.any():
        raise ValueError(
            f"v2 market-cap response has invalid core rows: {market} {dt}"
        )
    # The market-cap response is the authoritative universe.  A left join
    # intentionally discards fundamental-only tickers and validates both sides.
    try:
        merged = cap.merge(
            fundamental,
            on="code",
            how="left",
            validate="one_to_one",
            sort=False,
        )
    except (KeyError, pd.errors.MergeError) as exc:
        raise ValueError(f"v2 market/fundamental ticker join failed: {market} {dt}") from exc

    for column in V2_CORE_NUMERIC_COLUMNS:
        merged[column] = pd.to_numeric(merged[column], errors="coerce")
        invalid_range = (
            (merged[column] <= 0)
            if column in ("close", "market_cap")
            else (merged[column] < 0)
        )
        if merged[column].isna().any() or invalid_range.any():
            raise ValueError(
                f"v2 market-cap response has missing/invalid {column}: {market} {dt}"
            )
    for column in V2_FUNDAMENTAL_NUMERIC_COLUMNS:
        merged[column] = pd.to_numeric(merged[column], errors="coerce")

    merged["date"] = pd.Timestamp(dt)
    merged["market"] = market
    merged["is_preferred"] = ~merged["code"].str.endswith("0")
    result = merged[
        [*V2_COLUMNS[2:3], *V2_CORE_NUMERIC_COLUMNS, *V2_FUNDAMENTAL_NUMERIC_COLUMNS]
    ].copy()
    result["is_preferred"] = merged["is_preferred"]
    result.insert(0, "market", merged["market"])
    result.insert(0, "date", merged["date"])
    _validate_v2_frame(result, required_markets=(market,))
    return result


def _read_month_frame_for_validation(
    frame: pd.DataFrame, market: str, month: str
) -> pd.DataFrame:
    _validate_v2_frame(frame, required_markets=(market,), expected_month=month)
    return frame


def _transactional_v2_replace(
    v2_root: Path, replacements: list[tuple[Path, Path]]
) -> None:
    """Replace a v2 snapshot with byte-preserving rollback on any failure."""
    if not replacements:
        return
    backup_dir = Path(tempfile.mkdtemp(prefix=".market_data_v2-backup-", dir=str(v2_root.parent)))
    backups = {}
    try:
        for index, (destination, _source) in enumerate(replacements):
            if destination.is_file():
                backup = backup_dir / str(index)
                shutil.copy2(destination, backup)
                backups[destination] = backup

        replaced = []
        try:
            for destination, source in replacements:
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, destination)
                replaced.append(destination)
        except Exception:
            # Do not use os.replace for rollback: a test or filesystem can
            # fail the second replacement, and rollback must still work.
            for destination, _source in replacements:
                backup = backups.get(destination)
                if backup is not None and backup.is_file():
                    shutil.copy2(backup, destination)
                elif destination in replaced and destination.exists():
                    destination.unlink()
            raise
    finally:
        shutil.rmtree(backup_dir, ignore_errors=True)


def _build_v2_manifest(market_scope, inventory=None) -> dict:
    market_scope = _normalise_market_scope(market_scope)
    return {
        "schema": V2_SCHEMA_NAME,
        "schema_version": V2_SCHEMA_VERSION,
        "source": "pykrx",
        "pykrx_collection_functions": list(V2_COLLECTION_FUNCTIONS),
        "requested_market_scope": list(market_scope),
        "price_basis": V2_PRICE_BASIS,
        "known_corporate_action_limitations": V2_CORPORATE_ACTION_LIMITATION,
        "inventory": inventory
        if inventory is not None
        else {market: {} for market in market_scope},
    }


def _fetch_rebalancing_data_v2(
    start_date,
    end_date,
    cache_dir=None,
    force_refresh=False,
    lag_months=0,
    market_scope=V2_DEFAULT_MARKET_SCOPE,
    execution_mode="same_close",
) -> pd.DataFrame:
    """Collect/read the opt-in v2 contract without touching legacy cache files."""
    market_scope = _normalise_market_scope(market_scope)
    fetch_start_ts, fetch_end_ts = _parse_v2_dates(start_date, end_date, lag_months)
    target_dates = _v2_target_dates(
        start_date, end_date, lag_months, execution_mode=execution_mode
    )
    if not target_dates:
        return pd.DataFrame()
    year_months = _v2_months(target_dates)
    expected_by_month = _v2_expected_by_month(target_dates)
    v2_root = _v2_cache_root(cache_dir)
    manifest_file = v2_root / "manifest.json"

    manifest = None
    rebuilt_inventory = {market: {} for market in market_scope}
    if manifest_file.exists():
        try:
            manifest = _read_v2_manifest(v2_root, market_scope)
        except ValueError:
            if not force_refresh:
                raise
            # A forced rebuild does not trust a malformed manifest.  It may
            # replace the requested scope with a fresh, self-consistent one.
            manifest = None
    elif v2_root.exists() and any(v2_root.rglob("*.parquet")):
        if not force_refresh:
            raise ValueError("v2 parquet files exist without manifest.json")

    requested_keys = {(market, month) for market in market_scope for month in year_months}
    trusted_parts = {}
    reconstruct_months = set(year_months) if manifest is None and force_refresh else set()
    if manifest is None and force_refresh and v2_root.exists():
        # A malformed manifest cannot be used to identify trustworthy bytes.
        # Preserve only independently valid, out-of-request files and rebuild
        # their inventory from the parquet itself; requested files are never
        # read in this branch.
        for market in market_scope:
            for path in sorted((v2_root / market).glob("*.parquet")):
                month = path.stem
                if (market, month) in requested_keys:
                    continue
                part = _read_v2_parquet(path, market, month)
                trusted_parts[(market, month)] = part
                rebuilt_inventory[market][month] = _v2_inventory_record(path, part)
    if manifest is not None:
        if force_refresh:
            # A valid requested artifact may contribute dates outside this
            # request.  If one market's artifact fails verification, however,
            # the whole touched month is reconstructed without those extras.
            for month in year_months:
                for market in market_scope:
                    expected = manifest["inventory"].get(market, {}).get(month)
                    if expected is None:
                        reconstruct_months.add(month)
                        continue
                    trusted = _try_read_verified_v2_artifact(
                        v2_root, market, month, expected
                    )
                    if trusted is None:
                        reconstruct_months.add(month)
                    else:
                        trusted_parts[(market, month)] = trusted

            for market in manifest["inventory"]:
                for month in manifest["inventory"][market]:
                    if month in year_months:
                        continue
                    path = v2_root / market / f"{month}.parquet"
                    if _v2_sha256(path) != manifest["inventory"][market][month]["sha256"]:
                        raise ValueError(f"v2 unrequested artifact is corrupted: {path}")
                    trusted_parts[(market, month)] = _read_v2_parquet(
                        path, market, month
                    )
        else:
            trusted_parts = _verify_v2_inventory(v2_root, manifest)

    if force_refresh:
        for month in year_months:
            date_sets = [
                set(_v2_date_only_series(trusted_parts[(market, month)]["date"]))
                for market in market_scope
                if (market, month) in trusted_parts
            ]
            if date_sets and len(date_sets) != len(market_scope):
                reconstruct_months.add(month)
            elif date_sets and any(date_set != date_sets[0] for date_set in date_sets[1:]):
                reconstruct_months.add(month)

    for market, month in requested_keys:
        if month in reconstruct_months:
            trusted_parts.pop((market, month), None)

    cached_parts = {}
    files_to_fetch = []
    missing_by_key = {}
    for market in market_scope:
        for month in year_months:
            key = (market, month)
            if force_refresh:
                files_to_fetch.append(key)
                missing_by_key[key] = set(expected_by_month[month])
                continue
            cached = trusted_parts.get(key)
            if cached is None:
                files_to_fetch.append(key)
                missing_by_key[key] = set(expected_by_month[month])
                continue
            actual_dates = set(_v2_date_only_series(cached["date"]))
            missing_dates = expected_by_month[month] - actual_dates
            if missing_dates:
                files_to_fetch.append(key)
                missing_by_key[key] = missing_dates
            else:
                cached_parts[key] = cached

    fetch_dates = sorted(
        {
            date
            for missing_dates in missing_by_key.values()
            for date in missing_dates
        }
    )
    fetched_parts = {}
    files_to_fetch_set = set(files_to_fetch)
    for dt in tqdm(fetch_dates, desc="KRX v2 데이터 다운로드"):
        dt_str = dt.strftime("%Y%m%d")
        month = str(pd.Timestamp(dt).to_period("M"))
        for market in market_scope:
            key = (market, month)
            if key not in files_to_fetch_set or dt not in missing_by_key[key]:
                continue
            try:
                # Both calls deliberately receive the same explicit market.
                df_mcap = stock.get_market_cap(dt_str, market=market)
                df_fund = stock.get_market_fundamental(dt_str, market=market)
                part = _merge_v2_market_snapshot(df_mcap, df_fund, dt, market)
            except Exception as exc:
                raise RuntimeError(
                    f"v2 pykrx collection failed: {market} {dt_str}"
                ) from exc
            fetched_parts.setdefault(key, []).append(part)

    combined_parts = {}
    for market, month in files_to_fetch:
        key = (market, month)
        parts = fetched_parts.get(key, [])
        if not parts:
            raise ValueError(f"v2 collection returned no rows: {market} {month}")
        existing = trusted_parts.get(key)
        fetched_dates = {
            pd.Timestamp(date)
            for part in parts
            for date in _v2_date_only_series(part["date"])
        }
        if existing is not None:
            existing = existing[
                ~_v2_date_only_series(existing["date"]).isin(fetched_dates)
            ]
        combined = pd.concat(
            [frame for frame in (existing, *parts) if frame is not None],
            ignore_index=True,
        )
        combined = combined.loc[:, V2_COLUMNS].sort_values(
            ["date", "code"], kind="stable"
        )
        combined = combined.reset_index(drop=True)
        _read_month_frame_for_validation(combined, market, month)
        actual_dates = set(_v2_date_only_series(combined["date"]))
        if not expected_by_month[month].issubset(actual_dates):
            raise ValueError(f"v2 collection has incomplete coverage: {market}/{month}")
        combined_parts[key] = combined

    candidate_parts = []
    for market in market_scope:
        for month in year_months:
            key = (market, month)
            if key in combined_parts:
                candidate_parts.append(combined_parts[key])
            elif key in cached_parts:
                candidate_parts.append(cached_parts[key])
            else:
                raise ValueError(f"v2 data missing required market/month: {key}")

    candidate = pd.concat(candidate_parts, ignore_index=True)
    candidate_manifest = (
        json.loads(json.dumps(manifest))
        if manifest is not None
        else _build_v2_manifest(market_scope, inventory=rebuilt_inventory)
    )
    candidate_inventory = candidate_manifest["inventory"]
    for key, part in combined_parts.items():
        market, month = key
        # The hash is filled after the staged parquet has been written below.
        candidate_inventory.setdefault(market, {})[month] = {
            "dates": sorted(
                _v2_date_only_series(part["date"])
                .dt.strftime("%Y-%m-%d")
                .unique()
            ),
            "row_count": int(len(part)),
            "sha256": "0" * 64,
        }
    # Validate the complete touched month, including preserved extra dates,
    # before staging.  Range-only validation would permit asymmetric files.
    validate_market_data_v2(candidate, candidate_manifest, market_scope)

    if not combined_parts and manifest is not None:
        return load_market_data_v2(
            cache_dir=cache_dir,
            start_date=fetch_start_ts,
            end_date=fetch_end_ts,
            market_scope=market_scope,
            execution_mode=execution_mode,
        )

    cache_base = v2_root.parent
    cache_base.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix=".market_data_v2-", dir=str(cache_base)))
    try:
        staged_files = {}
        for (market, month), part in combined_parts.items():
            staged_file = staging_dir / market / f"{month}.parquet"
            staged_file.parent.mkdir(parents=True, exist_ok=True)
            part.to_parquet(staged_file, index=False)
            staged_files[(market, month)] = staged_file
            candidate_manifest["inventory"][market][month]["sha256"] = _v2_sha256(
                staged_file
            )
        validate_market_data_v2_manifest(candidate_manifest, market_scope)
        staged_manifest = staging_dir / "manifest.json"
        staged_manifest.write_text(
            json.dumps(candidate_manifest, indent=2, ensure_ascii=False, sort_keys=True)
        )
        replacements = [
            (v2_root / market / f"{month}.parquet", staged_file)
            for (market, month), staged_file in staged_files.items()
        ]
        replacements.append((manifest_file, staged_manifest))
        _transactional_v2_replace(v2_root, replacements)
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    return load_market_data_v2(
        cache_dir=cache_dir,
        start_date=fetch_start_ts,
        end_date=fetch_end_ts,
        market_scope=market_scope,
        execution_mode=execution_mode,
    )


def clear_cache(cache_dir=None):
    cache_base = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    if cache_base.exists():
        import shutil

        shutil.rmtree(cache_base)
        print(f"캐시 디렉토리 삭제 완료: {cache_base}")
    else:
        print("캐시 디렉토리가 존재하지 않습니다.")


def _fetch_kospi_for_ma(
    config: Config, cache_dir=None, cache_only=False
) -> pd.DataFrame:
    """Load daily KOSPI closes, optionally refusing every network fallback."""
    cache_base = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    cache_base.mkdir(parents=True, exist_ok=True)
    cache_file = cache_base / "kospi_ma.parquet"

    req_start = pd.Timestamp(config.start_date) - pd.DateOffset(days=420)
    req_end = pd.Timestamp(config.end_date)

    cache_error = None
    if cache_file.exists():
        try:
            df = pd.read_parquet(cache_file)
            if "date" in df.columns and "kospi_close" in df.columns:
                df["date"] = pd.to_datetime(df["date"], errors="coerce")
                df = df.set_index("date")
            else:
                df.index = pd.to_datetime(df.index, errors="coerce")
            df.index = df.index.normalize()
            df = df[~df.index.isna()]
            df = df[~df.index.duplicated(keep="last")].sort_index()
            if "kospi_close" not in df.columns:
                raise ValueError("cached KOSPI regime has no kospi_close column")
            df["kospi_close"] = pd.to_numeric(df["kospi_close"], errors="coerce")
            if (
                df.empty
                or df["kospi_close"].isna().any()
                or (df["kospi_close"] <= 0).any()
            ):
                raise ValueError("cached KOSPI regime has invalid close values")
            idx_min, idx_max = df.index.min(), df.index.max()
            required_end = min(req_end, pd.Timestamp.now().normalize())
            if (
                not pd.isna(idx_min)
                and not pd.isna(idx_max)
                and idx_min <= req_start
                and idx_max >= required_end
            ):
                if cache_only:
                    ma = df["kospi_close"].rolling(
                        config.ma_window, min_periods=config.ma_window
                    ).mean()
                    start_ma = ma[ma.index <= pd.Timestamp(config.start_date)].tail(1)
                    end_ma = ma[ma.index <= required_end].tail(1)
                    if (
                        start_ma.empty
                        or end_ma.empty
                        or pd.isna(start_ma.iloc[0])
                        or pd.isna(end_ma.iloc[0])
                    ):
                        raise ValueError(
                            "cached KOSPI regime has insufficient MA coverage"
                        )
                return df
            cache_error = ValueError(
                "cached KOSPI regime does not cover the requested lookback/horizon"
            )
        except Exception as exc:
            cache_error = exc

    if cache_only:
        detail = f": {cache_error}" if cache_error is not None else ""
        raise ValueError(f"cache-only KOSPI regime unavailable{detail}") from cache_error

    fetch_start = req_start.strftime("%Y%m%d")
    today = pd.Timestamp.now().normalize()
    fetch_end = min(req_end, today).strftime("%Y%m%d")

    try:
        raw = stock.get_index_ohlcv_by_date(fetch_start, fetch_end, "1001")
        df = raw.reset_index()
        df["date"] = pd.to_datetime(df["날짜"])
        df = (
            df[["date", "종가"]]
            .rename(columns={"종가": "kospi_close"})
            .set_index("date")
        )
        df.index.name = "date"
        df = df[~df.index.duplicated(keep="last")].sort_index()
        df.to_parquet(cache_file)
        return df
    except Exception:
        return pd.DataFrame()


def _execution_event(
    track_stats,
    event_type,
    classification,
    disposition,
    signal_date,
    execution_date,
    code=None,
    **extra,
):
    if track_stats is None:
        return
    rebalance_id = extra.pop("rebalance_id", None)
    if rebalance_id is None:
        signal_label = pd.Timestamp(signal_date).date()
        execution_label = (
            execution_date
            if isinstance(execution_date, str)
            else pd.Timestamp(execution_date).date()
        )
        rebalance_id = f"{signal_label}->{execution_label}"
    if disposition == "ORDER_EXECUTED":
        event_type = "ORDER_EXECUTED"
    def date_text(value):
        if value is None:
            return None
        if isinstance(value, str) and value.startswith("END_HORIZON"):
            return value
        return str(pd.Timestamp(value).date())

    track_stats["events"].append(
        {
            "rebalance_id": rebalance_id,
            "event_type": event_type,
            "classification": classification,
            "disposition": disposition,
            "signal_date": date_text(signal_date),
            "execution_date": date_text(execution_date),
            "code": code,
            "market": extra.pop("market", "UNKNOWN"),
            "side": extra.pop("side", "UNKNOWN"),
            "close": extra.pop("close", None),
            "trading_val": extra.pop("trading_val", None),
            "requested_krw": extra.pop("requested_krw", 0.0),
            "order_value": extra.pop("order_value", 0.0),
            **extra,
        }
    )
    if disposition == "VALUATION_INCOMPLETE":
        for record in track_stats["rebalances"]:
            if record.get("rebalance_id") == rebalance_id:
                record["telemetry_complete"] = False


def _frame_row(frame, code, market=None):
    rows = frame[frame["code"].astype(str) == str(code)]
    if market is not None and "market" in rows.columns:
        rows = rows[rows["market"].astype(str) == str(market)]
    return rows.iloc[0] if not rows.empty else None


def _asset_row(frame, asset):
    """Find a holding's observation without discarding its market identity."""
    return _frame_row(frame, asset["code"], asset.get("market"))


def _asset_market(asset, row=None):
    """Return the most specific known market for a holding observation."""
    if row is not None and "market" in row.index:
        market = row.get("market")
        if market is not None and not pd.isna(market):
            return str(market)
    market = asset.get("market") if asset is not None else None
    if market is not None and not pd.isna(market):
        return str(market)
    return "UNKNOWN"


def _valuation_reference(asset):
    """Explicit valuation-only fallback; never use this as an execution price."""
    reference = pd.to_numeric(
        pd.Series([asset.get("buy_price", 0.0)]), errors="coerce"
    ).iloc[0]
    return float(reference) if pd.notna(reference) and reference > 0 else 0.0


def _record_valuation_incomplete(
    track_stats,
    asset,
    signal_date,
    execution_date,
    rebalance_id,
    reason="ABSENT_VALUATION_ROW",
):
    """Ledger an observation that is usable only as an explicit valuation reference."""
    reference = _valuation_reference(asset)
    _execution_event(
        track_stats,
        reason,
        "VALUATION_ONLY_REFERENCE",
        "VALUATION_INCOMPLETE",
        signal_date,
        execution_date,
        asset["code"],
        rebalance_id=rebalance_id,
        market=_asset_market(asset),
        side="VALUATION",
        close=reference,
        trading_val=None,
        requested_krw=asset["shares"] * reference,
        order_value=0.0,
    )


def _value_asset_at(
    frame,
    asset,
    track_stats=None,
    signal_date=None,
    execution_date=None,
    rebalance_id=None,
):
    """Value an asset at an observed close or a clearly ledgered fallback.

    The fallback is deliberately valuation-only.  Callers must never feed this
    value into an order calculation.
    """
    row = _asset_row(frame, asset) if frame is not None else None
    if row is not None:
        _validate_execution_row(row, asset["code"], execution_date)
        return asset["shares"] * float(row["close"]), row
    if track_stats is not None:
        _record_valuation_incomplete(
            track_stats,
            asset,
            signal_date,
            execution_date,
            rebalance_id,
        )
    return asset["shares"] * _valuation_reference(asset), None


def _validate_execution_row(row, code, execution_date):
    if row is None:
        return
    close = pd.to_numeric(pd.Series([row["close"]]), errors="coerce").iloc[0]
    trading_val = pd.to_numeric(
        pd.Series([row["trading_val"]]), errors="coerce"
    ).iloc[0]
    if pd.isna(close) or pd.isna(trading_val) or close <= 0 or trading_val < 0:
        raise ValueError(
            f"invalid execution observation for {code} on {execution_date}"
        )


def _weights_and_hhi(values, total):
    if total <= 0:
        return {"CASH": 1.0}, 1.0
    weights = {code: value / total for code, value in values.items() if value}
    cash_value = values.get("CASH", 0.0)
    if "CASH" not in weights or cash_value:
        weights["CASH"] = cash_value / total
    return weights, sum(weight * weight for weight in weights.values())


def _allocation_budgets(rows, available_cash, allocation_mode):
    """Return fixed cash budgets for the supplied signal-date target rows."""
    if allocation_mode not in ("equal_weight", "market_cap_weight"):
        raise ValueError(
            "allocation_mode must be exactly 'equal_weight' or 'market_cap_weight'"
        )
    if len(rows) == 0:
        return {}
    if allocation_mode == "equal_weight":
        budget = available_cash / len(rows)
        return {row["code"]: budget for _, row in rows.iterrows()}

    if "market_cap" not in rows.columns:
        raise ValueError("market_cap_weight requires signal-date market_cap")
    caps = pd.to_numeric(rows["market_cap"], errors="coerce")
    if caps.isna().any() or not caps.map(math.isfinite).all() or (caps <= 0).any():
        raise ValueError("market_cap_weight requires finite positive signal-date caps")
    total_cap = float(caps.sum())
    if not math.isfinite(total_cap) or total_cap <= 0:
        raise ValueError("market_cap_weight requires a finite positive cap total")
    return {
        row["code"]: available_cash * float(cap) / total_cap
        for (_, row), cap in zip(rows.iterrows(), caps)
    }


def _record_rebalance_telemetry(
    track_stats,
    signal_date,
    execution_date,
    mode,
    cash_pre,
    cash_post,
    pre_values,
    target_values,
    post_values,
    telemetry_complete=True,
    rebalance_id=None,
    target_codes=None,
    retained_codes=None,
    sell_codes=None,
    new_codes=None,
):
    if track_stats is None:
        return
    if rebalance_id is None:
        rebalance_id = (
            f"{pd.Timestamp(signal_date).date()}->{pd.Timestamp(execution_date).date()}"
        )
    def date_text(value):
        if value is None:
            return None
        if isinstance(value, str) and value.startswith("END_HORIZON"):
            return value
        return str(pd.Timestamp(value).date())

    nav_pre = cash_pre + sum(pre_values.values())
    nav_post = cash_post + sum(post_values.values())
    pre_values = dict(pre_values)
    target_values = dict(target_values)
    post_values = dict(post_values)
    pre_values["CASH"] = cash_pre
    target_values.setdefault("CASH", max(nav_pre - sum(target_values.values()), 0.0))
    post_values["CASH"] = cash_post
    pre_weights, hhi_pre = _weights_and_hhi(pre_values, nav_pre)
    target_weights, hhi_target = _weights_and_hhi(target_values, nav_pre)
    post_weights, hhi_post = _weights_and_hhi(post_values, nav_post)
    codes = set(pre_values) | set(post_values)
    one_way = 0.5 * sum(
        abs(post_values.get(code, 0.0) - pre_values.get(code, 0.0))
        for code in codes
    )
    l1_error = sum(
        abs(post_weights.get(code, 0.0) - target_weights.get(code, 0.0))
        for code in set(post_weights) | set(target_weights)
    )
    track_stats["rebalances"].append(
        {
            "signal_date": date_text(signal_date),
            "execution_date": date_text(execution_date),
            "rebalance_id": rebalance_id,
            "mode": mode,
            "nav_pre": nav_pre,
            "nav_post": nav_post,
            "cash_pre": cash_pre,
            "cash_post": cash_post,
            "pre_weights": pre_weights,
            "target_weights": target_weights,
            "post_weights": post_weights,
            "hhi_pre": hhi_pre,
            "hhi_target": hhi_target,
            "hhi_post": hhi_post,
            "one_way_turnover_krw": one_way,
            "one_way_turnover_pct": one_way / nav_pre if nav_pre else 0.0,
            "l1_target_error": l1_error,
            "telemetry_complete": telemetry_complete,
            "pre_values": pre_values,
            "target_values": target_values,
            "post_values": post_values,
            "target_codes": list(target_codes or []),
            "retained_codes": list(retained_codes or []),
            "sell_codes": list(sell_codes or []),
            "new_codes": list(new_codes or []),
        }
    )
    if any(
        event.get("rebalance_id") == rebalance_id
        and event.get("disposition") in {"ORDER_SKIPPED", "VALUATION_INCOMPLETE"}
        for event in track_stats["events"]
    ):
        track_stats["rebalances"][-1]["telemetry_complete"] = False


def _execution_market(row, asset=None):
    if row is not None and "market" in row.index:
        return row["market"]
    if asset is not None:
        return asset.get("market")
    return None


def _asset_from_row(row):
    return {
        "code": row["code"],
        "market": row.get("market"),
    }


def run_backtest(
    market_data, config: Config, cache_dir=None, track_stats=None, cache_only=False
):
    if config.rebalance_freq not in ("monthly", "quarterly"):
        raise ValueError(
            f"지원하지 않는 리밸런싱 주기: '{config.rebalance_freq}' "
            "(monthly / quarterly 만 지원)"
        )
    if config.execution_mode not in ("same_close", "next_close"):
        raise ValueError("execution_mode must be exactly 'same_close' or 'next_close'")
    if track_stats is not None:
        track_stats.setdefault("buy_ratios", [])
        track_stats.setdefault("sell_ratios", [])
        track_stats.setdefault("events", [])
        track_stats.setdefault("rebalances", [])
    initial_capital = config.initial_capital
    buy_cost, sell_cost, slippage = config.buy_cost, config.sell_cost, config.slippage
    n_stocks = config.n_stocks

    market_data = market_data.copy()
    if config.execution_mode == "next_close" and "market" not in market_data.columns:
        raise ValueError("next_close requires validated v2 market data with market column")
    start_ts = pd.Timestamp(config.start_date)
    end_ts = pd.Timestamp(config.end_date)
    market_data["date"] = pd.to_datetime(market_data["date"])
    signal_data = market_data[
        (market_data["date"] >= start_ts) & (market_data["date"] <= end_ts)
    ].copy()
    if signal_data.empty:
        raise ValueError(
            f"기간 {config.start_date}~{config.end_date}에 market_data 없음"
        )
    if config.use_katsenelson:
        market_data = merge_dart_financials(market_data, cache_dir=cache_dir)
        signal_data = market_data[
            (market_data["date"] >= start_ts) & (market_data["date"] <= end_ts)
        ].copy()
    if config.execution_mode == "same_close":
        market_data = signal_data.copy()
    signal_data["year_month"] = signal_data["date"].dt.to_period("M")
    market_data["year_month"] = market_data["date"].dt.to_period("M")
    unique_months = sorted(signal_data["year_month"].unique())
    next_close_targets = {}
    next_close_signals = {}
    if config.execution_mode == "next_close":
        plan = _v2_signal_execution_plan(
            config.start_date, config.end_date, execution_mode="next_close"
        )
        for month in unique_months:
            item = plan.get(month)
            if item is None:
                next_close_targets[month] = None
                next_close_signals[month] = None
            else:
                next_close_targets[month] = item["execution_date"]
                next_close_signals[month] = item["signal_date"]

    # ── Pre-compute first-day close per month for momentum/volatility ──
    monthly_close = {}
    for ym in unique_months:
        ym_df = signal_data[signal_data["year_month"] == ym]
        first_date = sorted(ym_df["date"].unique())[0]
        monthly_close[ym] = ym_df[ym_df["date"] == first_date].set_index("code")[
            "close"
        ]

    # ── KOSPI 200-day MA market regime ──
    # A disabled regime is a strict cache/network no-op.  Phase 2 relies on
    # this branch to remain cache-only.
    if config.use_market_regime:
        kospi_regime = (
            _fetch_kospi_for_ma(config, cache_dir, cache_only=True)
            if cache_only
            else _fetch_kospi_for_ma(config, cache_dir)
        )
    else:
        kospi_regime = pd.DataFrame()
    if not kospi_regime.empty:
        kospi_regime["ma200"] = (
            kospi_regime["kospi_close"]
            .rolling(config.ma_window, min_periods=config.ma_window)
            .mean()
        )

    cash = initial_capital
    current_portfolio = []
    history = []
    total_cost_spent = 0
    _katsenelson_empty_warned = [False]
    first_first_day = None
    last_real_rebalance_id = None

    first_backtest_month = None
    if (
        config.fundamental_lag_months > 0
        and len(unique_months) > config.fundamental_lag_months
    ):
        first_backtest_month = unique_months[config.fundamental_lag_months]

    for current_month in unique_months:
        if first_backtest_month is not None and current_month < first_backtest_month:
            continue

        month_df = signal_data[signal_data["year_month"] == current_month]
        if month_df.empty:
            continue

        trading_days = sorted(month_df["date"].unique())
        if len(trading_days) < (1 if config.execution_mode == "next_close" else 2):
            continue

        first_day = trading_days[0]
        last_day = trading_days[-1]
        if config.execution_mode == "next_close":
            planned_signal = next_close_signals.get(current_month)
            if planned_signal is not None:
                first_day = planned_signal
                last_day = max(
                    signal_data.loc[
                        signal_data["year_month"] == current_month, "date"
                    ].unique()
                )
                if first_day not in set(market_data["date"]):
                    raise ValueError(
                        f"next_close missing exact signal date for {current_month}"
                    )
        execution_day = first_day
        if config.execution_mode == "next_close":
            execution_day = next_close_targets.get(current_month)
            if execution_day is None:
                rebalance_id = f"{pd.Timestamp(first_day).date()}->END_HORIZON"
                evaluation_date = last_day
                evaluation_df = month_df[month_df["date"] == evaluation_date]
                pre_values = {}
                for asset in current_portfolio:
                    value, _row = _value_asset_at(
                        evaluation_df,
                        asset,
                        track_stats=track_stats,
                        signal_date=first_day,
                        execution_date="END_HORIZON",
                        rebalance_id=rebalance_id,
                    )
                    pre_values[asset["code"]] = value
                evaluation_value = cash + sum(pre_values.values())
                history_row = {
                    "Date": str(pd.Timestamp(evaluation_date).date()),
                    "Portfolio_Value": int(evaluation_value),
                    "Total_Return(%)": round(
                        ((evaluation_value - initial_capital) / initial_capital) * 100,
                        2,
                    ),
                    "Stock_Count": len(current_portfolio),
                }
                if not history or pd.Timestamp(history[-1]["Date"]) < pd.Timestamp(
                    evaluation_date
                ):
                    history.append(history_row)
                elif pd.Timestamp(history[-1]["Date"]) == pd.Timestamp(evaluation_date):
                    history[-1] = history_row
                if first_first_day is None:
                    first_first_day = first_day
                _record_rebalance_telemetry(
                    track_stats,
                    first_day,
                    "END_HORIZON",
                    config.execution_mode,
                    cash,
                    cash,
                    pre_values,
                    pre_values,
                    pre_values,
                    telemetry_complete=False,
                    rebalance_id=rebalance_id,
                )
                _execution_event(
                    track_stats,
                    "REBALANCE_SKIPPED_END_HORIZON",
                    "T1_AFTER_EVALUATION_END",
                    "REBALANCE_SKIPPED",
                    first_day,
                    "END_HORIZON",
                    code="PORTFOLIO",
                    rebalance_id=rebalance_id,
                    market="ALL",
                    side="REBALANCE",
                    close=None,
                    trading_val=None,
                    requested_krw=0.0,
                    order_value=0.0,
                )
                continue
            if execution_day not in set(market_data["date"]):
                raise ValueError(f"next_close missing exact T+1 date for {current_month}")

        if first_first_day is None:
            first_first_day = first_day

        month_cost = 0

        first_day_df = market_data[market_data["date"] == first_day]
        execution_df = month_df[month_df["date"] == execution_day]
        if config.execution_mode == "next_close":
            execution_df = market_data[market_data["date"] == execution_day]

        # ── Market regime: KOSPI 200-day MA ──
        is_bull = True
        if config.use_market_regime and not kospi_regime.empty:
            first_ts = pd.Timestamp(first_day)
            closest = kospi_regime[kospi_regime.index <= first_ts].tail(1)
            if (
                not closest.empty
                and closest["ma200"].notna().iloc[0]
                and closest["kospi_close"].notna().iloc[0]
            ):
                is_bull = closest["kospi_close"].iloc[0] >= closest["ma200"].iloc[0]

        # ── Bear market → liquidate ──
        rebalance_id = f"{pd.Timestamp(first_day).date()}->{pd.Timestamp(execution_day).date()}"
        if not is_bull and current_portfolio:
            sell_proceeds = 0
            bear_mark_source = (
                execution_df if config.execution_mode == "next_close" else first_day_df
            )
            pre_bear_values = {}
            bear_valuation_complete = True
            bear_sell_codes = [asset["code"] for asset in current_portfolio]
            for asset in current_portfolio:
                value, mark = _value_asset_at(
                    bear_mark_source,
                    asset,
                    track_stats=track_stats if config.execution_mode == "next_close" else None,
                    signal_date=first_day,
                    execution_date=execution_day,
                    rebalance_id=rebalance_id,
                )
                pre_bear_values[asset["code"]] = value
                bear_valuation_complete &= mark is not None
            skipped_sells = []
            for asset in current_portfolio:
                stock_info = (
                    execution_df if config.execution_mode == "next_close" else first_day_df
                )
                stock_info = stock_info[stock_info["code"] == asset["code"]]
                if stock_info.empty:
                    if config.execution_mode == "next_close":
                        _execution_event(
                            track_stats,
                            "ABSENT_EXECUTION_ROW",
                            "OBSERVED_ABSENCE_UNKNOWN_CAUSE",
                            "ORDER_SKIPPED",
                            first_day,
                            execution_day,
                            asset["code"],
                            rebalance_id=rebalance_id,
                            market=_asset_market(asset),
                            side="SELL",
                            requested_krw=0.0,
                            close=None,
                            trading_val=None,
                        )
                        skipped_sells.append(asset)
                        continue
                    exec_sell_price = asset["buy_price"] * 0.1
                    eff_slippage = slippage
                else:
                    if config.execution_mode == "next_close":
                        _validate_execution_row(stock_info.iloc[0], asset["code"], execution_day)
                        if stock_info.iloc[0]["trading_val"] == 0:
                            _execution_event(
                                track_stats,
                                "ZERO_TRADING_VALUE",
                                "OBSERVED_NONTRADABLE_UNKNOWN_CAUSE",
                                "ORDER_SKIPPED",
                                first_day,
                                execution_day,
                                asset["code"],
                                rebalance_id=rebalance_id,
                                market=_asset_market(asset, stock_info.iloc[0]),
                                side="SELL",
                                close=stock_info.iloc[0]["close"],
                                trading_val=0.0,
                                requested_krw=asset["shares"] * stock_info.iloc[0]["close"],
                            )
                            skipped_sells.append(asset)
                            continue
                    exec_sell_price = stock_info.iloc[0]["close"]
                    trade_val = stock_info.iloc[0]["trading_val"]
                    gross_sell_value = asset["shares"] * exec_sell_price
                    eff_slippage = (
                        min(slippage, (gross_sell_value / trade_val) * 0.5)
                        if trade_val > 0
                        else slippage
                    )
                    if track_stats is not None and trade_val > 0:
                        track_stats["sell_ratios"].append(gross_sell_value / trade_val)
                    _execution_event(
                        track_stats,
                        "ORDER_EXECUTED",
                        "OBSERVED_EXECUTABLE",
                        "ORDER_EXECUTED",
                        first_day,
                        execution_day,
                        asset["code"],
                        rebalance_id=f"{pd.Timestamp(first_day).date()}->{pd.Timestamp(execution_day).date()}",
                        market=_asset_market(asset, stock_info.iloc[0]),
                        side="SELL",
                        close=exec_sell_price,
                        trading_val=trade_val,
                        requested_krw=gross_sell_value,
                        order_value=gross_sell_value,
                    )
                gross_sell_value = asset["shares"] * exec_sell_price
                net_sell_val = gross_sell_value * (1 - eff_slippage) * (1 - sell_cost)
                month_cost += gross_sell_value - net_sell_val
                sell_proceeds += net_sell_val
            cash += sell_proceeds
            current_portfolio = skipped_sells
            total_cost_spent += month_cost
            bear_post_values = {
                asset["code"]: _value_asset_at(
                    bear_mark_source,
                    asset,
                    track_stats=None,
                    signal_date=first_day,
                    execution_date=execution_day,
                    rebalance_id=rebalance_id,
                )[0]
                for asset in skipped_sells
            }
            bear_target_values = {
                "CASH": (cash - sell_proceeds) + sum(pre_bear_values.values())
            }
            _record_rebalance_telemetry(
                track_stats,
                first_day,
                execution_day,
                config.execution_mode,
                cash - sell_proceeds,
                cash,
                pre_bear_values,
                bear_target_values,
                bear_post_values,
                telemetry_complete=bear_valuation_complete and not skipped_sells,
                target_codes=[],
                retained_codes=[asset["code"] for asset in skipped_sells],
                sell_codes=bear_sell_codes,
                new_codes=[],
            )
            last_real_rebalance_id = rebalance_id

        # ── Bull market → determine rebalance schedule ──
        if is_bull:
            month_number = current_month.month
            is_quarter_start = month_number in [1, 4, 7, 10]
            is_rebalance = (
                config.rebalance_freq == "monthly"
                or not current_portfolio
                or is_quarter_start
            )

            if is_rebalance:
                # ── Lag fundamental data (look-ahead bias 보정) ──
                if config.fundamental_lag_months > 0:
                    lag_period = current_month - config.fundamental_lag_months
                    lagged_df = market_data[market_data["year_month"] == lag_period]
                    if lagged_df.empty:
                        print(
                            f"  ⚠️ [lag] {current_month}: lag {config.fundamental_lag_months}개월 전 데이터 "
                            "없음 → 이번 리밸런싱 스킵 (당월 데이터로 폴백하지 않음)"
                        )
                        is_rebalance = False
                    else:
                        lag_dates = sorted(lagged_df["date"].unique())
                        lag_fund = lagged_df[lagged_df["date"] == lag_dates[0]][
                            ["code", "per", "pbr", "div", "bps", "eps"]
                        ].copy()
                        lag_fund.columns = [
                            "code",
                            "per_lag",
                            "pbr_lag",
                            "div_lag",
                            "bps_lag",
                            "eps_lag",
                        ]
                        first_day_df = first_day_df.merge(
                            lag_fund, on="code", how="left"
                        )
                        first_day_df["per"] = first_day_df["per_lag"]
                        first_day_df["pbr"] = first_day_df["pbr_lag"]
                        first_day_df["div"] = first_day_df["div_lag"]
                        first_day_df["bps"] = first_day_df["bps_lag"]
                        first_day_df["eps"] = first_day_df["eps_lag"]
                        first_day_df = first_day_df.drop(
                            columns=[
                                "per_lag",
                                "pbr_lag",
                                "div_lag",
                                "bps_lag",
                                "eps_lag",
                            ]
                        )

                if is_rebalance:
                    rebalance_cash_pre = cash
                    rebalance_id = f"{pd.Timestamp(first_day).date()}->{pd.Timestamp(execution_day).date()}"
                    rebalance_pre_values = {}
                    rebalance_valuation_complete = True
                    for asset in current_portfolio:
                        mark_source = (
                            execution_df
                            if config.execution_mode == "next_close"
                            else first_day_df
                        )
                        mark = _asset_row(mark_source, asset)
                        if mark is not None:
                            rebalance_pre_values[asset["code"]] = (
                                asset["shares"] * mark["close"]
                            )
                        elif config.execution_mode == "next_close":
                            rebalance_pre_values[asset["code"]] = (
                                asset["shares"] * _valuation_reference(asset)
                            )
                            rebalance_valuation_complete = False
                            _record_valuation_incomplete(
                                track_stats,
                                asset,
                                first_day,
                                execution_day,
                                rebalance_id,
                            )
                        else:
                            rebalance_pre_values[asset["code"]] = (
                                asset["shares"] * asset["buy_price"] * 0.1
                            )

                    # Select target portfolio using first_day data
                    try:
                        universe = first_day_df[
                            (~first_day_df["is_preferred"])
                            & (first_day_df["market_cap"] >= config.min_market_cap)
                            & (first_day_df["trading_val"] >= config.min_trading_val)
                        ].copy()
                    except (TypeError, ValueError) as exc:
                        if config.allocation_mode == "market_cap_weight":
                            raise ValueError(
                                "market_cap_weight requires finite positive signal-date caps"
                            ) from exc
                        raise

                    if config.max_market_cap > 0:
                        universe = universe[
                            universe["market_cap"] <= config.max_market_cap
                        ]

                    # ── 적자 기업(음수 PER)은 "저PER"로 오스코어 유입 방지 ──
                    if config.exclude_negative_per:
                        universe = universe[universe["per"] > 0].copy()

                    # ── Percentile-based fundamental filters (intersection) ──
                    if config.use_multi_factor:
                        for col in ["pbr", "div", "bps", "eps"]:
                            if col not in universe.columns:
                                universe[col] = float("nan")
                        universe["roe"] = universe["eps"] / universe["bps"]
                        pbr_r = universe["pbr"].rank(pct=True)
                        universe = universe[
                            (pbr_r <= config.pbr_pctile) & (universe["pbr"] >= 0)
                        ].copy()

                    # ── Momentum & Volatility (bias-free price-based factors) ──
                    if config.use_momentum or config.use_low_volatility:
                        if current_month in monthly_close:
                            past_month = current_month - config.momentum_window
                            if config.use_momentum and past_month in monthly_close:
                                past_prices = monthly_close[past_month]
                                universe = universe.merge(
                                    past_prices.rename("price_12m_ago"),
                                    on="code",
                                    how="left",
                                )
                                universe["momentum"] = (
                                    universe["close"] / universe["price_12m_ago"]
                                ) - 1
                                universe = universe[universe["momentum"] > -1].copy()

                            if config.use_low_volatility:
                                vol_prices = []
                                for idx in range(1, config.momentum_window + 1):
                                    ym = current_month - idx
                                    if ym in monthly_close:
                                        vol_prices.append(
                                            monthly_close[ym].rename(f"p_{idx}")
                                        )
                                if len(vol_prices) >= 6:
                                    vol_df = pd.concat(vol_prices, axis=1)
                                    vol_df.columns = [
                                        f"p_{i + 1}" for i in range(len(vol_prices))
                                    ]
                                    monthly_returns = vol_df.pct_change(
                                        axis=1, fill_method=None
                                    ).iloc[:, 1:]
                                    vol_series = (
                                        monthly_returns.std(axis=1)
                                        .dropna()
                                        .rename("volatility")
                                    )
                                    universe = universe.merge(
                                        vol_series, on="code", how="left"
                                    )

                    # ── Dynamic scoring ──
                    score_components = []

                    if config.use_katsenelson:
                        # ── 카스넬슨 가치투자: 품질 필터 + 가치 스코어링 ──
                        fin_cols = [
                            "cash",
                            "total_liabilities",
                            "total_equity",
                            "borrowings",
                            "operating_income",
                            "interest_paid",
                            "operating_cf",
                            "capex",
                            "current_assets",
                            "net_income",
                            "depreciation",
                            "revenue",
                            "revenue_3y_ago",
                            "operating_income_3y_ago",
                            "net_income_3y_ago",
                        ]
                        for col in fin_cols:
                            if col not in universe.columns:
                                universe[col] = float("nan")

                        universe = universe[universe["total_equity"].notna()].copy()
                        if universe.empty:
                            if not _katsenelson_empty_warned[0]:
                                print(
                                    f"  ⚠️ [카스넬슨] {current_month}: 재무데이터가 있는 종목이 없어 "
                                    "유니버스 전멸 → 전량 현금화 (DART 수집 상태 확인 필요)"
                                )
                                _katsenelson_empty_warned[0] = True
                            target_stocks = universe
                        else:
                            for col in fin_cols:
                                universe[col] = pd.to_numeric(
                                    universe[col], errors="coerce"
                                )

                            metrics_df = universe.apply(
                                lambda r: pd.Series(
                                    build_financial_metrics(
                                        r.to_dict(), r["market_cap"]
                                    )
                                ),
                                axis=1,
                            )
                            universe = pd.concat([universe, metrics_df], axis=1)

                            # 품질 필터 (하드 스크린)
                            # ROIC/D-E/FCF-Yield는 필수 조건. 이자보상/EV-EBITDA는
                            # 데이터가 있을 때만 적용 (무부채·이자없는 기업 불이익 방지).
                            roic_ok = universe["roic"].notna() & (
                                universe["roic"] >= config.min_roic
                            )
                            de_ok = universe["debt_to_equity"].notna() & (
                                universe["debt_to_equity"] <= config.max_debt_equity
                            )
                            fy_ok = universe["fcf_yield"].notna() & (
                                universe["fcf_yield"] >= config.min_fcf_yield
                            )
                            q = roic_ok & de_ok & fy_ok

                            # 이자보상배율: 데이터가 있는 종목만 하드 필터 적용
                            if config.min_interest_coverage > 0:
                                ic_data = universe["interest_coverage"].notna()
                                ic_ok = ~ic_data | (
                                    universe["interest_coverage"]
                                    >= config.min_interest_coverage
                                )
                                q &= ic_ok

                            # EV/EBITDA: 데이터가 있는 종목만 하드 필터 적용
                            if config.max_ev_ebitda > 0:
                                ev_data = universe["ev_ebitda"].notna()
                                ev_ok = ~ev_data | (
                                    universe["ev_ebitda"] <= config.max_ev_ebitda
                                )
                                q &= ev_ok

                            universe = universe[q].copy()

                            if not universe.empty:
                                # ── 가치 + 성장 스코어링 (Q-G-V 3요소) ──
                                score_components = []

                                # Q (질) 은 이미 하드 필터로 처리됨.
                                # G (성장): 3년 매출/영업이익 CAGR 순위 (높을수록 좋음)
                                if config.katsenelson_use_growth:
                                    universe["rank_revenue_cagr"] = universe[
                                        "revenue_cagr_3y"
                                    ].rank(ascending=False, pct=True)
                                    universe["rank_oi_cagr"] = universe[
                                        "oi_cagr_3y"
                                    ].rank(ascending=False, pct=True)
                                    score_components.extend(
                                        ["rank_revenue_cagr", "rank_oi_cagr"]
                                    )

                                # V (가격): 낮을수록 좋은 지표들 순위
                                universe["rank_ev_ebitda"] = universe["ev_ebitda"].rank(
                                    pct=True
                                )
                                universe["rank_per"] = universe["per"].rank(pct=True)
                                universe["rank_fcf_yield"] = universe["fcf_yield"].rank(
                                    ascending=False, pct=True
                                )
                                score_components.extend(
                                    [
                                        "rank_ev_ebitda",
                                        "rank_per",
                                        "rank_fcf_yield",
                                    ]
                                )

                                # NCAV는 그레이엄 net-net 보조 팩터 (선택)
                                if config.katsenelson_use_ncav:
                                    universe["rank_ncav"] = universe["ncav_ratio"].rank(
                                        ascending=False, pct=True
                                    )
                                    score_components.append("rank_ncav")

                                # 모멘텀/저변동성이 켜져 있으면 보조 팩터로 추가
                                if (
                                    config.use_momentum
                                    and "momentum" in universe.columns
                                ):
                                    universe["rank_momentum"] = universe[
                                        "momentum"
                                    ].rank(ascending=False, pct=True)
                                    score_components.append("rank_momentum")
                                if (
                                    config.use_low_volatility
                                    and "volatility" in universe.columns
                                ):
                                    universe["rank_vol"] = universe["volatility"].rank(
                                        pct=True
                                    )
                                    score_components.append("rank_vol")

                    elif config.use_momentum and "momentum" in universe.columns:
                        universe["rank_momentum"] = universe["momentum"].rank(
                            ascending=False, pct=True
                        )
                        score_components.append("rank_momentum")
                    else:
                        universe["rank_per"] = universe["per"].rank(pct=True)
                        score_components.append("rank_per")

                    if config.use_multi_factor:
                        universe["rank_roe"] = universe["roe"].rank(
                            ascending=False, pct=True
                        )
                        universe["rank_div"] = (
                            universe["div"].fillna(0).rank(ascending=False, pct=True)
                        )
                        score_components.extend(["rank_roe", "rank_div"])

                    if config.use_low_volatility and "volatility" in universe.columns:
                        universe["rank_vol"] = universe["volatility"].rank(pct=True)
                        score_components.append("rank_vol")

                    universe["score"] = universe[score_components].sum(axis=1)
                    target_stocks = universe.sort_values(by="score").head(n_stocks)
                    if config.allocation_mode == "market_cap_weight":
                        # Validate signal-T caps before any sell or buy is applied.
                        _allocation_budgets(
                            target_stocks, 1.0, config.allocation_mode
                        )

                    # Determine which current stocks to keep (partial turnover)
                    if current_portfolio and config.max_turnover < 1.0:
                        target_codes = set(target_stocks["code"])
                        if "score" in target_stocks.columns:
                            target_sort_key = dict(
                                zip(target_stocks["code"], target_stocks["score"])
                            )
                            overlap = [
                                (a, target_sort_key[a["code"]])
                                for a in current_portfolio
                                if a["code"] in target_codes
                            ]
                            overlap.sort(key=lambda x: x[1])
                        else:
                            target_sort_key = dict(
                                zip(target_stocks["code"], target_stocks["per"])
                            )
                            overlap = [
                                (a, target_sort_key[a["code"]])
                                for a in current_portfolio
                                if a["code"] in target_codes
                            ]
                            overlap.sort(key=lambda x: x[1])
                        n_keep = n_stocks - max(1, int(n_stocks * config.max_turnover))
                        keep_map = {a["code"] for a, _ in overlap[:n_keep]}
                        to_sell = [
                            a for a in current_portfolio if a["code"] not in keep_map
                        ]
                        to_keep = [
                            a for a in current_portfolio if a["code"] in keep_map
                        ]
                    else:
                        to_sell = current_portfolio[:] if current_portfolio else []
                        to_keep = []

                    # Execute sells (volume-based slippage)
                    skipped_sells = []
                    if to_sell:
                        sell_amount = 0
                        for asset in to_sell:
                            execution_source = execution_df if config.execution_mode == "next_close" else first_day_df
                            stock_row = _asset_row(execution_source, asset)
                            if stock_row is None:
                                if config.execution_mode == "next_close":
                                    _execution_event(track_stats, "ABSENT_EXECUTION_ROW", "OBSERVED_ABSENCE_UNKNOWN_CAUSE", "ORDER_SKIPPED", first_day, execution_day, asset["code"], rebalance_id=rebalance_id, market=_asset_market(asset), side="SELL", close=None, trading_val=None, requested_krw=0.0, order_value=0.0)
                                    skipped_sells.append(asset)
                                    continue
                                exec_sell_price = asset["buy_price"] * 0.1
                                eff_slippage = slippage
                            else:
                                if config.execution_mode == "next_close":
                                    _validate_execution_row(stock_row, asset["code"], execution_day)
                                    if stock_row["trading_val"] == 0:
                                        _execution_event(track_stats, "ZERO_TRADING_VALUE", "OBSERVED_NONTRADABLE_UNKNOWN_CAUSE", "ORDER_SKIPPED", first_day, execution_day, asset["code"], rebalance_id=rebalance_id, market=_asset_market(asset, stock_row), side="SELL", close=stock_row["close"], trading_val=0.0, requested_krw=asset["shares"] * stock_row["close"], order_value=0.0)
                                        skipped_sells.append(asset)
                                        continue
                                exec_sell_price = stock_row["close"]
                                trade_val = stock_row["trading_val"]
                                gross_sell_value = asset["shares"] * exec_sell_price
                                eff_slippage = (
                                    min(slippage, (gross_sell_value / trade_val) * 0.5)
                                    if trade_val > 0
                                    else slippage
                                )
                                if track_stats is not None and trade_val > 0:
                                    track_stats["sell_ratios"].append(
                                        gross_sell_value / trade_val
                                    )
                                _execution_event(track_stats, "ORDER_EXECUTED", "OBSERVED_EXECUTABLE", "ORDER_EXECUTED", first_day, execution_day, asset["code"], rebalance_id=rebalance_id, market=_asset_market(asset, stock_row), side="SELL", close=exec_sell_price, trading_val=trade_val, requested_krw=gross_sell_value, order_value=gross_sell_value)
                            gross_sell_value = asset["shares"] * exec_sell_price
                            net_sell_val = (
                                gross_sell_value * (1 - eff_slippage) * (1 - sell_cost)
                            )
                            month_cost += gross_sell_value - net_sell_val
                            sell_amount += net_sell_val
                        cash += sell_amount
                        if config.execution_mode == "next_close":
                            current_portfolio = skipped_sells + to_keep

                    # Execute buys (volume-based slippage)
                    retained = skipped_sells + to_keep[:]
                    retained_codes = {asset["code"] for asset in retained}
                    new_portfolio = retained[:]
                    n_new = max(0, n_stocks - len(retained))
                    new_to_buy = target_stocks[
                        ~target_stocks["code"].isin(retained_codes)
                    ].head(n_new)
                    if n_new > 0 and cash > 0:
                        if len(new_to_buy) > 0:
                            buy_budgets = _allocation_budgets(
                                new_to_buy, cash, config.allocation_mode
                            )
                            for _, row in new_to_buy.iterrows():
                                target_cash_per_stock = buy_budgets[row["code"]]
                                execution_row = _frame_row(execution_df, row["code"], row.get("market")) if config.execution_mode == "next_close" else row
                                if config.execution_mode == "next_close":
                                    if execution_row is None:
                                        _execution_event(track_stats, "ABSENT_EXECUTION_ROW", "OBSERVED_ABSENCE_UNKNOWN_CAUSE", "ORDER_SKIPPED", first_day, execution_day, row["code"], rebalance_id=rebalance_id, market=row.get("market", "UNKNOWN"), side="BUY", close=None, trading_val=None, requested_krw=target_cash_per_stock, order_value=0.0)
                                        continue
                                    _validate_execution_row(execution_row, row["code"], execution_day)
                                    if execution_row["trading_val"] == 0:
                                        _execution_event(track_stats, "ZERO_TRADING_VALUE", "OBSERVED_NONTRADABLE_UNKNOWN_CAUSE", "ORDER_SKIPPED", first_day, execution_day, row["code"], rebalance_id=rebalance_id, market=execution_row.get("market", "UNKNOWN"), side="BUY", close=execution_row["close"], trading_val=0.0, requested_krw=target_cash_per_stock, order_value=0.0)
                                        continue
                                trade_val = execution_row["trading_val"]
                                execution_close = execution_row["close"]
                                req_shares = int(
                                    target_cash_per_stock
                                    / (execution_close * (1 + slippage) * (1 + buy_cost))
                                )
                                if req_shares <= 0:
                                    continue
                                order_value = req_shares * execution_close
                                eff_slippage = (
                                    min(slippage, (order_value / trade_val) * 0.5)
                                    if trade_val > 0
                                    else slippage
                                )
                                if track_stats is not None and trade_val > 0:
                                    track_stats["buy_ratios"].append(
                                        order_value / trade_val
                                    )
                                exec_buy_price = execution_close * (1 + eff_slippage)
                                shares = int(
                                    target_cash_per_stock
                                    / (exec_buy_price * (1 + buy_cost))
                                )
                                if shares > 0:
                                    actual_cost = (
                                        shares * exec_buy_price * (1 + buy_cost)
                                    )
                                    month_cost += actual_cost - (shares * execution_close)
                                    cash -= actual_cost
                                    new_portfolio.append(
                                        {
                                            "code": row["code"],
                                            "shares": shares,
                                            "buy_price": execution_close,
                                            "market": execution_row.get("market", "UNKNOWN"),
                                        }
                                    )
                                    _execution_event(track_stats, "ORDER_EXECUTED", "OBSERVED_EXECUTABLE", "ORDER_EXECUTED", first_day, execution_day, row["code"], rebalance_id=rebalance_id, market=execution_row.get("market", "UNKNOWN"), side="BUY", close=execution_close, trading_val=trade_val, requested_krw=target_cash_per_stock, order_value=shares * execution_close)

                    current_portfolio = new_portfolio
                    # Preserve the market provenance of every opened holding.
                    for asset in current_portfolio:
                        if asset.get("market") is None:
                            row = _frame_row(execution_df, asset["code"])
                            if row is not None:
                                asset["market"] = row.get("market")
                    total_cost_spent += month_cost
                    target_nav = rebalance_cash_pre + sum(rebalance_pre_values.values())
                    target_values = _allocation_budgets(
                        target_stocks, target_nav, config.allocation_mode
                    )
                    post_values = {}
                    for asset in current_portfolio:
                        post_values[asset["code"]] = _value_asset_at(
                            execution_df
                            if config.execution_mode == "next_close"
                            else first_day_df,
                            asset,
                            track_stats=None,
                            signal_date=first_day,
                            execution_date=execution_day,
                            rebalance_id=rebalance_id,
                        )[0]
                    _record_rebalance_telemetry(
                        track_stats,
                        first_day,
                        execution_day,
                        config.execution_mode,
                        rebalance_cash_pre,
                        cash,
                        rebalance_pre_values,
                        target_values,
                        post_values,
                        telemetry_complete=rebalance_valuation_complete,
                        rebalance_id=rebalance_id,
                        target_codes=list(target_stocks["code"]),
                        retained_codes=[asset["code"] for asset in retained],
                        sell_codes=[asset["code"] for asset in to_sell],
                        new_codes=list(new_to_buy["code"]),
                    )
                    last_real_rebalance_id = rebalance_id

        # ── LAST DAY: mark-to-market for reporting ──
        last_day_df = month_df[month_df["date"] == last_day]
        valuation_rebalance_id = last_real_rebalance_id or rebalance_id
        holdings_value = 0
        for asset in current_portfolio:
            if config.execution_mode == "next_close":
                value, _row = _value_asset_at(
                    last_day_df,
                    asset,
                    track_stats=track_stats,
                    signal_date=first_day,
                    execution_date=last_day,
                    rebalance_id=valuation_rebalance_id,
                )
                holdings_value += value
            else:
                stock_info = last_day_df[last_day_df["code"] == asset["code"]]
                if not stock_info.empty:
                    holdings_value += asset["shares"] * stock_info.iloc[0]["close"]
                else:
                    holdings_value += asset["shares"] * asset["buy_price"] * 0.1

        portfolio_value = cash + holdings_value
        total_return = ((portfolio_value - initial_capital) / initial_capital) * 100

        history.append(
            {
                "Date": str(last_day.date()),
                "Portfolio_Value": int(portfolio_value),
                "Total_Return(%)": round(total_return, 2),
                "Stock_Count": len(current_portfolio),
            }
        )

    # Add initial state row at the beginning
    if first_first_day is not None:
        history.insert(
            0,
            {
                "Date": str(first_first_day.date()),
                "Portfolio_Value": initial_capital,
                "Total_Return(%)": 0.0,
                "Stock_Count": 0,
            },
        )
    elif config.execution_mode == "next_close":
        history.append(
            {
                "Date": str(start_ts.date()),
                "Portfolio_Value": initial_capital,
                "Total_Return(%)": 0.0,
                "Stock_Count": 0,
            }
        )
    elif not history:
        history.append(
            {
                "Date": str(start_ts.date()),
                "Portfolio_Value": initial_capital,
                "Total_Return(%)": 0.0,
                "Stock_Count": 0,
            }
        )

    history_df = pd.DataFrame(history)
    history_df["Peak"] = history_df["Portfolio_Value"].cummax()
    history_df["Drawdown"] = (
        (history_df["Portfolio_Value"] - history_df["Peak"]) / history_df["Peak"]
    ) * 100
    mdd = history_df["Drawdown"].min()

    first_date = history_df["Date"].iloc[0]
    last_date = history_df["Date"].iloc[-1]
    years = (pd.Timestamp(last_date) - pd.Timestamp(first_date)).days / 365.25
    final_value = int(history_df["Portfolio_Value"].iloc[-1])
    cagr = (
        ((final_value / initial_capital) ** (1 / years) - 1) * 100 if years > 0 else 0.0
    )

    metrics = {
        "INITIAL_CAPITAL": initial_capital,
        "FINAL_PORTFOLIO_VALUE": final_value,
        "TOTAL_RETURN_PCT": history_df["Total_Return(%)"].iloc[-1],
        "CAGR_PCT": round(cagr, 2),
        "MAX_DRAWDOWN_PCT": round(mdd, 2),
        "TOTAL_COST_IMPACT_KRW": int(total_cost_spent),
    }

    if track_stats is not None:
        for record in track_stats["rebalances"]:
            if any(
                event.get("rebalance_id") == record.get("rebalance_id")
                and event.get("disposition")
                in {"ORDER_SKIPPED", "VALUATION_INCOMPLETE", "REBALANCE_SKIPPED"}
                for event in track_stats["events"]
            ):
                record["telemetry_complete"] = False

    return history_df, metrics
