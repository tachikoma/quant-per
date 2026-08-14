import pandas as pd
import pytest

from config import get_korean_business_days


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