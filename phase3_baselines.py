"""Phase 3: 기준선 및 MA200 분해.

캐시 전용 (pykrx 재다운로드 금지). 같은 기간·같은 유니버스로:
  1. 동일 유니버스 동일가중  (종목선정 알파 0 기준선)
  2. 저변동성 단독 / 모멘텀 단독 (팩터 기여 분해)
  3. M2 / K3 / PBR (복합 전략)
  4. 위 전략 전부 MA200 off vs on (레짐 필터 기여 분해)

결과: results/phase3_baselines.csv, results/phase3_ma200_decomposition.csv
"""

import pandas as pd
from pathlib import Path
from dataclasses import replace

from config import Config
from engine import run_backtest
from report import benchmark_strategy

START = "2016-01-01"
END = "2026-06-30"


def load_market_data():
    parts = []
    for f in sorted(Path(".cache/backtest/market_data").glob("*.parquet")):
        parts.append(pd.read_parquet(f))
    md = pd.concat(parts, ignore_index=True)
    md["year_month"] = md["date"].dt.to_period("M")
    return md


def equal_weight_baseline(md, config: Config):
    """유니버스 전체를 매월 동일가중으로 리밸런싱하는 기준선.

    엔진과 동일 컨벤션: 월 첫 거래일 종가로 매수, 같은 달 마지막 거래일 종가로 MTM.
    다음 달 첫 거래일에 전 종목 매도 후 재편성 (전체 교체율 100%).
    """
    initial_capital = config.initial_capital
    buy_cost, sell_cost, slippage = config.buy_cost, config.sell_cost, config.slippage
    friction = sell_cost + buy_cost + 2 * slippage

    cash = float(initial_capital)
    current_portfolio = []
    history = []
    first_first_day = None
    monthly_close = {}
    unique_months = sorted(md["year_month"].unique())
    for ym in unique_months:
        ym_df = md[md["year_month"] == ym]
        first_date = sorted(ym_df["date"].unique())[0]
        monthly_close[ym] = ym_df[ym_df["date"] == first_date].set_index("code")[
            "close"
        ]

    for i, current_month in enumerate(unique_months):
        month_df = md[md["year_month"] == current_month]
        if month_df.empty:
            continue
        trading_days = sorted(month_df["date"].unique())
        if len(trading_days) < 2:
            continue
        first_day, last_day = trading_days[0], trading_days[-1]
        if first_first_day is None:
            first_first_day = first_day

        last_day_df = month_df[month_df["date"] == last_day].set_index("code")

        # 보유 포트폴리오 (이전 달 첫 거래일에 매수한 것)를 이번 달 마지막 거래일로 MTM
        def market_value(assets, ref_df):
            total = 0.0
            for a in assets:
                if a["code"] in ref_df.index:
                    total += a["shares"] * ref_df.loc[a["code"], "close"]
                else:
                    total += a["shares"] * a["buy_price"] * 0.1
            return total

        portfolio_value = cash + market_value(current_portfolio, last_day_df)

        total_ret = (portfolio_value / initial_capital - 1) * 100
        history.append(
            {
                "Date": str(last_day.date()),
                "Portfolio_Value": int(portfolio_value),
                "Total_Return(%)": round(total_ret, 2),
                "Stock_Count": len(current_portfolio),
            }
        )

        # 다음 달 첫 거래일 진입 준비: 폐쇄된 포트폴리오 매도 → 재편성
        # (이로써 현재 달은 보유만 하고, 다음 달 초에 신규 편성)
        if i + 1 >= len(unique_months):
            continue
        next_ym = unique_months[i + 1]
        next_month_df = md[md["year_month"] == next_ym]
        if next_month_df.empty:
            continue
        next_days = sorted(next_month_df["date"].unique())
        if len(next_days) < 2:
            continue

        next_first_df = next_month_df[next_month_df["date"] == next_days[0]]
        universe = next_first_df[~next_first_df["is_preferred"]]
        if config.max_market_cap > 0:
            universe = universe[universe["market_cap"] <= config.max_market_cap]
        universe = universe[
            (universe["market_cap"] >= config.min_market_cap)
            & (universe["trading_val"] >= config.min_trading_val)
        ]
        codes = sorted(universe["code"].unique())
        fd_close = monthly_close.get(next_ym)
        if not codes or fd_close is None:
            current_portfolio = []
            cash = portfolio_value
            continue

        investable = [(c, fd_close.get(c)) for c in codes]
        investable = [
            (c, px)
            for c, px in investable
            if px is not None and not pd.isna(px) and px > 0
        ]
        if not investable:
            current_portfolio = []
            cash = portfolio_value
            continue

        cash = portfolio_value * friction
        per_stock = (portfolio_value - cash) / len(investable)
        current_portfolio = [
            {"code": c, "shares": per_stock / px, "buy_price": px}
            for c, px in investable
        ]

    history.insert(
        0,
        {
            "Date": str(first_first_day.date()),
            "Portfolio_Value": initial_capital,
            "Total_Return(%)": 0.0,
            "Stock_Count": 0,
        },
    )
    h = pd.DataFrame(history)
    h["Peak"] = h["Portfolio_Value"].cummax()
    h["Drawdown"] = (h["Portfolio_Value"] - h["Peak"]) / h["Peak"] * 100
    first_date = pd.Timestamp(h["Date"].iloc[0])
    last_date = pd.Timestamp(h["Date"].iloc[-1])
    years = (last_date - first_date).days / 365.25
    final = int(h["Portfolio_Value"].iloc[-1])
    cagr = ((final / initial_capital) ** (1 / years) - 1) * 100
    metrics = {
        "CAGR_PCT": round(cagr, 2),
        "MAX_DRAWDOWN_PCT": round(h["Drawdown"].min(), 2),
        "TOTAL_RETURN_PCT": h["Total_Return(%)"].iloc[-1],
        "TOTAL_COST_IMPACT_KRW": 0,
    }
    return h, metrics


