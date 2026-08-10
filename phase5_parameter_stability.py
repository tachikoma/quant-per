"""Phase 5: 파라미터 안정성 그리드 (캐시 전용).

과적합 판단: 최적점이 아니라 주변 값의 plateau를 확인한다.
각 파라미터를 홀로 변경했을 때 CAGR/MDD의 안정성을 기록.

결과: results/phase5_parameter_stability.csv
"""

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


def run_one(md, base, over):
    cfg = replace(base, start_date=START, end_date=END, **over)
    h, m = run_backtest(md, cfg)
    return m["CAGR_PCT"], m["MAX_DRAWDOWN_PCT"], m["TOTAL_RETURN_PCT"]


def main():
    md = load_market_data()
    base = Config.from_env()
    out = Path("results")
    out.mkdir(exist_ok=True)
    rows = []

    # 전략별 기본 설정
    M2 = dict(
        use_katsenelson=False,
        use_multi_factor=False,
        use_momentum=True,
        momentum_window=12,
        use_low_volatility=True,
        max_turnover=0.5,
    )
    K1 = dict(
        use_katsenelson=True,
        use_multi_factor=False,
        min_roic=0.05,
        max_ev_ebitda=15.0,
        max_turnover=0.5,
    )
    K3 = dict(
        use_katsenelson=True,
        use_multi_factor=False,
        use_low_volatility=True,
        min_roic=0.05,
        max_ev_ebitda=15.0,
        max_turnover=0.5,
    )
    PBR = dict(
        use_katsenelson=False,
        use_multi_factor=True,
        use_momentum=False,
        use_low_volatility=False,
        max_turnover=0.5,
    )

    # (레이블, 전략 기본설정, 파라미터명, 값 리스트)
    grids = [
        ("M2_모멘텀기간", M2, "momentum_window", [6, 9, 12, 18]),
        ("M2_종목수", M2, "n_stocks", [20, 30, 40, 50]),
        ("M2_교체율", M2, "max_turnover", [0.3, 0.5, 0.7, 1.0]),
        ("M2_MA윈도우", M2, "ma_window", [150, 200, 250]),
        ("K1_종목수", K1, "n_stocks", [20, 30, 40, 50]),
        ("K1_교체율", K1, "max_turnover", [0.3, 0.5, 0.7, 1.0]),
        ("K1_MA윈도우", K1, "ma_window", [150, 200, 250]),
        ("K3_종목수", K3, "n_stocks", [20, 30, 40, 50]),
        ("K3_교체율", K3, "max_turnover", [0.3, 0.5, 0.7, 1.0]),
        ("PBR_종목수", PBR, "n_stocks", [20, 30, 40, 50]),
        ("PBR_교체율", PBR, "max_turnover", [0.3, 0.5, 0.7, 1.0]),
        ("PBR_리밸런싱", PBR, "rebalance_freq", ["monthly", "quarterly"]),
    ]

    for label, over, param, values in grids:
        cagrs, mdds = [], []
        for v in values:
            cagr, mdd, cum = run_one(md, base, {**over, param: v})
            cagrs.append(cagr)
            mdds.append(mdd)
            rows.append(
                {
                    "그리드": label,
                    "파라미터": param,
                    "값": str(v),
                    "CAGR(%)": cagr,
                    "MDD(%)": mdd,
                    "누적수익(%)": cum,
                }
            )
            print(f"  {label} {param}={v}: CAGR={cagr} MDD={mdd}", flush=True)
        # plateau 진단: 최대-최소 CAGR (작을수록 안정)
        spread = max(cagrs) - min(cagrs)
        worst_mdd = min(mdds)
        rows.append(
            {
                "그리드": label,
                "파라미터": f"{param}_SPREAD",
                "값": "진단",
                "CAGR(%)": round(spread, 2),
                "MDD(%)": worst_mdd,
                "누적수익(%)": None,
            }
        )
        print(
            f"  → {label} CAGR 스프레드 {spread:.2f}pp / 최악 MDD {worst_mdd}",
            flush=True,
        )

    df = pd.DataFrame(rows)
    df.to_csv(out / "phase5_parameter_stability.csv", index=False, encoding="utf-8-sig")
    print(f"\n저장: {out}/phase5_parameter_stability.csv")
    diag = df[df["값"] == "진단"]
    print("\n== Plateau 진단 (CAGR 스프레드) ==")
    print(diag[["그리드", "CAGR(%)", "MDD(%)"]].to_string(index=False))


if __name__ == "__main__":
    main()
