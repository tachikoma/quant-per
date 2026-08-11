"""Phase 1: 재현성 고정 (캐시 전용, 네트워크 호출 없음).

- 동일 기간: 2016-01-01 ~ 2026-06-30 (market_data 캐시 유효 기간)
- 동일 비용: BUY_COST=0.00015, SELL_COST=0.0023, SLIPPAGE=0.002 (기본값)
- 5개 전략 재현: K1/K2/K3/M2/PBR
- 결과: results/phase1_reproducibility.csv
"""

from dataclasses import replace
from pathlib import Path

import pandas as pd

from config import Config
from engine import run_backtest
from report import benchmark_strategy

PERIOD = dict(start_date="2016-01-01", end_date="2026-06-30")


def load_market_data() -> pd.DataFrame:
    parts = []
    for f in sorted(Path(".cache/backtest/market_data").glob("*.parquet")):
        parts.append(pd.read_parquet(f))
    return pd.concat(parts, ignore_index=True)


def run(config: Config, label: str):
    print(f"  ▶ {label} 실행 중...", flush=True)
    history, metrics = run_backtest(market_data, config)
    history = benchmark_strategy(history, config)
    last = history.iloc[-1]
    row = {
        "전략": label,
        "기간": f"{config.start_date}~{config.end_date}",
        "CAGR(%)": metrics["CAGR_PCT"],
        "MDD(%)": metrics["MAX_DRAWDOWN_PCT"],
        "누적수익(%)": metrics["TOTAL_RETURN_PCT"],
        "KOSPI(%)": round(last.get("KOSPI_Return(%)", 0), 2),
        "Alpha(%)": round(last.get("Alpha(%)", 0), 2),
        "비용(만원)": round(metrics["TOTAL_COST_IMPACT_KRW"] / 1e4, 1),
        "최종종목수": int(last.get("Stock_Count", 0)),
    }
    print(
        f"    → CAGR {row['CAGR(%)']}% / MDD {row['MDD(%)']}% / 누적 {row['누적수익(%)']}%",
        flush=True,
    )
    return row


if __name__ == "__main__":
    market_data = load_market_data()
    print(
        f"market_data: {market_data['date'].min().date()} ~ {market_data['date'].max().date()}, "
        f"{market_data['code'].nunique()}종목\n"
    )

    base = Config.from_env()

    strategies = [
        (
            "K1: 카스넬슨 가치투자",
            replace(
                base,
                use_katsenelson=True,
                use_multi_factor=False,
                use_momentum=False,
                use_low_volatility=False,
                min_roic=0.05,
                max_ev_ebitda=15.0,
                max_turnover=0.5,
                exclude_negative_per=False,
                **PERIOD,
            ),
        ),
        (
            "K2: 카스넬슨 + 모멘텀",
            replace(
                base,
                use_katsenelson=True,
                use_multi_factor=False,
                use_momentum=True,
                momentum_window=12,
                use_low_volatility=False,
                min_roic=0.05,
                max_ev_ebitda=15.0,
                max_turnover=0.5,
                exclude_negative_per=False,
                **PERIOD,
            ),
        ),
        (
            "K3: 카스넬슨 + 저변동성",
            replace(
                base,
                use_katsenelson=True,
                use_multi_factor=False,
                use_momentum=False,
                use_low_volatility=True,
                min_roic=0.05,
                max_ev_ebitda=15.0,
                max_turnover=0.5,
                exclude_negative_per=False,
                **PERIOD,
            ),
        ),
        (
            "M2: 모멘텀 + 저변동성",
            replace(
                base,
                use_katsenelson=False,
                use_multi_factor=False,
                use_momentum=True,
                momentum_window=12,
                use_low_volatility=True,
                max_turnover=0.5,
                exclude_negative_per=False,
                **PERIOD,
            ),
        ),
        (
            "PBR: 멀티팩터",
            replace(
                base,
                use_katsenelson=False,
                use_multi_factor=True,
                use_momentum=False,
                use_low_volatility=False,
                max_turnover=0.5,
                exclude_negative_per=True,
                **PERIOD,
            ),
        ),
    ]

    results = []
    for label, config in strategies:
        try:
            results.append(run(config, label))
        except Exception as e:
            print(f"  ❌ {label} 실패: {e}")
            results.append({"전략": label, "오류": str(e)})

    df = pd.DataFrame(results)
    Path("results").mkdir(exist_ok=True)
    df.to_csv("results/phase1_reproducibility.csv", index=False, encoding="utf-8-sig")
    print("\n\n" + "=" * 70)
    print(pd.DataFrame(results).to_string(index=False))
    print("\n저장: results/phase1_reproducibility.csv")