def extended_metrics(h: pd.DataFrame):
    """Sharpe / Sortino / Calmar."""
    pv = h["Portfolio_Value"].astype(float)
    rets = pv.pct_change().dropna()
    n = len(rets)
    if n == 0:
        return {"Sharpe": None, "Sortino": None, "Calmar": None, "연환산_변동성": None}
    ann_factor = 12
    mu = rets.mean() * ann_factor
    sd = rets.std() * (ann_factor**0.5)
    downside = rets[rets < 0].std() * (ann_factor**0.5)
    sharpe = mu / sd if sd > 0 else None
    sortino = mu / downside if downside and downside > 0 else None
    cagr = (pv.iloc[-1] / pv.iloc[0]) ** (1 / (n / 12)) - 1
    mdd = abs(h["Drawdown"].min() / 100)
    calmar = cagr / mdd if mdd > 0 else None
    return {
        "Sharpe": round(sharpe, 2) if sharpe is not None else None,
        "Sortino": round(sortino, 2) if sortino is not None else None,
        "Calmar": round(calmar, 2) if calmar is not None else None,
        "연환산_변동성(%)": round(sd * 100, 2),
    }


def annual_returns(h: pd.DataFrame):
    h = h.copy()
    h["Date"] = pd.to_datetime(h["Date"])
    h["Year"] = h["Date"].dt.year
    h["Yr_ret(%)"] = h["Portfolio_Value"].pct_change().fillna(0) * 100
    year_first = h.groupby("Year")["Portfolio_Value"].first()
    year_last = h.groupby("Year")["Portfolio_Value"].last()
    return ((year_last / year_first.shift(1)) - 1).dropna() * 100


