import pandas as pd
import pytest
from dart_data import (
    RPT_Q1,
    RPT_SEMI,
    RPT_Q3,
    available_from,
    merge_dart_financials,
)


class TestAvailableFrom:
    def test_annual(self):
        """연간 보고서(결산 12월)는 다음해 4월 15일부터 사용 가능."""
        assert available_from(2023) == pd.Timestamp("2024-04-15")

    def test_q1(self):
        """1분기(3월 결산) 보고서는 6월 15일부터 사용 가능."""
        assert available_from(2023, RPT_Q1) == pd.Timestamp("2024-06-15")

    def test_semi(self):
        assert available_from(2023, RPT_SEMI) == pd.Timestamp("2024-09-15")

    def test_q3(self):
        assert available_from(2023, RPT_Q3) == pd.Timestamp("2024-12-15")


class TestMergeDartFinancials:
    def test_empty_input(self):
        out = merge_dart_financials(pd.DataFrame())
        assert out.empty

    def test_no_cache(self, tmp_path):
        """DART 캐시가 없으면 원본 그대로 반환."""
        market = pd.DataFrame(
            [{"date": pd.Timestamp("2024-01-02"), "code": "005930", "close": 70000}]
        )
        out = merge_dart_financials(market, cache_dir=tmp_path)
        assert list(out.columns) == list(market.columns)
        assert len(out) == 1

    def test_lookahead_prevention(self, tmp_path):
        """공시 사용 가능 시점 이전에는 해당 연도 재무제표를 사용하지 않는다."""
        # 가짜 DART 캐시 생성: corp 00126380 (삼성전자), 2022/2023 연간
        cache_dir = tmp_path / ".cache" / "backtest"
        dart_dir = cache_dir / "dart_data"
        dart_dir.mkdir(parents=True, exist_ok=True)

        fin = pd.DataFrame(
            [
                {
                    "year": 2022,
                    "operating_income": 100_000_000,
                    "cash": 50_000_000,
                    "total_liabilities": 200_000_000,
                    "total_equity": 300_000_000,
                    "borrowings": 40_000_000,
                    "current_assets": 250_000_000,
                    "net_income": 30_000_000,
                    "operating_cf": 80_000_000,
                    "capex": 20_000_000,
                    "interest_paid": 5_000_000,
                    "depreciation": 10_000_000,
                },
                {
                    "year": 2023,
                    "operating_income": 200_000_000,
                    "cash": 60_000_000,
                    "total_liabilities": 220_000_000,
                    "total_equity": 380_000_000,
                    "borrowings": 50_000_000,
                    "current_assets": 300_000_000,
                    "net_income": 60_000_000,
                    "operating_cf": 150_000_000,
                    "capex": 30_000_000,
                    "interest_paid": 5_000_000,
                    "depreciation": 15_000_000,
                },
            ]
        )
        fin.to_parquet(dart_dir / "00126380.parquet", index=False)

        # corp_code 매핑 캐시
        codes = pd.DataFrame(
            [
                {"corp_code": "00126380", "ticker": "005930", "corp_name": "삼성전자"},
                {"corp_code": "99999999", "ticker": "000660", "corp_name": "다른종목"},
            ]
        )
        codes.to_parquet(cache_dir / "dart_corp_codes.parquet", index=False)

        market = pd.DataFrame(
            [
                # 2024-03-01: 2023년 보고서(2024-04-15 공시) 아직 사용 불가 → 2022년 사용
                {
                    "date": pd.Timestamp("2024-03-01"),
                    "code": "005930",
                    "close": 70000,
                    "market_cap": 1_000_000_000,
                    "trading_val": 10_000_000_000,
                    "per": 10,
                    "is_preferred": False,
                },
                # 2024-05-01: 2023년 보고서 사용 가능 → 2023년 사용
                {
                    "date": pd.Timestamp("2024-05-01"),
                    "code": "005930",
                    "close": 70000,
                    "market_cap": 1_000_000_000,
                    "trading_val": 10_000_000_000,
                    "per": 10,
                    "is_preferred": False,
                },
            ]
        )

        out = merge_dart_financials(market, cache_dir=cache_dir)
        out = out.sort_values("date").reset_index(drop=True)

        # 2024-03-01 → 2022년 보고서 (운영이익 100M)
        assert out.loc[0, "operating_income"] == pytest.approx(100_000_000)
        # 2024-05-01 → 2023년 보고서 (운영이익 200M)
        assert out.loc[1, "operating_income"] == pytest.approx(200_000_000)

    def test_ticker_without_financials(self, tmp_path):
        """재무제표가 없는 티커는 NaN으로 병합 (컬럼은 추가됨)."""
        cache_dir = tmp_path / ".cache" / "backtest"
        dart_dir = cache_dir / "dart_data"
        dart_dir.mkdir(parents=True, exist_ok=True)

        # 매핑만 있고 재무제표 파일이 없는 종목
        codes = pd.DataFrame(
            [
                {"corp_code": "00126380", "ticker": "005930", "corp_name": "삼성전자"},
            ]
        )
        codes.to_parquet(cache_dir / "dart_corp_codes.parquet", index=False)
        # 다른 종목(000660)의 재무제표만 존재 → 매핑에는 없지만 fin 로드됨
        fin = pd.DataFrame(
            [
                {
                    "year": 2023,
                    "operating_income": 100_000_000,
                    "cash": 50_000_000,
                    "total_liabilities": 200_000_000,
                    "total_equity": 300_000_000,
                    "borrowings": 40_000_000,
                    "current_assets": 250_000_000,
                    "net_income": 30_000_000,
                    "operating_cf": 80_000_000,
                    "capex": 20_000_000,
                    "interest_paid": 5_000_000,
                    "depreciation": 10_000_000,
                },
            ]
        )
        fin.to_parquet(dart_dir / "00126380.parquet", index=False)

        market = pd.DataFrame(
            [
                {
                    "date": pd.Timestamp("2024-03-01"),
                    "code": "000000",
                    "close": 70000,
                    "market_cap": 1_000_000_000,
                    "trading_val": 10_000_000_000,
                    "per": 10,
                    "is_preferred": False,
                },
            ]
        )
        out = merge_dart_financials(market, cache_dir=cache_dir)
        assert "operating_income" in out.columns
        assert out["operating_income"].isna().all()
