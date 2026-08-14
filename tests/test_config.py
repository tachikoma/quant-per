import pandas as pd
import pytest

from config import (
    get_korean_business_days,
    KRX_FALSE_POSITIVE_CLOSURES,
    KRX_OFFICIAL_CLOSURES,
)


def _dates(start, end):
    return pd.to_datetime(get_korean_business_days(start, end))


@pytest.mark.parametrize(
    "closure,preceding",
    [
        ("2016-12-30", "2016-12-29"),
        ("2018-12-31", "2018-12-28"),
        ("2021-12-31", "2021-12-30"),
    ],
)
def test_krx_year_end_closure_excluded_but_preceding_trading_day_kept(
    closure, preceding
):
    year = int(closure[:4])
    days = _dates(f"{year}-12-28", f"{year + 1}-01-03")
    assert pd.Timestamp(closure) not in days
    assert pd.Timestamp(preceding) in days


def test_cross_year_late_december_returns_chronological_unique_days():
    days = _dates("2021-12-28", "2022-01-05")
    assert list(days) == sorted(days)
    assert len(days) == len(set(days))
    # 2021-12-31 is removed exactly once; its neighbors survive.
    assert pd.Timestamp("2021-12-31") not in days
    assert pd.Timestamp("2021-12-28") in days
    assert pd.Timestamp("2021-12-29") in days
    assert pd.Timestamp("2021-12-30") in days
    assert pd.Timestamp("2022-01-03") in days
    assert pd.Timestamp("2022-01-04") in days
    assert pd.Timestamp("2022-01-05") in days


@pytest.mark.parametrize(
    "date",
    [
        "2016-02-08", "2016-02-09", "2016-02-10",
        "2016-04-13",
        "2016-05-06",
        "2016-09-14", "2016-09-15", "2016-09-16",
        "2017-01-27", "2017-01-30",
        "2017-05-03",
        "2017-05-09",
        "2017-10-02",
        "2017-10-04", "2017-10-05", "2017-10-06",
        "2018-06-13",
        "2020-04-15",
        "2020-08-17",
        "2021-10-11",
        "2022-03-09",
        "2022-06-01",
        "2023-10-02",
        "2024-04-10",
        "2024-10-01",
        "2025-01-27",
        "2025-03-03",
        "2025-05-06",
        "2025-06-03",
        "2025-10-08",
        "2026-06-03",
    ],
)
def test_krx_official_closure_is_excluded(date):
    days = _dates(f"{date[:4]}-01-01", f"{date[:4]}-12-31")
    assert pd.Timestamp(date) not in days


@pytest.mark.parametrize(
    "date",
    [
        "2016-05-02",
        "2016-10-10",
        "2016-12-26",
        "2017-01-02",
        "2019-05-13",
        "2020-03-02",
        "2021-06-07",
        "2022-05-02",
        "2022-05-09",
        "2022-12-26",
        "2023-01-02",
    ],
)
def test_krx_false_positive_remains_a_trading_day(date):
    days = _dates(f"{date[:4]}-01-01", f"{date[:4]}-12-31")
    assert pd.Timestamp(date) in days


def test_krx_correction_layer_is_bounded_to_2016_2026():
    for (y, m, d) in KRX_OFFICIAL_CLOSURES | KRX_FALSE_POSITIVE_CLOSURES:
        assert pd.Timestamp("2016-01-01") <= pd.Timestamp(y, m, d) < pd.Timestamp(
            "2026-07-01"
        )