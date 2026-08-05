"""카스넬슨 '적극적 가치투자' 재무 지표 계산 모듈.

계정 금액은 DART 원 단위 기준. 계산 함수는 순수 함수로 합성 데이터로 테스트 가능.
"""

import math

import pandas as pd


def _is_bad(val):
    """None이거나 NaN이면 True."""
    if val is None:
        return True
    try:
        return math.isnan(float(val))
    except (TypeError, ValueError):
        return True


def safe_div(numerator, denominator):
    """0으로 나누기 방지. 음수/None 처리."""
    if _is_bad(denominator) or _is_bad(numerator):
        return None
    try:
        d = float(denominator)
        if d == 0:
            return None
        n = float(numerator)
        return n / d
    except (TypeError, ValueError):
        return None


def calc_net_debt(borrowings, cash) -> float | None:
    """순차입금 = 총차입금 - 현금."""
    if _is_bad(borrowings) or _is_bad(cash):
        return None
    return borrowings - cash


def calc_ebitda(operating_income, depreciation) -> float | None:
    """EBITDA = 영업이익 + 감가상각비 (+ 무형자산상각비)."""
    if _is_bad(operating_income):
        return None
    if _is_bad(depreciation):
        return operating_income
    return operating_income + depreciation


def calc_ev_ebitda(
    market_cap, borrowings, cash, operating_income, depreciation
) -> float | None:
    """EV/EBITDA = (시가총액 + 순차입금) / EBITDA."""
    ebitda = calc_ebitda(operating_income, depreciation)
    if ebitda is None or ebitda == 0:
        return None
    nd = calc_net_debt(borrowings, cash)
    if nd is None:
        ev = market_cap
    else:
        ev = market_cap + nd
    return ev / ebitda


def calc_roic(
    operating_income, total_liabilities, total_equity, cash, tax_rate=0.25
) -> float | None:
    """ROIC = NOPAT / 투하자본.

    NOPAT = 영업이익 × (1 - 세율)
    Invested Capital = (부채총계 + 자본총계) - 현금 (단순 근사)
    """
    if _is_bad(operating_income):
        return None
    nopat = operating_income * (1 - tax_rate)
    invested = None
    if total_liabilities is not None and total_equity is not None:
        if not _is_bad(total_liabilities) and not _is_bad(total_equity):
            invested = total_liabilities + total_equity
            if not _is_bad(cash):
                invested -= cash
    if invested is None or invested == 0:
        return None
    return nopat / invested


def calc_interest_coverage(operating_income, interest_paid) -> float | None:
    """이자보상배율 = 영업이익 / 이자비용(지급이자)."""
    return safe_div(operating_income, interest_paid)


def calc_fcf(operating_cf, capex) -> float | None:
    """FCF = 영업현금흐름 - 자본적지출."""
    if _is_bad(operating_cf):
        return None
    if _is_bad(capex):
        return operating_cf
    return operating_cf - capex


def calc_fcf_yield(fcf, market_cap) -> float | None:
    """FCF Yield = FCF / 시가총액."""
    return safe_div(fcf, market_cap)


def calc_ncav(current_assets, total_liabilities) -> float | None:
    """NCAV = 유동자산 - 부채총계 (net-net)."""
    if _is_bad(current_assets) or _is_bad(total_liabilities):
        return None
    return current_assets - total_liabilities


def calc_debt_to_equity(borrowings, total_equity) -> float | None:
    """부채비율(차입금 기준) = 총차입금 / 자본총계."""
    return safe_div(borrowings, total_equity)


def calc_earnings_stability(operating_incomes: list) -> float | None:
    """이익 안정성 = 평균 / 표준편차 (변동계수 역수).

    값이 높을수록 안정적. 1개 이하면 None.
    """
    vals = [v for v in operating_incomes if not _is_bad(v) and v != 0]
    if len(vals) < 2:
        return None
    s = pd.Series(vals)
    mean = s.mean()
    std = s.std(ddof=0)
    if std == 0:
        return float("inf")
    return float(mean / abs(std))


def calc_roe(net_income, total_equity) -> float | None:
    """ROE = 순이익 / 자본총계."""
    return safe_div(net_income, total_equity)


def build_financial_metrics(
    fin_row: dict, market_cap: float, tax_rate: float = 0.25
) -> dict:
    """DART flat 재무제표 행 → 카스넬슨 지표 dict."""
    oi = fin_row.get("operating_income")
    dep = fin_row.get("depreciation")
    b = fin_row.get("borrowings")
    cash = fin_row.get("cash")
    tl = fin_row.get("total_liabilities")
    te = fin_row.get("total_equity")
    oc = fin_row.get("operating_cf")
    cx = fin_row.get("capex")
    ca = fin_row.get("current_assets")
    ni = fin_row.get("net_income")
    ip = fin_row.get("interest_paid")

    return {
        "roe": calc_roe(ni, te),
        "roic": calc_roic(oi, tl, te, cash, tax_rate),
        "debt_to_equity": calc_debt_to_equity(b, te),
        "interest_coverage": calc_interest_coverage(oi, ip),
        "fcf": calc_fcf(oc, cx),
        "fcf_yield": calc_fcf_yield(calc_fcf(oc, cx), market_cap),
        "ev_ebitda": calc_ev_ebitda(market_cap, b, cash, oi, dep),
        "ncav": calc_ncav(ca, tl),
        "ncav_ratio": safe_div(calc_ncav(ca, tl), market_cap),
    }
