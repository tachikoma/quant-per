"""Phase 6: OOS 검증 프로토콜 (캐시 전용).

진정한 OOS는 오늘 이후 미래 기간이므로 여기서는 실행할 수 없다.
본 스크립트는 두 가지를 제공한다:

1. 의사-OOS 진단: 비겹침 하위 기간 폴드(2~3년)에서 전략이 일관되게 작동하는지.
   (한계: 전체 2016-2026 데이터는 이미 파라미터 탐색에 사용됨 — 이 결과는
    "진단"일 뿐 진정한 OOS로 간주하지 않는다.)

2. OOS 체크포인트: BACKTEST_START/END를 미래 날짜로 지정해 실행하면
    향후 수집된 미래 데이터를 자체 OOS로 검증한다. 사후 재튜닝 금지.

사용법:
  uv run python phase6_oos.py                    # 비겹침 폴드 진단
  uv run python phase6_oos.py --checkpoint 2026-08-01 2026-12-31  # 미래 OOS
결과: results/phase6_oos_folds.csv
"""

import argparse
from dataclasses import replace
from pathlib import Path

import pandas as pd

from config import Config
from engine import run_backtest

# ── 전략·파라미터 동결 (2026-08-10 확정) ──────────────────────────────
# 이 블록을 수정한 뒤에는 이전 OOS 결과가 무효가 된다.
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

FOLDS = [
    ("2016-2019", "2016-01-01", "2019-12-31"),
    ("2019-2022", "2019-01-01", "2022-12-31"),
    ("2022-2025", "2022-01-01", "2025-06-30"),
    ("2023-2026", "2023-01-01", "2026-06-30"),
]


def load_market_data():
    parts = []
    for f in sorted(Path(".cache/backtest/market_data").glob("*.parquet")):
        parts.append(pd.read_parquet(f))
    return pd.concat(parts, ignore_index=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--checkpoint",
        nargs=2,
        metavar=("START", "END"),
        help="미래 OOS 체크포인트 실행",
    )
    args = parser.parse_args()

    md = load_market_data()
    base = Config.from_env()
    out = Path("results")
    out.mkdir(exist_ok=True)
    rows = []

    if args.checkpoint:
        start, end = args.checkpoint
        print(f"▶ OOS 체크포인트: {start} ~ {end} (미래 데이터 — 재튜닝 금지)")
        for label, over in STRATEGIES.items():
            cfg = replace(base, start_date=start, end_date=end, **over)
            h, m = run_backtest(md, cfg)
            rows.append(
                {
                    "기간": f"{start}~{end}",
                    "전략": label,
                    "CAGR(%)": m["CAGR_PCT"],
                    "MDD(%)": m["MAX_DRAWDOWN_PCT"],
                    "누적수익(%)": m["TOTAL_RETURN_PCT"],
                }
            )
            print(f"  {label}: CAGR={m['CAGR_PCT']} 누적={m['TOTAL_RETURN_PCT']}")
    else:
        print("▶ 의사-OOS 폴드 진단 (전체 기간은 파라미터 탐색에 사용됨 — 진단 전용)")
        for fold_name, start, end in FOLDS:
            for label, over in STRATEGIES.items():
                cfg = replace(base, start_date=start, end_date=end, **over)
                h, m = run_backtest(md, cfg)
                rows.append(
                    {
                        "기간": fold_name,
                        "전략": label,
                        "CAGR(%)": m["CAGR_PCT"],
                        "MDD(%)": m["MAX_DRAWDOWN_PCT"],
                        "누적수익(%)": m["TOTAL_RETURN_PCT"],
                    }
                )
                print(
                    f"  {fold_name} {label}: CAGR={m['CAGR_PCT']} "
                    f"누적={m['TOTAL_RETURN_PCT']}",
                    flush=True,
                )

    df = pd.DataFrame(rows)
    df.to_csv(out / "phase6_oos_folds.csv", index=False, encoding="utf-8-sig")
    print(f"\n저장: {out}/phase6_oos_folds.csv")

    # 폴드 진단 시 일관성 요약
    if not args.checkpoint:
        pivot = df.pivot(index="전략", columns="기간", values="CAGR(%)")
        print("\n== 폴드별 CAGR(%) ==")
        print(pivot.round(2).to_string())
        if len(pivot.columns) > 1:
            pivot = pivot.dropna(how="all", axis=1)
            spread = pivot.max(axis=1) - pivot.min(axis=1)
            print("\n== 폴드 간 CAGR 스프레드 (작을수록 일관) ==")
            print(spread.round(2).to_string())


if __name__ == "__main__":
    main()
