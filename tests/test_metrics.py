import pytest
from metrics import (
    calc_net_debt,
    calc_ebitda,
    calc_ev_ebitda,
    calc_roic,
    calc_interest_coverage,
    calc_fcf,
    calc_fcf_yield,
    calc_ncav,
    calc_debt_to_equity,
    calc_earnings_stability,
    calc_roe,
    calc_cagr_3y,
    build_financial_metrics,
)


class TestCagr3y:
    def test_normal(self):
        # 100 → 133.1 (3년, 10% CAGR)
        assert calc_cagr_3y(133.1, 100) == pytest.approx(0.10, rel=0.02)

    def test_zero_past(self):
        assert calc_cagr_3y(100, 0) is None

    def test_negative(self):
        assert calc_cagr_3y(-100, 100) is None
        assert calc_cagr_3y(100, -100) is None

    def test_none(self):
        assert calc_cagr_3y(None, 100) is None
        assert calc_cagr_3y(100, None) is None
        assert calc_cagr_3y(float("nan"), 100) is None


class TestNetDebt:
    def test_positive(self):
        assert calc_net_debt(1000, 300) == 700

    def test_negative(self):
        assert calc_net_debt(100, 500) == -400

    def test_none(self):
        assert calc_net_debt(None, 300) is None
        assert calc_net_debt(100, None) is None

    def test_nan(self):
        assert calc_net_debt(float("nan"), 300) is None


class TestEBITDA:
    def test_with_depreciation(self):
        assert calc_ebitda(100, 30) == 130

    def test_no_depreciation(self):
        assert calc_ebitda(100, None) == 100
        assert calc_ebitda(100, float("nan")) == 100

    def test_none_oi(self):
        assert calc_ebitda(None, 30) is None


class TestEVEbitda:
    def test_normal(self):
        # mcap 1000, debt 100, cash 50, OI 100, dep 20 → EV=1050, EBITDA=120 → 8.75
        assert calc_ev_ebitda(1000, 100, 50, 100, 20) == pytest.approx(8.75)

    def test_zero_ebitda(self):
        assert calc_ev_ebitda(1000, 0, 0, 0, 0) is None


class TestROIC:
    def test_normal(self):
        # OI 100, tax 25%, debt+equity 1000, cash 100 → NOPAT 75, invested 900 → 0.0833
        assert calc_roic(100, 600, 400, 100) == pytest.approx(0.08333, rel=1e-3)

    def test_no_oi(self):
        assert calc_roic(None, 600, 400, 100) is None


class TestInterestCoverage:
    def test_normal(self):
        assert calc_interest_coverage(100, 20) == 5.0

    def test_zero_interest(self):
        assert calc_interest_coverage(100, 0) is None


class TestFCF:
    def test_normal(self):
        assert calc_fcf(500, 100) == 400

    def test_no_capex(self):
        assert calc_fcf(500, None) == 500

    def test_nan_ocf(self):
        assert calc_fcf(float("nan"), 100) is None

    def test_yield(self):
        assert calc_fcf_yield(400, 10000) == pytest.approx(0.04)


class TestNCAV:
    def test_normal(self):
        assert calc_ncav(1000, 600) == 400

    def test_missing(self):
        assert calc_ncav(None, 600) is None


class TestDebtToEquity:
    def test_normal(self):
        assert calc_debt_to_equity(500, 1000) == 0.5

    def test_zero_equity(self):
        assert calc_debt_to_equity(500, 0) is None


class TestEarningsStability:
    def test_stable(self):
        s = calc_earnings_stability([100, 101, 99, 100, 100])
        assert s is not None and s > 10

    def test_volatile(self):
        s = calc_earnings_stability([100, -50, 30, 200, 10])
        assert s is not None and s < 1

    def test_insufficient(self):
        assert calc_earnings_stability([100]) is None
        assert calc_earnings_stability([]) is None


class TestROE:
    def test_normal(self):
        assert calc_roe(100, 1000) == 0.1


class TestBuildMetrics:
    def test_complete_row(self):
        fin = {
            "cash": 100,
            "current_assets": 500,
            "total_liabilities": 400,
            "total_equity": 600,
            "borrowings": 100,
            "revenue": 1000,
            "revenue_3y_ago": 800,
            "operating_income": 200,
            "operating_income_3y_ago": 150,
            "net_income": 120,
            "net_income_3y_ago": 90,
            "interest_paid": 10,
            "depreciation": 50,
            "operating_cf": 300,
            "investing_cf": -150,
            "capex": 80,
        }
        m = build_financial_metrics(fin, market_cap=2000)
        assert m["roe"] == pytest.approx(0.2)
        assert m["fcf"] == pytest.approx(220)
        assert m["fcf_yield"] == pytest.approx(0.11)
        # (1000/800)^(1/3)-1 ≈ 7.7%
        assert m["revenue_cagr_3y"] == pytest.approx((1000 / 800) ** (1 / 3) - 1)
        assert m["oi_cagr_3y"] == pytest.approx((200 / 150) ** (1 / 3) - 1)
        assert m["ncav"] == pytest.approx(100)
        assert m["debt_to_equity"] == pytest.approx(100 / 600)
        assert m["interest_coverage"] == pytest.approx(20.0)
        assert m["ev_ebitda"] == pytest.approx((2000 + (100 - 100)) / 250)

    def test_empty_row(self):
        m = build_financial_metrics({}, market_cap=1000)
        for v in m.values():
            assert v is None
