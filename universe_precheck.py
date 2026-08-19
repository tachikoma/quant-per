"""유니버스 확대 선행 검증 (Pre-check).

캐시 전용, pykrx 재다운로드 없음.
4개 유니버스 설정 x PBR 전략 x 단일 기간(2016-01~2026-06).
"go/no-go 판단용" -- 정식 Phase 1~7은 별도.

Usage:
    uv run python universe_precheck.py
    uv run python universe_precheck.py --configs A B C   # D 제외
    uv run python universe_precheck.py --end 2026-08-18  # 기간 확장
"""

import argparse
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

from config import Config
from engine import run_backtest
from report import benchmark_strategy


# ---------------------------------------------------------------------------
# 유니버스 설정 정의 (Oracle 검토 반영, 파라미터 동결)
# ---------------------------------------------------------------------------
UNIVERSE_CONFIGS = {
    "A": {
        "label": "기본 (20B~1T, K+K)",
        "min_market_cap": 20_000_000_000,
        "max_market_cap": 1_000_000_000_000,
        "note": "현재 상태 (베이스라인)",
    },
    "B": {
        "label": "확대 (500B~10T, K+K)",
        "min_market_cap": 500_000_000_000,
        "max_market_cap": 10_000_000_000_000,
        "note": "하한 raised + 천장 확대",
    },
    "C": {
        "label": "대형주 (1T~, K+K)",
        "min_market_cap": 1_000_000_000_000,
        "max_market_cap": 0,
        "note": "대형주 전용",
    },
    "D": {
        "label": "KOSPI only (500B~)",
        "min_market_cap": 500_000_000_000,
        "max_market_cap": 0,
        "note": "KOSPI only -- v1 캐시로 market 필터 불가, v2 수집 필요",
        "requires_v2": True,
    },
}


def load_market_data(cache_dir=None):
    """v1 market_data 캐시 전부 로드 (네트워크 호출 없음)."""
    cache = Path(cache_dir) if cache_dir else Path(".cache") / "backtest"
    market_dir = cache / "market_data"
    parts = []
    for f in sorted(market_dir.glob("*.parquet")):
        parts.append(pd.read_parquet(f))
    if not parts:
        raise FileNotFoundError(f"캐시 없음: {market_dir}")
    return pd.concat(parts, ignore_index=True)


def run_single(market_data, base_config, key, spec, track_stats=False):
    """하나의 유니버스 설정으로 백테스트 실행."""
    config = replace(
        base_config,
        min_market_cap=spec["min_market_cap"],
        max_market_cap=spec["max_market_cap"],
    )

    max_label = f"max={config.max_market_cap / 1e9:.0f}B" if config.max_market_cap > 0 else "max=inf"
    print(f"\n{'=' * 60}", flush=True)
    print(f"  [{key}] {spec['label']}", flush=True)
    print(
        f"  min={config.min_market_cap / 1e9:.0f}B, {max_label}",
        flush=True,
    )

    stats = {} if track_stats else None
    history, metrics = run_backtest(market_data, config, track_stats=stats)
    history = benchmark_strategy(history, config)

    last = history.iloc[-1]
    kospi_ret = round(last.get("KOSPI_Return(%)", 0), 2)
    alpha = round(last.get("Alpha(%)", 0), 2)

    avg_stocks = (
        history["Stock_Count"].mean()
        if "Stock_Count" in history.columns
        else 0
    )

    avg_turnover = 0.0
    if track_stats and stats and stats.get("rebalances"):
        turnovers = [
            r["one_way_turnover_pct"]
            for r in stats["rebalances"]
            if r.get("telemetry_complete", True)
        ]
        avg_turnover = sum(turnovers) / len(turnovers) * 100 if turnovers else 0.0

    row = {
        "설정": key,
        "라벨": spec["label"],
        "CAGR(%)": metrics["CAGR_PCT"],
        "MDD(%)": metrics["MAX_DRAWDOWN_PCT"],
        "누적수익(%)": metrics["TOTAL_RETURN_PCT"],
        "KOSPI(%)": kospi_ret,
        "Alpha(%)": alpha,
        "평균종목수": round(avg_stocks, 1),
        "평균턴오버(%)": round(avg_turnover, 1),
        "비용(만원)": round(metrics["TOTAL_COST_IMPACT_KRW"] / 1e4, 1),
        "최종자산": f"{metrics['FINAL_PORTFOLIO_VALUE']:,}",
    }

    print(
        f"  -> CAGR {row['CAGR(%)']}% / MDD {row['MDD(%)']}% / "
        f"누적 {row['누적수익(%)']}% / KOSPI {row['KOSPI(%)']}% / "
        f"Alpha {row['Alpha(%)']}%",
        flush=True,
    )

    return row


def print_summary(rows):
    """결과 비교 테이블 출력."""
    print(f"\n{'=' * 80}")
    print("  유니버스 Pre-check 결과 비교")
    print(f"{'=' * 80}")

    hdr = (
        f"{'설정':>4} | {'CAGR':>8} | {'MDD':>8} | {'누적':>8} | "
        f"{'KOSPI':>8} | {'Alpha':>8} | {'종목':>5} | {'턴오버':>7}"
    )
    print(hdr)
    print("-" * len(hdr))

    for r in rows:
        print(
            f"  {r['설정']:>2} | {r['CAGR(%)']:>7.2f}% | {r['MDD(%)']:>7.2f}% | "
            f"{r['누적수익(%)']:>7.2f}% | {r['KOSPI(%)']:>7.2f}% | "
            f"{r['Alpha(%)']:>7.2f}% | {r['평균종목수']:>5.1f} | "
            f"{r['평균턴오버(%)']:>6.1f}%"
        )

    print("-" * len(hdr))
    print()


