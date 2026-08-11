import sys
import argparse
from dataclasses import replace
from config import Config
from engine import fetch_rebalancing_data, run_backtest, clear_cache
from report import benchmark_strategy, print_report


def run_single(config: Config, label: str, cache_dir=None):
    print("\n" + "=" * 70)
    print(f"  실험: {label}")
    print("=" * 70)
    print(
        f"  시총 {config.min_market_cap // 1e8:.0f}억~{config.max_market_cap // 1e8:.0f}억, 거래대금≥{config.min_trading_val // 1e8:.0f}억, PBR 하위 {config.pbr_pctile:.0%}, {config.rebalance_freq}, {config.n_stocks}종목"
    )
    if config.use_momentum:
        print(f"  모멘텀: {config.momentum_window}개월")
    if config.use_low_volatility:
        print("  저변동성 포함")
    if config.use_multi_factor:
        print(
            f"  멀티팩터: PBR 하위 {config.pbr_pctile:.0%}, PER+ROE+배당 스코어링 | max_turnover={config.max_turnover}"
        )
    if config.fundamental_lag_months > 0:
        print(f"  펀더멘털 시차: {config.fundamental_lag_months}개월 lag")
    print()

    data = fetch_rebalancing_data(
        config.start_date,
        config.end_date,
        cache_dir=cache_dir,
        force_refresh=False,
        lag_months=config.fundamental_lag_months,
    )

    history, metrics = run_backtest(data, config, cache_dir=cache_dir)

    history = benchmark_strategy(
        history, config, cache_dir=cache_dir, force_refresh=False
    )

    print_report(history, metrics, config)
    return history, metrics


def parse_args():
    parser = argparse.ArgumentParser(description="백테스트 실험 배치 실행")
    parser.add_argument("--cache-dir", default=None)
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="캐시 사용하지 않고 전체 데이터 다시 다운로드",
    )
    parser.add_argument("--clear-cache", action="store_true", help="캐시 전체 삭제")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.clear_cache:
        clear_cache(args.cache_dir)
        sys.exit(0)

    base = Config.from_env()

    # 실험 결과 요약
    # ──────────────────────────────────────────────────
    # D: PER 0~4 + MF     + 4m lag → CAGR -8.87% (bias 파괴적)
    # P1: PER 0~12 + MF   + 4m lag → CAGR -2.39%
    # P3: MF-only + 4m lag (PER 0~99) → CAGR -1.73%
    # M1: 모멘텀 단독                → CAGR +3.52% (bias 0)
    # M2: 모멘텀 + 저변동성          → CAGR +8.02% ⭐ (bias 0)
    # M3: 모멘텀 + Quality(2m lag)   → CAGR +0.63%
    # M4: 모멘텀 + 저변동성 + Q(2m)  → CAGR +2.45%
    # ──────────────────────────────────────────────────
    # 최적: M2 (모멘텀+저변동성) — bias-free, 30종목 풀채움
    # 대안: PER+MF — CAGR 10%지만 bias 리스크 있음
    #
    # 카스넬슨 (DART 재무제표 기반, 공시일 lag 자동 적용):
    # K1~K4는 먼저 `uv run python collect_dart_data.py`로 재무제표 수집 필요

    start_kwargs = dict(start_date="2008-01-01", end_date="2026-06-26")

    experiments = [
        # M2: 최적 — 모멘텀 + 저변동성 (bias-free, CAGR 8.02%)
        {
            "label": "M2: 모멘텀 + 저변동성",
            "config": replace(
                base,
                use_multi_factor=False,
                use_momentum=True,
                momentum_window=12,
                use_low_volatility=True,
                max_turnover=0.5,
                exclude_negative_per=False,
                **start_kwargs,
            ),
        },
        # M1: 12-month momentum only (bias-free)
        {
            "label": "M1: 12m 모멘텀 단독",
            "config": replace(
                base,
                use_multi_factor=False,
                use_momentum=True,
                momentum_window=12,
                use_low_volatility=False,
                max_turnover=0.5,
                exclude_negative_per=False,
                **start_kwargs,
            ),
        },
        # K1: 카스넬슨 가치투자 기본 (ROIC+FCF+EV/EBITDA 품질+가치)
        {
            "label": "K1: 카스넬슨 가치투자",
            "config": replace(
                base,
                use_katsenelson=True,
                use_multi_factor=False,
                use_momentum=False,
                use_low_volatility=False,
                min_roic=0.05,
                max_ev_ebitda=15.0,
                min_interest_coverage=2.0,
                max_turnover=0.5,
                exclude_negative_per=False,
                **start_kwargs,
            ),
        },
        # K2: 카스넬슨 + 모멘텀 결합
        {
            "label": "K2: 카스넬슨 + 모멘텀",
            "config": replace(
                base,
                use_katsenelson=True,
                use_multi_factor=False,
                use_momentum=True,
                momentum_window=12,
                use_low_volatility=False,
                min_roic=0.05,
                max_ev_ebitda=15.0,
                min_interest_coverage=2.0,
                max_turnover=0.5,
                exclude_negative_per=False,
                **start_kwargs,
            ),
        },
        # K3: 카스넬슨 + 저변동성
        {
            "label": "K3: 카스넬슨 + 저변동성",
            "config": replace(
                base,
                use_katsenelson=True,
                use_multi_factor=False,
                use_momentum=False,
                use_low_volatility=True,
                min_roic=0.05,
                max_ev_ebitda=15.0,
                min_interest_coverage=2.0,
                max_turnover=0.5,
                exclude_negative_per=False,
                **start_kwargs,
            ),
        },
        # M3: momentum + quality (2m lag)
        {
            "label": "M3: 모멘텀 + Quality (2m lag)",
            "config": replace(
                base,
                use_multi_factor=True,
                use_momentum=True,
                momentum_window=12,
                use_low_volatility=False,
                fundamental_lag_months=2,
                max_turnover=0.5,
                exclude_negative_per=False,
                **start_kwargs,
            ),
        },
        # M4: momentum + low vol + quality (2m lag)
        {
            "label": "M4: 모멘텀 + 저변동성 + Quality (2m lag)",
            "config": replace(
                base,
                use_multi_factor=True,
                use_momentum=True,
                momentum_window=12,
                use_low_volatility=True,
                fundamental_lag_months=2,
                max_turnover=0.5,
                exclude_negative_per=False,
                **start_kwargs,
            ),
        },
    ]

    for exp in experiments:
        run_single(exp["config"], exp["label"], cache_dir=args.cache_dir)

    print("\n\n✅ 모든 실험 완료!")
