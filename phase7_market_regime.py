"""Phase 7: 시장조건 의존성 재검증 (A2, 캐시 전용).

PBR(음수 PER 필터 반영, 2026-08-11 재동결) 성과가
  - KOSPI 시장 베타인지
  - MA200 레짐 타이밍 필터 기여인지
  - 진짜 종목선택 알파인지
를 OOS 폴드 단위로 분해한다.

분해식 (각 폴드 누적수익률 기준):
  PBR(on)      = PBR(off) + MA200타이밍기여
  PBR(off)     = 유니버스동일가중 + 종목선택알파
  유니버스동일가중 ≈ KOSPI + 유니버스차이(시장베타·스타일 편중)

판정 기준:
  - 종목선택알파(PBR(off)−EW)가 폴드 간 지속 양수면 진짜 알파
  - PBR(off)≈EW이면 종목선택 알파 부재 (베타·편중만)
  - MA200타이밍기여(PBR(on)−PBR(off))가 크면 레짐 의존 (M2와 동일 패턴)

사용법:
  uv run python phase7_market_regime.py
결과: results/phase7_market_regime.csv
"""

from dataclasses import replace
from pathlib import Path

import pandas as pd

from config import Config
from engine import run_backtest
from phase3_baselines import equal_weight_baseline

# ── PBR 재동결 (2026-08-11: 음수 PER 필터 반영) ────────────────────────
PBR = dict(
    use_katsenelson=False,
    use_multi_factor=True,
    use_momentum=False,
    use_low_volatility=False,
    max_turnover=0.5,
    exclude_negative_per=True,
)

FOLDS = [
    ("2016-2019", "2016-01-01", "2019-12-31"),
    ("2019-2022", "2019-01-01", "2022-12-31"),
    ("2022-2025", "2022-01-01", "2025-06-30"),
    ("2023-2026", "2023-01-01", "2026-06-30"),
]


def load_market_data() -> pd.DataFrame:
    parts = []
    for f in sorted(Path(".cache/backtest/market_data").glob("*.parquet")):
        parts.append(pd.read_parquet(f))
    md = pd.concat(parts, ignore_index=True)
    md["year_month"] = md["date"].dt.to_period("M")
    return md


def kospi_fold_return(kospi: pd.DataFrame, start: str, end: str) -> float:
    seg = kospi.loc[start:end, "kospi_close"].dropna()
    if len(seg) < 2:
        return float("nan")
    return (seg.iloc[-1] / seg.iloc[0] - 1) * 100


def fold_market_data(md: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    return md[(md["date"] >= pd.Timestamp(start)) & (md["date"] <= pd.Timestamp(end))]


def main():
    md = load_market_data()
    kospi = pd.read_parquet(".cache/backtest/kospi.parquet")
    base = Config.from_env()
    out = Path("results")
    out.mkdir(exist_ok=True)

    rows = []
    print("== 폴드별 PBR MA200 on/off + 유니버스 동일가중 분해 ==")
    for fold_name, start, end in FOLDS:
        print(f"\n▶ {fold_name} ({start}~{end})", flush=True)
        fmd = fold_market_data(md, start, end)
        kospi_ret = kospi_fold_return(kospi, start, end)

        # 1) PBR MA200 on (기본 동결 설정)
        cfg_on = replace(base, start_date=start, end_date=end, **PBR)
        h_on, m_on = run_backtest(fmd, cfg_on)
        # 2) PBR MA200 off (종목선택만, 풀노출)
        cfg_off = replace(
            base, start_date=start, end_date=end, use_market_regime=False, **PBR
        )
        h_off, m_off = run_backtest(fmd, cfg_off)
        # 3) 유니버스 동일가중 기준선 (종목선택 알파=0)
        cfg_ew = replace(
            base,
            start_date=start,
            end_date=end,
            max_market_cap=1_000_000_000_000,
        )
        h_ew, m_ew = equal_weight_baseline(fmd, cfg_ew)

        pbr_on = m_on["TOTAL_RETURN_PCT"]
        pbr_off = m_off["TOTAL_RETURN_PCT"]
        ew = m_ew["TOTAL_RETURN_PCT"]
        exposure = (h_on["Stock_Count"] > 0).mean() * 100

        rows.append(
            {
                "폴드": fold_name,
                "KOSPI 누적(%)": round(kospi_ret, 2),
                "유니버스동일가중 누적(%)": round(ew, 2),
                "PBR(on) 누적(%)": round(pbr_on, 2),
                "PBR(off) 누적(%)": round(pbr_off, 2),
                "PBR(on) MDD(%)": m_on["MAX_DRAWDOWN_PCT"],
                "PBR(off) MDD(%)": m_off["MAX_DRAWDOWN_PCT"],
                "시장노출기간(%)(on)": round(exposure, 1),
                "종목선택알파(off−EW)(pp)": round(pbr_off - ew, 2),
                "MA200타이밍기여(on−off)(pp)": round(pbr_on - pbr_off, 2),
                "유니버스차이(EW−KOSPI)(pp)": round(ew - kospi_ret, 2),
                "PBR(on)−KOSPI(pp)": round(pbr_on - kospi_ret, 2),
            }
        )
        print(
            f"  KOSPI={kospi_ret:7.2f}  EW={ew:7.2f}  PBR(on)={pbr_on:7.2f} "
            f"PBR(off)={pbr_off:7.2f}"
        )
        print(
            f"  → 종목선택알파={pbr_off - ew:+.2f}pp  "
            f"MA200기여={pbr_on - pbr_off:+.2f}pp  "
            f"노출={exposure:.0f}%"
        )

    df = pd.DataFrame(rows)
    df.to_csv(out / "phase7_market_regime.csv", index=False, encoding="utf-8-sig")

    print("\n" + "=" * 78)
    print(df.to_string(index=False))
    print("\n저장: results/phase7_market_regime.csv")

    # ── 요약 판정 ──
    sel = df["종목선택알파(off−EW)(pp)"]
    ma = df["MA200타이밍기여(on−off)(pp)"]
    print("\n== 요약 판정 ==")
    print(f"종목선택알파: 폴드 {len(sel)}개 중 양수 {int((sel > 0).sum())}개 "
          f"(평균 {sel.mean():+.2f}pp)")
    print(f"MA200타이밍기여: 폴드 {len(ma)}개 중 양수 {int((ma > 0).sum())}개 "
          f"(평균 {ma.mean():+.2f}pp)")


if __name__ == "__main__":
    main()
