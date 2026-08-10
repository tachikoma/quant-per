"""Phase 4: 현실적 비용·체결 스트레스 (캐시 전용).

각 전략(M2/K1/K3/PBR)에 대해:
  1. 비용 시나리오: 기본 / 슬리피지×2 / 슬리피지×3 / 매도비용+0.1%p
  2. 주문금액/일거래대금 비율 분포 (1%/5%/10% 초과 종목 비율)
  3. 회전율, 비용이 수익에서 차지하는 비중

결과: results/phase4_cost_stress.csv, results/phase4_order_ratio.csv
"""

import numpy as np
import pandas as pd
from pathlib import Path
from dataclasses import replace

from config import Config
from engine import run_backtest

START = "2016-01-01"
END = "2026-06-30"


def load_market_data():
    parts = []
    for f in sorted(Path(".cache/backtest/market_data").glob("*.parquet")):
        parts.append(pd.read_parquet(f))
    return pd.concat(parts, ignore_index=True)


STRATEGIES = {
    "M2": dict(
        use_katsenelson=False,
        use_multi_factor=False,
        use_momentum=True,
        momentum_window=12,
        use_low_volatility=True,
        max_turnover=0.5,
    ),
    "K1": dict(
        use_katsenelson=True,
        use_multi_factor=False,
        min_roic=0.05,
        max_ev_ebitda=15.0,
        max_turnover=0.5,
    ),
    "K3": dict(
        use_katsenelson=True,
        use_multi_factor=False,
        use_low_volatility=True,
        min_roic=0.05,
        max_ev_ebitda=15.0,
        max_turnover=0.5,
    ),
    "PBR": dict(
        use_katsenelson=False,
        use_multi_factor=True,
        use_momentum=False,
        use_low_volatility=False,
        max_turnover=0.5,
    ),
}

SCENARIOS = {
    "기본": dict(),
    "슬리피지x2": dict(slippage=0.004),
    "슬리피지x3": dict(slippage=0.006),
    "매도비용+0.1%p": dict(sell_cost=0.0033),
}


def main():
    md = load_market_data()
    base = Config.from_env()

    out = Path("results")
    out.mkdir(exist_ok=True)
    cost_rows, ratio_rows = [], []

    for strat_label, over in STRATEGIES.items():
        for scen_label, cost_over in SCENARIOS.items():
            cfg = replace(base, start_date=START, end_date=END, **over, **cost_over)
            stats = {}
            h, m = run_backtest(md, cfg, track_stats=stats)

            buy = np.array(stats.get("buy_ratios", []))
            sell = np.array(stats.get("sell_ratios", []))
            all_r = (
                np.concatenate([buy, sell]) if len(buy) or len(sell) else np.array([])
            )

            def pct_over(threshold):
                return (all_r > threshold).mean() * 100 if len(all_r) else 0.0

            # 회전율 근사: 종목 구성 변경 비율 평균
            turnover = (h["Stock_Count"].diff().abs().fillna(0)).mean()
            initial = m["INITIAL_CAPITAL"]
            final = m["FINAL_PORTFOLIO_VALUE"]
            cost_share = (
                m["TOTAL_COST_IMPACT_KRW"] / max(1, m["TOTAL_COST_IMPACT_KRW"] + final)
            ) * 100

            cost_rows.append(
                {
                    "전략": strat_label,
                    "시나리오": scen_label,
                    "CAGR(%)": m["CAGR_PCT"],
                    "누적수익(%)": m["TOTAL_RETURN_PCT"],
                    "MDD(%)": m["MAX_DRAWDOWN_PCT"],
                    "비용(만원)": round(m["TOTAL_COST_IMPACT_KRW"] / 1e4, 1),
                    "비용/원금(%)": round(
                        m["TOTAL_COST_IMPACT_KRW"] / initial * 100, 2
                    ),
                    "비용/총자산(%)": round(cost_share, 2),
                    "월평균회전(종목)": round(turnover, 2),
                    "평균매수비율": round(buy.mean(), 4) if len(buy) else None,
                    "평균매도비율": round(sell.mean(), 4) if len(sell) else None,
                }
            )
            if scen_label == "기본":
                ratio_rows.append(
                    {
                        "전략": strat_label,
                        "주문수": len(all_r),
                        "평균비율": round(all_r.mean(), 4) if len(all_r) else None,
                        "중앙값": round(np.median(all_r), 4) if len(all_r) else None,
                        "p95": round(np.percentile(all_r, 95), 4)
                        if len(all_r)
                        else None,
                        "1%초과(%)": round(pct_over(0.01), 2),
                        "5%초과(%)": round(pct_over(0.05), 2),
                        "10%초과(%)": round(pct_over(0.10), 2),
                    }
                )
            print(
                f"  {strat_label} / {scen_label}: CAGR={m['CAGR_PCT']} "
                f"누적={m['TOTAL_RETURN_PCT']} MDD={m['MAX_DRAWDOWN_PCT']} "
                f"비용={m['TOTAL_COST_IMPACT_KRW'] / 1e4:.0f}만원",
                flush=True,
            )

    df = pd.DataFrame(cost_rows)
    df.to_csv(out / "phase4_cost_stress.csv", index=False, encoding="utf-8-sig")
    rd = pd.DataFrame(ratio_rows)
    rd.to_csv(out / "phase4_order_ratio.csv", index=False, encoding="utf-8-sig")
    print(f"\n저장: {out}/phase4_cost_stress.csv, {out}/phase4_order_ratio.csv")
    print("\n주문금액/일거래대금 비율:")
    print(rd.to_string(index=False))


if __name__ == "__main__":
    main()
