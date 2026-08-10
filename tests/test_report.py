import pandas as pd

from report import align_to_dates


class TestAlignToDates:
    """벤치마크 정렬이 미래 값을 사용하지 않는지 검증."""

    def _kospi(self):
        df = pd.DataFrame(
            {"kospi_close": [100.0, 101.0, 102.0]},
            index=pd.to_datetime(["2020-01-02", "2020-01-06", "2020-01-07"]),
        )
        df.index.name = "date"
        return df

    def test_exact_date_uses_same_value(self):
        kospi = self._kospi()
        out = align_to_dates(kospi, ["2020-01-06"])
        assert out.iloc[0]["kospi_close"] == 101.0

    def test_missing_date_uses_last_previous_value(self):
        """평가일이 KOSPI에 없으면 직전 값(과거)을 사용. 미래 값 금지."""
        kospi = self._kospi()
        # 2020-01-05는 KOSPI에 없음 → 2020-01-02(100.0) 사용
        out = align_to_dates(kospi, ["2020-01-05"])
        assert out.iloc[0]["kospi_close"] == 100.0

    def test_future_date_not_used(self):
        """평가일 이전 값이 없으면 None (미래 값을 끌어오지 않음)."""
        kospi = self._kospi()
        out = align_to_dates(kospi, ["2019-12-31"])
        assert pd.isna(out.iloc[0]["kospi_close"])