def main():
    md = load_market_data()
    base = Config.from_env()
    rows = []

    variants = [
        ("동일유니버스동일가중", None),
        (
            "저변동성_단독",
            dict(
                use_katsenelson=False,
                use_multi_factor=False,
                use_momentum=False,
                use_low_volatility=True,
                max_turnover=0.5,
                exclude_negative_per=False,
            ),
        ),
        (
            "모멘텀_단독",
            dict(
                use_katsenelson=False,
                use_multi_factor=False,
                use_momentum=True,
                momentum_window=12,
                use_low_volatility=False,
                max_turnover=0.5,
                exclude_negative_per=False,
            ),
        ),
        (
            "M2_모멘텀저변동",
            dict(
                use_katsenelson=False,
                use_multi_factor=False,
                use_momentum=True,
                momentum_window=12,
                use_low_volatility=True,
                max_turnover=0.5,
                exclude_negative_per=False,
            ),
        ),
        (
            "K1_카스넬슨",
            dict(
                use_katsenelson=True,
                use_multi_factor=False,
                use_momentum=False,
                use_low_volatility=False,
                min_roic=0.05,
                max_ev_ebitda=15.0,
                exclude_negative_per=False,
            ),
        ),
        (
            "K3_카스넬슨저변동",
            dict(
                use_katsenelson=True,
                use_multi_factor=False,
                use_momentum=False,
                use_low_volatility=True,
                min_roic=0.05,
                max_ev_ebitda=15.0,
                exclude_negative_per=False,
            ),
        ),
        (
            "K2_카스넬슨모멘텀",
            dict(
                use_katsenelson=True,
                use_multi_factor=False,
                use_momentum=True,
                momentum_window=12,
                use_low_volatility=False,
                min_roic=0.05,
                max_ev_ebitda=15.0,
                exclude_negative_per=False,
            ),
        ),
        (
            "PBR_멀티팩터",
            dict(
                use_katsenelson=False,
                use_multi_factor=True,
                use_momentum=False,
                use_low_volatility=False,
                max_turnover=0.5,
                exclude_negative_per=True,
            ),
        ),
    ]

    katsenelson_over = dict(
        use_katsenelson=True,
        use_multi_factor=False,
        min_roic=0.05,
        max_ev_ebitda=15.0,
        max_turnover=0.5,
        exclude_negative_per=False,
    )

    for label, overrides in variants:
        if label == "동일유니버스동일가중":
            cfg = replace(
                base, start_date=START, end_date=END, max_market_cap=1_000_000_000_000
            )
            h, m = equal_weight_baseline(md, cfg)
        else:
            cfg = replace(base, start_date=START, end_date=END, **overrides)
            h, m = run_backtest(md, cfg)
            h = benchmark_strategy(h, cfg)
        ext = extended_metrics(h)
        ar = annual_returns(h)
        row = {
            "전략": label,
            "CAGR(%)": m["CAGR_PCT"],
            "누적수익(%)": m["TOTAL_RETURN_PCT"],
            "MDD(%)": m["MAX_DRAWDOWN_PCT"],
            "비용(만원)": round(m["TOTAL_COST_IMPACT_KRW"] / 1e4, 1),
            "최종종목수": int(h["Stock_Count"].iloc[-1]),
            **ext,
        }
        for yr, r in ar.items():
            row[f"연도_{yr}"] = round(r, 2)
        rows.append(row)
        print(
            f"  {label:18s}: CAGR={m['CAGR_PCT']} MDD={m['MAX_DRAWDOWN_PCT']} "
            f"누적={m['TOTAL_RETURN_PCT']} Sharpe={ext['Sharpe']}",
            flush=True,
        )

    df = pd.DataFrame(rows)
    out = Path("results")
    out.mkdir(exist_ok=True)
    df.to_csv(out / "phase3_baselines.csv", index=False, encoding="utf-8-sig")

    # ── MA200 on/off 분해 ──
    print("\n== MA200 레짐 on/off 분해 ==")
    ma_rows = []
    for label, base_over in [
        (
            "M2",
            dict(
                use_katsenelson=False,
                use_multi_factor=False,
                use_momentum=True,
                momentum_window=12,
                use_low_volatility=True,
                max_turnover=0.5,
                exclude_negative_per=False,
            ),
        ),
        ("K1", katsenelson_over),
        ("K3", {**katsenelson_over, "use_low_volatility": True}),
        (
            "PBR",
            dict(
                use_katsenelson=False,
                use_multi_factor=True,
                max_turnover=0.5,
                exclude_negative_per=True,
            ),
        ),
    ]:
        for regime in [True, False]:
            cfg = replace(
                base,
                start_date=START,
                end_date=END,
                use_market_regime=regime,
                **base_over,
            )
            h, m = run_backtest(md, cfg)
            h = benchmark_strategy(h, cfg)
            ext = extended_metrics(h)
            exposure = (h["Stock_Count"] > 0).mean() * 100
            ma_rows.append(
                {
                    "전략": label,
                    "MA200": "on" if regime else "off",
                    "CAGR(%)": m["CAGR_PCT"],
                    "누적수익(%)": m["TOTAL_RETURN_PCT"],
                    "MDD(%)": m["MAX_DRAWDOWN_PCT"],
                    "Sharpe": ext["Sharpe"],
                    "Sortino": ext["Sortino"],
                    "시장노출기간(%)": round(exposure, 1),
                }
            )
            print(
                f"  {label} MA200={'on' if regime else 'off'}: "
                f"CAGR={m['CAGR_PCT']} 누적={m['TOTAL_RETURN_PCT']} "
                f"MDD={m['MAX_DRAWDOWN_PCT']} Sharpe={ext['Sharpe']}",
                flush=True,
            )
    ma_df = pd.DataFrame(ma_rows)
    ma_df.to_csv(
        out / "phase3_ma200_decomposition.csv", index=False, encoding="utf-8-sig"
    )
    print(f"\n저장: {out}/phase3_baselines.csv, {out}/phase3_ma200_decomposition.csv")


if __name__ == "__main__":
    main()