def print_go_no_go(rows):
    """Oracle 권고 기준 go/no-go 판정."""
    print(f"{'=' * 80}")
    print("  Go / No-Go 판정 (Oracle 권고 기준)")
    print(f"{'=' * 80}")

    max_cagr = max(r["CAGR(%)"] for r in rows)
    max_alpha = max(r["Alpha(%)"] for r in rows)
    any_mdd_over_40 = any(r["MDD(%)"] < -40 for r in rows)
    all_underperform = all(r["Alpha(%)"] < 0 for r in rows)
    all_cagr_under_12 = all(r["CAGR(%)"] < 12 for r in rows)
    any_cagr_over_18 = any(r["CAGR(%)"] > 18 for r in rows)

    print(f"  최대 CAGR: {max_cagr}%")
    print(f"  최대 Alpha: {max_alpha}%")
    print(
        f"  MDD > 40% 설정: {'있음' if any_mdd_over_40 else '없음'}"
    )
    print()

    if all_cagr_under_12 and all_underperform:
        print("  STOP: 전 설정 CAGR < 12% + KOSPI 대비 전부 언더퍼폼")
        print("     -> 프레임워크로 시장 이길 수 없음. 유니버스 확대 의미 없음.")
        verdict = "STOP"
    elif any_cagr_over_18 and not all_underperform:
        print("  GO: 최적 설정 CAGR > 18% + KOSPI 아웃퍼폼 존재")
        print("     -> 유니버스 확대 실험 진행 권고 (Phase 1~7)")
        verdict = "GO"
    elif max_cagr >= 12:
        print("  CONDITIONAL: 최적 설정 CAGR 12~18%")
        print("     -> 3개월 checkpoint 후 본실험 결정")
        verdict = "CONDITIONAL"
    else:
        print("  STOP: 최적 설정 CAGR < 12%")
        print("     -> 유니버스 확대 의미 없음")
        verdict = "STOP"

    if any_mdd_over_40:
        print("  + 리스크 경고: MDD > 40% 설정 존재")

    print(f"\n  판정: {verdict}")
    return verdict


def save_results(rows, path):
    """CSV 저장."""
    df = pd.DataFrame(rows)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    print(f"\n  결과 저장: {path}")


def main():
    parser = argparse.ArgumentParser(description="유니버스 확대 선행 검증")
    parser.add_argument(
        "--configs",
        nargs="+",
        default=["A", "B", "C"],
        help="실행할 설정 (기본: A B C, D는 v2 수집 필요)",
    )
    parser.add_argument(
        "--start", default="2016-01-01", help="백테스트 시작일"
    )
    parser.add_argument(
        "--end", default="2026-06-30", help="백테스트 종료일"
    )
    parser.add_argument(
        "--track-turnover",
        action="store_true",
        help="턴오버 추적 (느림, 선택)",
    )
    parser.add_argument(
        "--output",
        default="results/universe_precheck.csv",
        help="결과 CSV 경로",
    )
    args = parser.parse_args()

    # 설정 검증
    for key in args.configs:
        if key not in UNIVERSE_CONFIGS:
            print(f"오류: 알 수 없는 설정 '{key}'. 사용 가능: {list(UNIVERSE_CONFIGS.keys())}")
            sys.exit(1)
        spec = UNIVERSE_CONFIGS[key]
        if spec.get("requires_v2"):
            print(
                f"주의: 설정 {key} ({spec['label']})는 v2 market_data 수집이 필요합니다. "
                f"v1 캐시에는 market 컬럼이 없어 KOSPI 필터링 불가."
            )

    # 데이터 로드
    print("market_data 캐시 로드 중...")
    market_data = load_market_data()
    print(
        f"  기간: {market_data['date'].min().date()} ~ {market_data['date'].max().date()}, "
        f"{market_data['code'].nunique()}종목"
    )

    # 기본 설정 (frozen params)
    base = Config.from_env()
    base = replace(
        base,
        start_date=args.start,
        end_date=args.end,
        use_market_regime=False,  # PBR은 regime off에서 수익 상승 확인됨
        # 파라미터 동결 (from .env)
    )
    print(
        f"  설정: {base.start_date}~{base.end_date}, "
        f"n_stocks={base.n_stocks}, pbr_pctile={base.pbr_pctile}, "
        f"per_pctile={base.per_pctile}, regime=off"
    )

    # 각 설정 실행
    rows = []
    for key in args.configs:
        spec = UNIVERSE_CONFIGS[key]
        if spec.get("requires_v2"):
            print(f"\n  [{key}] skip -- v2 데이터 필요")
            continue
        row = run_single(
            market_data, base, key, spec, track_stats=args.track_turnover
        )
        rows.append(row)

    if not rows:
        print("실행된 설정이 없습니다.")
        sys.exit(1)

    # 결과 출력 및 저장
    print_summary(rows)
    verdict = print_go_no_go(rows)
    save_results(rows, args.output)

    # D 설정 참고사항
    if "D" in args.configs:
        print(f"\n{'=' * 80}")
        print("  설정 D (KOSPI only, 500B~) 참고")
        print(f"{'=' * 80}")
        print("  v1 캐시에는 market 컬럼이 없어 KOSPI/KOSDAQ 분리 불가.")
        print("  D 실행을 위해서는 다음 중 하나 필요:")
        print("    1. v2 market_data 수집 (market_scope=['KOSPI'])")
        print("    2. pykrx ticker list로 필터링 (네트워크 필요)")
        print("  -> A/B/C 결과가 유망하면 D를 포함한 전체 실험 진행.")

    return verdict


if __name__ == "__main__":
    main()
