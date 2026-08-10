#!/usr/bin/env python3
"""간이 리플레이 엔진 (신호 품질 검증용)

실행:
    python replay_runner.py                        # 오늘 전체
    python replay_runner.py 2026-05-27             # 특정 날짜
    python replay_runner.py 2026-05-27 010170      # 특정 종목

결과: 콘솔 출력 + reports/replay_YYYYMMDD.txt 저장
"""

from __future__ import annotations

import sys
import io

import csv
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

MINUTE_BARS_DIR = Path("data/minute_bars")
REPORTS_DIR     = Path("reports")
AFTER_MINUTES   = [5, 10, 20]

# ── 비용 설정 ─────────────────────────────────────────────────────
# 2026-08-07 (1J): 비용은 domain/cost_model.py 단일 출처에서 읽습니다.
# 예전엔 여기에 0.25 + 0.10 = 0.35%를 직접 박아뒀는데,
# daily_report는 0.90%를 쓰고 있어 두 기준이 갈라져 있었습니다.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from domain.cost_model import load_cost_model  # noqa: E402
from domain.replay_context import (  # noqa: E402
    ReplayDayContext, resolve_prev_close, build_day_context, is_full_window,
)
from config.settings import load_settings  # noqa: E402

class ReplayConfigError(RuntimeError):
    """리플레이 설정을 신뢰할 수 없을 때 발생합니다."""


# 2026-08-07 (1J.3.1): 예전엔 설정 로딩 실패 시 조용히 60으로
# 돌아갔음 — 1J.1에서 비용 모델에 대해 제거했던 것과 **같은 문제**를
# replay에 남겨둔 것. 실제 설정이 minute_bar_count=90으로 바뀌었는데
# 로딩이 실패하면 51일 백테스트가 아무 경고 없이 60봉으로 돌아감.
# 백테스트는 fail-closed가 맞습니다.
ALLOW_CONFIG_FALLBACK = False      # --allow-config-fallback 으로만 활성
FALLBACK_MINUTE_BAR_COUNT = 60


def resolve_minute_bar_count(*, allow_fallback: bool = False) -> int:
    try:
        value = load_settings().market_regime.minute_bar_count
    except Exception as exc:
        if allow_fallback:
            print(f"[WARN] 설정 로딩 실패 — fallback {FALLBACK_MINUTE_BAR_COUNT}봉 사용: {exc}")
            return FALLBACK_MINUTE_BAR_COUNT
        raise ReplayConfigError(
            f"설정을 읽을 수 없어 리플레이를 중단합니다: {exc}\n"
            f"(백테스트가 다른 윈도우 크기로 조용히 도는 것을 막기 위함 — "
            f"디버깅 목적이면 --allow-config-fallback)") from exc
    if not isinstance(value, int) or value <= 0:
        if allow_fallback:
            print(f"[WARN] minute_bar_count가 유효하지 않음({value!r}) — "
                  f"fallback {FALLBACK_MINUTE_BAR_COUNT}봉 사용")
            return FALLBACK_MINUTE_BAR_COUNT
        raise ReplayConfigError(
            f"minute_bar_count가 유효하지 않습니다: {value!r} (양의 정수여야 함)")
    return value


# 2026-08-07 (1J.3.2): import 시 config I/O 금지.
# 예전엔 여기서 즉시 resolve해서, 설정이 정말 깨졌을 때
# `--allow-config-fallback`을 줘도 main()에 도달하기 전에
# ReplayConfigError가 나 옵션이 무용지물이었음.
# main()에서 CLI 파싱 후 resolve하고, run_replay에는 명시적으로
# 전달합니다(1J.5가 이 모듈을 import하므로 side-effect 최소화).
MINUTE_BAR_COUNT: int | None = None

# 2026-08-07 (1J.3.1): prev_close 복원 결과와 horizon 품질을 집계해
# 리포트에 노출합니다. "전체 데이터를 분석했다"고 말하지 않기 위함 —
# 실측상 같은 CSV만으로는 51.7%만 복원됩니다.
class ReplayEvaluationError(RuntimeError):
    """리플레이 평가 중 analyzer가 실패했을 때 발생합니다."""


@dataclass
class ReplayQualityStats:
    """거래일별로 분리 가능한 리플레이 품질 통계 (1J.3.2).

    예전엔 module global이라 같은 프로세스에서 51거래일을 돌리면
    통계가 계속 누적됐습니다. 1K가 멀티데이 분석이므로 객체로
    분리해 거래일별/전체 집계를 따로 낼 수 있게 합니다.
    """

    prev_close_sources: dict[str, int] = field(default_factory=dict)
    calendar_gaps: dict[int, int] = field(default_factory=dict)
    horizon_elapsed: dict[int, list] = field(
        default_factory=lambda: {5: [], 10: [], 20: []})
    analyzer_error_symbols: list[str] = field(default_factory=list)
    analyzer_error_timestamps: list[str] = field(default_factory=list)
    symbol_days: int = 0
    analyzer_mode: str = "LIVE_MINUTE_ANALYZER"
    # 2026-08-07 (1J.3.3): programmatic 실행(run_replay를 직접 호출)
    # 에서는 전역 MINUTE_BAR_COUNT가 None일 수 있어 리포트가 실제와
    # 다르게 표시됐음. 실제 사용값을 stats에 기록합니다.
    minute_bar_count: int | None = None
    # history 완전성 (1J.3.3)
    history_status: dict[str, int] = field(default_factory=dict)
    full_window_candidates: int = 0
    partial_window_candidates: int = 0
    first_full_window: dict[str, str] = field(default_factory=dict)

    @property
    def analyzer_error_count(self) -> int:
        return len(self.analyzer_error_timestamps)

    @property
    def total_candidate_points(self) -> int:
        return self.full_window_candidates + self.partial_window_candidates

    def merge(self, other: "ReplayQualityStats") -> None:
        # 2026-08-07 (1J.3.3): 서로 다른 설정이 섞이면 1K에서 쓰면
        # 안 되므로 MIXED_CONFIG로 표시합니다.
        if self.minute_bar_count is None:
            self.minute_bar_count = other.minute_bar_count
        elif other.minute_bar_count is not None and \
                other.minute_bar_count != self.minute_bar_count:
            self.minute_bar_count = "MIXED_CONFIG"
        if other.analyzer_mode != self.analyzer_mode:
            self.analyzer_mode = "MIXED_CONFIG"
        for k, v in other.history_status.items():
            self.history_status[k] = self.history_status.get(k, 0) + v
        self.full_window_candidates += other.full_window_candidates
        self.partial_window_candidates += other.partial_window_candidates
        self.first_full_window.update(other.first_full_window)
        for k, v in other.prev_close_sources.items():
            self.prev_close_sources[k] = self.prev_close_sources.get(k, 0) + v
        for k, v in other.calendar_gaps.items():
            self.calendar_gaps[k] = self.calendar_gaps.get(k, 0) + v
        for m in self.horizon_elapsed:
            self.horizon_elapsed[m].extend(other.horizon_elapsed.get(m, []))
        self.analyzer_error_symbols.extend(other.analyzer_error_symbols)
        self.analyzer_error_timestamps.extend(other.analyzer_error_timestamps)
        self.symbol_days += other.symbol_days


# 하위호환 — 단일 거래일 CLI 실행용 기본 인스턴스
_DEFAULT_STATS = ReplayQualityStats()


def reset_replay_quality_stats() -> ReplayQualityStats:
    global _DEFAULT_STATS
    _DEFAULT_STATS = ReplayQualityStats()
    return _DEFAULT_STATS
COST_MODEL = load_cost_model()
TOTAL_COST_PCT = COST_MODEL.base_roundtrip_pct   # 하위호환(Base 시나리오)
ROUND_TRIP_COST_PCT = TOTAL_COST_PCT
SLIPPAGE_PCT = 0.0

# ── 고위험 종목 경고 기준 ─────────────────────────────────────────
RISK_MAE_THRESHOLD     = -3.0   # MAE 평균이 이 값 이하면 경고
RISK_5M_THRESHOLD      = -1.0   # 5분 평균 수익률이 이 값 이하면 경고
RISK_20M_THRESHOLD     = -1.0   # 20분 평균 수익률이 이 값 이하면 경고


@dataclass
class MinuteBarRow:
    cntr_tm: str
    open_price: int
    high_price: int
    low_price: int
    close_price: int
    volume: int
    acc_volume: int = 0


def load_bars(symbol: str, target_date: date) -> list[MinuteBarRow]:
    path = MINUTE_BARS_DIR / target_date.strftime("%Y%m%d") / f"{symbol}.csv"
    if not path.exists():
        return []
    bars, acc = [], 0
    with path.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                v = int(float(r.get("volume", 0) or 0))
                acc += v
                bars.append(MinuteBarRow(
                    cntr_tm     = r["cntr_tm"],
                    open_price  = int(float(r.get("open",  0) or 0)),
                    high_price  = int(float(r.get("high",  0) or 0)),
                    low_price   = int(float(r.get("low",   0) or 0)),
                    close_price = int(float(r.get("close", 0) or 0)),
                    volume      = v,
                    acc_volume  = acc,
                ))
            except (ValueError, KeyError):
                continue
    return bars


def try_import_analyzer(*, allow_simple_fallback: bool = False):
    """live와 동일한 MinuteAnalyzer를 만듭니다 (기본 fail-closed)."""
    try:
        import os
        sys.path.insert(0, os.getcwd())
        from config.settings import load_settings
        from domain.market_regime.minute_analyzer import MinuteAnalyzer
        settings = load_settings()
        cfg = settings.market_regime
        return MinuteAnalyzer(
            min_trading_value       = 0,
            pullback_min_pct        = cfg.pullback_min_pct,
            pullback_max_pct        = cfg.pullback_max_pct,
            change_rate_min         = cfg.change_rate_min,
            change_rate_max         = cfg.change_rate_max,
            rebound_min_pct         = cfg.rebound_min_pct,
            v_bottom_lookback       = cfg.v_bottom_lookback,
            v_low_min_age           = cfg.v_low_min_age,
            v_low_max_age           = cfg.v_low_max_age,
            v_drop_threshold_pct    = cfg.v_drop_threshold_pct,
            v_rebound_threshold_pct = cfg.v_rebound_threshold_pct,
            v_max_rebound_pct       = cfg.v_max_rebound_pct,
            v_volume_ratio          = cfg.v_volume_ratio,
            v_min_bar_amount        = 0,
            v_bottom_spike_ratio    = cfg.v_bottom_spike_ratio,
            v_ma5_slope_bars        = cfg.v_ma5_slope_bars,
        )
    except Exception as e:
        # 2026-08-07 (1J.3.1): 예전엔 None을 돌려주고 replay가
        # A(simple) 간이 전략(등락률 2~18%)으로 51일을 끝까지 돌 수
        # 있었음 — 백테스트가 실패하지 않고 **다른 전략** 결과를
        # 내놓는 셈이라 가장 위험한 형태. 기본은 즉시 실패.
        if allow_simple_fallback:
            print(f"[WARN] MinuteAnalyzer 생성 실패 — SIMPLE_FALLBACK으로 진행: {e}")
            return None
        raise ReplayConfigError(
            f"MinuteAnalyzer를 만들 수 없어 리플레이를 중단합니다: {e}\n"
            f"(간이 전략으로 대체하면 실제 전략과 다른 결과가 나옵니다 — "
            f"디버깅 목적이면 --allow-simple-fallback)") from e


def get_time_bucket(cntr_tm: str) -> str:
    """봉 시각을 시간대 버킷으로 분류합니다."""
    try:
        hhmm = int(cntr_tm[8:12])
        if hhmm < 1000:   return "09:00~10:00"
        if hhmm < 1330:   return "10:00~13:30"
        if hhmm < 1450:   return "13:30~14:50"
        return "14:50~"
    except (ValueError, IndexError):
        return "기타"


def run_replay(symbol: str, bars: list[MinuteBarRow], analyzer,
               target_date: date | None = None,
               *, minute_bar_count: int | None = None,
               quality_stats: "ReplayQualityStats | None" = None,
               skip_analyzer_errors: bool = False) -> list[dict]:
    # 2026-08-07 (1J.3): target_date를 명시적으로 받습니다 —
    # 파일에 전일 꼬리 봉이 섞여 있어 봉만 보고는 어느 날의
    # candidate를 평가해야 하는지 알 수 없기 때문입니다.
    if target_date is None:
        from domain.replay_context import parse_bar_dt as _pbd
        _dts = [d for d in (_pbd(getattr(b, 'cntr_tm', '')) for b in bars) if d]
        target_date = max(d.date() for d in _dts) if _dts else None
        if target_date is None:
            return []
    if len(bars) < 5:
        return []
    results = []
    # 2026-08-07 (1J.3): 시간축을 live와 맞추기 위해 공용 컨텍스트 사용.
    #   - 전일 꼬리 봉은 history로만 쓰고 candidate에서는 제외
    #   - analyzer window는 현재봉 포함 최근 minute_bar_count(=60)개
    #   - prev_close는 target_date 이전 마지막 봉의 close
    stats = quality_stats if quality_stats is not None else _DEFAULT_STATS
    mbc = minute_bar_count if minute_bar_count is not None else (
        MINUTE_BAR_COUNT if MINUTE_BAR_COUNT is not None else resolve_minute_bar_count())
    stats.minute_bar_count = mbc
    # 2026-08-07 (1J.3.3): prev_close만이 아니라 **history**도 함께
    # 복원합니다. 장초 파일은 직전 데이터 날짜 tail을 prepend하고,
    # 장중부터 시작한 파일은 억지로 채우지 않습니다.
    ctx, history_status = build_day_context(
        symbol, bars, target_date, mbc, load_bars, MINUTE_BARS_DIR)
    stats.history_status[history_status] = \
        stats.history_status.get(history_status, 0) + 1
    pc = resolve_prev_close(ctx, symbol, load_bars, MINUTE_BARS_DIR, target_date)
    stats.symbol_days += 1
    stats.prev_close_sources[pc.source] = stats.prev_close_sources.get(pc.source, 0) + 1
    if pc.calendar_gap_days is not None:
        stats.calendar_gaps[pc.calendar_gap_days] = \
            stats.calendar_gaps.get(pc.calendar_gap_days, 0) + 1
    if not pc.available:
        # 임의 추정하지 않고 skip — 등락률 A조건에 직접 들어가는 값이라
        # 잘못 넣으면 결과가 조용히 왜곡됨.
        return []
    prev_close = pc.value

    for i in ctx.target_indices:
        if i < 5:
            continue
        window  = ctx.analysis_window(i)
        current = ctx.all_bars[i]
        entry_dt = ctx.bar_dt(i)
        if entry_dt is None:
            continue
        # 2026-08-07 (1J.3.3): live와 같은 60봉이 확보된 시점인지.
        # 1J.5 A(Aligned Value Fidelity)는 full-window 표본을 주
        # 평가 대상으로 삼습니다(partial은 별도 limitation).
        full_window = is_full_window(ctx, i)
        if full_window:
            stats.full_window_candidates += 1
            key = f"{symbol}@{target_date.isoformat()}"
            if key not in stats.first_full_window:
                stats.first_full_window[key] = str(current.cntr_tm)
        else:
            stats.partial_window_candidates += 1
        is_v = is_pr = False
        patterns = "-"

        if analyzer is not None:
            try:
                analysis = analyzer.analyze(window, prev_close)
                if analysis is None:
                    continue
                has_pattern = any([
                    analysis.is_valid_change_rate,
                    analysis.is_valid_rebound,
                    analysis.is_valid_pulldown,
                    analysis.is_v_rebound,
                    analysis.is_pulldown_recovery,
                ])
                if not (has_pattern and analysis.price_above_vwap):
                    continue
                is_v  = analysis.is_v_rebound
                is_pr = analysis.is_pulldown_recovery
                pats  = []
                if analysis.is_v_rebound:            pats.append("V")
                if analysis.is_pulldown_recovery:    pats.append("PR")
                if analysis.is_valid_change_rate:    pats.append("A")
                if analysis.is_valid_rebound:        pats.append("B")
                if analysis.is_valid_pulldown:       pats.append("C")
                patterns = "/".join(pats) if pats else "-"
            except Exception as exc:
                # 2026-08-07 (1J.3.2): 예전엔 조용히 continue라
                # analyzer 오류로 사라진 후보를 "전략 불일치"로
                # 오해할 수 있었음(1J.5 fidelity에서 특히 위험).
                stats.analyzer_error_symbols.append(symbol)
                stats.analyzer_error_timestamps.append(str(current.cntr_tm))
                if skip_analyzer_errors:
                    continue
                raise ReplayEvaluationError(
                    f"analyzer 평가 실패 — symbol={symbol} "
                    f"target_date={target_date} cntr_tm={current.cntr_tm} "
                    f"window_len={len(window)} prev_close={prev_close}: {exc}\n"
                    f"(후보가 조용히 사라지면 fidelity를 오판하므로 즉시 중단합니다 — "
                    f"장기 실행에서 건너뛰려면 --skip-analyzer-errors)"
                ) from exc
        else:
            chg = (current.close_price - prev_close) / prev_close * 100
            if not (2.0 <= chg <= 18.0):
                continue
            patterns = "A(simple)"

        entry_price = current.close_price
        entry_time  = current.cntr_tm

        # 2026-08-07 (1J.3): "5분 후"를 봉 개수(i+m)가 아니라 clock-time
        # 으로 계산. 실측상 46.1% 파일에 1분 초과 gap, 15.3%에 5분 이상
        # gap이 있어 i+5가 8분·15분·35분 후가 될 수 있었음.
        after_pcts: dict[int, float | None] = {}
        elapsed: dict[int, float | None] = {}
        for m in AFTER_MINUTES:
            hp = ctx.price_at_horizon(entry_dt, m)
            if hp.available:
                after_pcts[m] = (hp.price - entry_price) / entry_price * 100
                elapsed[m] = round(hp.actual_elapsed_minutes, 1)
            else:
                after_pcts[m] = None
                elapsed[m] = None
            stats.horizon_elapsed[m].append(elapsed[m])

        mfe, mae = ctx.mfe_mae(entry_dt, entry_price, minutes=20)

        results.append({
            "entry_time":  entry_time,
            "full_window": full_window,
            "history_status": history_status,
            "entry_price": entry_price,
            "patterns":    patterns,
            "is_v":        is_v,
            "is_pr":       is_pr,
            "time_bucket": get_time_bucket(entry_time),
            "after_5m":    after_pcts.get(5),
            "after_10m":   after_pcts.get(10),
            "after_20m":   after_pcts.get(20),
            # 비용 반영 순수익
            # 2026-08-07 (1J.1): net_*는 Base alias, 3시나리오 병행 산출
            "gross_5m": COST_MODEL.net(after_pcts.get(5), "gross"),
            "base_5m": COST_MODEL.net(after_pcts.get(5), "base"),
            "stress_5m": COST_MODEL.net(after_pcts.get(5), "stress"),
            "net_5m": COST_MODEL.net(after_pcts.get(5), "base"),
            "gross_10m": COST_MODEL.net(after_pcts.get(10), "gross"),
            "base_10m": COST_MODEL.net(after_pcts.get(10), "base"),
            "stress_10m": COST_MODEL.net(after_pcts.get(10), "stress"),
            "net_10m": COST_MODEL.net(after_pcts.get(10), "base"),
            "gross_20m": COST_MODEL.net(after_pcts.get(20), "gross"),
            "base_20m": COST_MODEL.net(after_pcts.get(20), "base"),
            "stress_20m": COST_MODEL.net(after_pcts.get(20), "stress"),
            "net_20m": COST_MODEL.net(after_pcts.get(20), "base"),
            # 2026-08-07 (1J.3): idx+m을 "m분 후"라고 부르지 않기 위해
            # 실제 경과 시간을 함께 기록합니다.
            "elapsed_5m":  elapsed.get(5),
            "elapsed_10m": elapsed.get(10),
            "elapsed_20m": elapsed.get(20),
            "mfe":         round(mfe, 2),
            "mae":         round(mae, 2),
        })
    return results



def build_quality_report(stats: "ReplayQualityStats | None" = None) -> list[str]:
    """리플레이 데이터 품질 요약 (1J.3.1 → 1J.3.2).

    "전체 데이터를 분석했다"고 말할 수 없는 이유를 수치로 드러냅니다.
    """
    st = stats if stats is not None else _DEFAULT_STATS
    L: list[str] = []
    L.append("")
    L.append("── 리플레이 데이터 품질 ──")
    L.append(f"  analyzer_mode = {st.analyzer_mode}")
    if st.analyzer_mode == "SIMPLE_FALLBACK":
        L.append("  ⚠ 간이 전략(A simple)입니다 — 실제 전략과 다르므로")
        L.append("    1K enforce 판단에 사용하지 마십시오.")
    L.append(f"  minute_bar_count = {st.minute_bar_count} (live와 동일)")
    if st.minute_bar_count == "MIXED_CONFIG" or st.analyzer_mode == "MIXED_CONFIG":
        L.append("  ⚠ 서로 다른 설정이 섞였습니다 — 1K에서 사용 금지.")

    if st.analyzer_error_count:
        L.append("")
        L.append(f"  ⚠ analyzer_error_count      {st.analyzer_error_count}")
        L.append(f"    analyzer_error_symbols    "
                 f"{sorted(set(st.analyzer_error_symbols))[:10]}")
        L.append(f"    analyzer_error_timestamps {st.analyzer_error_timestamps[:5]}")
        L.append("    ※ --skip-analyzer-errors로 건너뛴 결과입니다.")
        L.append("      1J.5/1K 기본 실행에서는 이 옵션을 쓰지 마십시오.")

    total = st.symbol_days
    if total:
        src = st.prev_close_sources
        avail = total - src.get("UNAVAILABLE", 0)
        L.append("")
        L.append(f"  total_symbol_days          {total}")
        L.append(f"  same_file_count            {src.get('SAME_FILE_PRETARGET', 0)}")
        L.append(f"  previous_data_day_count    {src.get('PREVIOUS_DATA_DAY_FILE', 0)}")
        L.append(f"  unavailable_count          {src.get('UNAVAILABLE', 0)}")
        L.append(f"  prev_close_coverage_pct    {avail / total * 100:.1f}%")
        L.append("  ※ prev_close는 **바로 직전 데이터 날짜 1곳**만 봅니다.")
        L.append("    (과거를 무제한으로 거슬러 올라가면 며칠 전 종가를")
        L.append("     전일 종가로 쓰게 되어 등락률·A조건이 왜곡됩니다)")
        L.append("  ※ 복원하지 못한 종목-일은 제외되므로 '전체 데이터'가 아닙니다.")
        if st.calendar_gaps:
            gaps = ", ".join(f"{k}일:{v}" for k, v in sorted(st.calendar_gaps.items()))
            L.append(f"  previous_data_day의 calendar_gap 분포: {gaps}")
            L.append("    (주말·공휴일을 건너뛰면 3~4일이 정상입니다)")

    # history 완전성 (1J.3.3)
    if st.history_status or st.total_candidate_points:
        L.append("")
        L.append("  history 완전성")
        for k in ("SAME_FILE_COMPLETE", "PREVIOUS_DAY_RECONSTRUCTED",
                  "INCOMPLETE_INTRADAY"):
            n = st.history_status.get(k, 0)
            if n:
                L.append(f"    {k:28s} {n:5d}")
        tcp = st.total_candidate_points
        if tcp:
            L.append(f"    total_candidate_points       {tcp:5d}")
            L.append(f"    full_window_candidate_count  {st.full_window_candidates:5d}")
            L.append(f"    partial_window_candidate_count {st.partial_window_candidates:5d}")
            L.append(f"    full_window_coverage_pct     "
                     f"{st.full_window_candidates / tcp * 100:.1f}%")
        L.append("  ※ 장초 파일은 직전 데이터 날짜 tail로 history를 복원하고,")
        L.append("    장중부터 시작한 파일은 억지로 채우지 않습니다(INCOMPLETE_INTRADAY).")
        L.append("  ※ 1J.5 Aligned Value Fidelity는 full-window 표본을 주 대상으로,")
        L.append("    partial 표본은 별도 limitation으로 다루십시오.")

    # horizon 품질 — bucket은 상호 배타적
    if any(st.horizon_elapsed.get(m) for m in (5, 10, 20)):
        L.append("")
        L.append("  horizon 품질 (target 대비 실제 경과 시간)")
        for m in (5, 10, 20):
            vals = st.horizon_elapsed.get(m, [])
            if not vals:
                continue
            ok = [v for v in vals if v is not None]
            na = len(vals) - len(ok)
            if not ok:
                L.append(f"    {m:2d}분: 표본 {len(vals)} / 전부 N/A")
                continue
            # 2026-08-07 (1J.3.2): 예전엔 exact가 ≤1m stale에도
            # 중복 집계돼 "exact 251 / ≤1m stale 251"처럼 보였음.
            # 서로 배타적으로 나누고 합계 invariant를 보장합니다.
            exact = sum(1 for v in ok if abs(m - v) < 1e-9)
            under1 = sum(1 for v in ok if 0 < (m - v) <= 1)
            one_to_3 = sum(1 for v in ok if 1 < (m - v) <= 3)
            other = len(ok) - exact - under1 - one_to_3
            sv = sorted(ok)
            med = sv[len(sv) // 2]
            p10 = sv[int(len(sv) * 0.10)]
            p90 = sv[int(len(sv) * 0.90)]
            line = (f"    {m:2d}분: 표본 {len(vals):5d}  exact {exact:5d}"
                    f"  ≤1m stale {under1:5d}  1~3m stale {one_to_3:5d}  N/A {na:5d}")
            if other:
                line += f"  기타 {other}"
            L.append(line)
            L.append(f"          실제경과 median {med:.1f}분  p10 {p10:.1f}  p90 {p90:.1f}")
            assert exact + under1 + one_to_3 + other + na == len(vals)
        L.append("  ※ 최대 3분 stale 봉까지 mark-to-market으로 인정합니다.")
        L.append("  ※ exact / ≤1m / 1~3m / N/A는 상호 배타적이며 합계 = 표본입니다.")
    return L


def is_high_risk(results: list[dict]) -> tuple[bool, list[str]]:
    """고위험 종목 여부와 사유를 반환합니다."""
    reasons = []
    vals_5m  = [r["net_5m"]  for r in results if r["net_5m"]  is not None]
    vals_20m = [r["net_20m"] for r in results if r["net_20m"] is not None]
    maes     = [r["mae"]     for r in results]

    if vals_5m and sum(vals_5m) / len(vals_5m) <= RISK_5M_THRESHOLD:
        reasons.append(f"5분 순수익 평균 {sum(vals_5m)/len(vals_5m):+.2f}%")
    if vals_20m and sum(vals_20m) / len(vals_20m) <= RISK_20M_THRESHOLD:
        reasons.append(f"20분 순수익 평균 {sum(vals_20m)/len(vals_20m):+.2f}%")
    if maes and sum(maes) / len(maes) <= RISK_MAE_THRESHOLD:
        reasons.append(f"MAE 평균 {sum(maes)/len(maes):+.2f}%")
    return bool(reasons), reasons


def format_report(symbol: str, results: list[dict], target_date: date) -> str:
    lines = []
    W = 60

    def sep(c="─"): lines.append(c * W)
    sep("═")
    lines.append(f"  📊 리플레이  {symbol}  {target_date}")
    sep("═")

    if not results:
        lines.append("  BUY 신호 없음")
        sep()
        return "\n".join(lines)

    lines.append(f"  BUY 신호: {len(results)}건  │  비용 시나리오: {COST_MODEL.describe()}")
    lines.append("  ※ 아래 '비용 후'는 Base 기준. daily_report는 Stress(0.90%)를 쓰므로 수치가 다릅니다(같은 cost_model의 다른 시나리오).")
    lines.append("  ※ 패턴 리플레이: 패턴 감지 봉 기준 수익률 (실제 전략 BUY와 다를 수 있음 — 점수제/쿨다운 미적용)")
    lines.append("")

    # ── 1. 수익률 (비용 전/후) ───────────────────────────────────
    for m in AFTER_MINUTES:
        raw_key = f"after_{m}m"
        net_key = f"net_{m}m"
        raw_vals = [r[raw_key] for r in results if r[raw_key] is not None]
        net_vals = [r[net_key] for r in results if r[net_key] is not None]
        if not raw_vals:
            continue
        raw_win = sum(1 for v in raw_vals if v > 0)
        net_win = sum(1 for v in net_vals if v > 0)
        raw_avg = sum(raw_vals) / len(raw_vals)
        net_avg = sum(net_vals) / len(net_vals) if net_vals else 0
        lines.append(
            f"  {m:>2}분 후  승률 {raw_win}/{len(raw_vals)} ({raw_win/len(raw_vals)*100:.0f}%)"
            f"  평균 {raw_avg:+.2f}%  →  비용 후 {net_avg:+.2f}%  "
            f"(순수익 승률 {net_win}/{len(net_vals)} {net_win/len(net_vals)*100:.0f}%)"
        )
        # 2026-08-07 (1J): 결론이 비용 가정에 얼마나 민감한지
        # 드러내기 위해 세 시나리오를 항상 함께 출력합니다.
        # Base와 Stress가 같은 방향이면 견고한 결론, 부호가 갈리면
        # 비용 가정에 의존하는 취약한 결론이라는 뜻입니다.
        rates = COST_MODEL.positive_rates(raw_vals)
        for _s in ("gross", "base", "stress"):
            _key = "gross_positive_rate" if _s == "gross" else f"{_s}_net_positive_rate"
            lines.append(
                f"           {_s.capitalize():7s}{raw_avg - COST_MODEL.cost_pct(_s):+.2f}%"
                f"   플러스비율 {rates[_key]*100:.0f}%"
            )

    avg_mfe = sum(r["mfe"] for r in results) / len(results)
    avg_mae = sum(r["mae"] for r in results) / len(results)
    lines.append(f"  MFE 평균 {avg_mfe:+.2f}%  MAE 평균 {avg_mae:+.2f}%")

    # ── 고위험 경고 ────────────────────────────────────────────────
    high_risk, risk_reasons = is_high_risk(results)
    if high_risk:
        lines.append("")
        lines.append(f"  ⚠️  [WATCHLIST_EXCLUDE_CANDIDATE]  {symbol}")
        for r in risk_reasons:
            lines.append(f"      → {r}")

    # ── 2. 패턴 조합별 성과 ────────────────────────────────────────
    lines.append("")
    lines.append("  [ 패턴별 5분 순수익 성과 ]")
    pat_groups: dict[str, list[float]] = defaultdict(list)
    for r in results:
        if r["net_5m"] is not None:
            pat_groups[r["patterns"]].append(r["net_5m"])
    for pat, vals in sorted(pat_groups.items(), key=lambda x: -len(x[1])):
        w   = sum(1 for v in vals if v > 0)
        avg = sum(vals) / len(vals)
        flag = " ⚠️" if avg < -1.0 else (" ✅" if avg > 0 else "")
        lines.append(
            f"    {pat:<14} {len(vals):>3}건  승률 {w/len(vals)*100:>3.0f}%"
            f"  순수익 {avg:+.2f}%{flag}"
        )

    # ── 3. 시간대별 성과 ───────────────────────────────────────────
    lines.append("")
    lines.append("  [ 시간대별 5분 순수익 성과 ]")
    time_groups: dict[str, list[float]] = defaultdict(list)
    for r in results:
        if r["net_5m"] is not None:
            time_groups[r["time_bucket"]].append(r["net_5m"])
    for bucket in ["09:00~10:00", "10:00~13:30", "13:30~14:50", "14:50~"]:
        vals = time_groups.get(bucket, [])
        if not vals:
            continue
        w   = sum(1 for v in vals if v > 0)
        avg = sum(vals) / len(vals)
        lines.append(
            f"    {bucket:<14} {len(vals):>3}건  승률 {w/len(vals)*100:>3.0f}%"
            f"  순수익 {avg:+.2f}%"
        )

    # ── 4. V자 vs 일반 비교 ─────────────────────────────────────────
    v_res   = [r for r in results if r["is_v"]  and r["net_5m"] is not None]
    pr_res  = [r for r in results if r["is_pr"] and r["net_5m"] is not None]
    gen_res = [r for r in results if not r["is_v"] and not r["is_pr"] and r["net_5m"] is not None]
    if v_res or pr_res:
        lines.append("")
        lines.append("  [ V자/PR vs 일반 비교 (5분 순수익) ]")
        lines.append("    ※ V자 건수 = 패턴 감지 횟수 (signal_log V자와 다름 — 전략 미적용 기준)")
        for label, grp in [("V자", v_res), ("PR", pr_res), ("일반", gen_res)]:
            if not grp: continue
            w   = sum(1 for r in grp if r["net_5m"] > 0)
            avg = sum(r["net_5m"] for r in grp) / len(grp)
            lines.append(f"    {label:<6} {len(grp):>3}건  승률 {w/len(grp)*100:>3.0f}%  순수익 {avg:+.2f}%")

    # ── 5. 신호 목록 (최근 10건) ────────────────────────────────────
    lines.append("")
    lines.append("  [ 신호 목록 (최근 10건) ]")
    for r in results[-10:]:
        a5  = f"{r['after_5m']:+.1f}%" if r["after_5m"] is not None else "  ?"
        n5  = f"{r['net_5m']:+.1f}%"   if r["net_5m"]  is not None else "  ?"
        v   = "V" if r["is_v"] else ("PR" if r["is_pr"] else "-")
        lines.append(
            f"    {r['entry_time'][8:12]}  {r['entry_price']:,}원"
            f"  [{v}]  5m:{a5}(순{n5})"
            f"  MFE:{r['mfe']:+.1f}%  MAE:{r['mae']:+.1f}%"
        )

    sep()
    return "\n".join(lines)


def build_summary(all_results: dict[str, list[dict]], actual_buys: set = None) -> str:
    """전 종목 요약 리포트를 생성합니다."""
    lines = ["═" * 60, "  📋 전 종목 요약", "═" * 60, ""]

    risk_symbols = []
    for symbol, results in sorted(all_results.items()):
        if not results:
            continue
        net_5m = [r["net_5m"] for r in results if r["net_5m"] is not None]
        maes   = [r["mae"] for r in results]
        if not net_5m:
            continue
        win  = sum(1 for v in net_5m if v > 0)
        avg  = sum(net_5m) / len(net_5m)
        mae  = sum(maes) / len(maes)
        high_risk, _ = is_high_risk(results)
        flag = "  ⚠️ 고위험" if high_risk else ""
        # 2026-07-16: 기존엔 신호 건수를 len(results)(net_5m=None인 장마감
        # 근처 봉까지 포함)로 표시하면서, 정작 옆의 평균은 net_5m만으로
        # 계산해서 분자·분모가 안 맞았음(004310 사례: 여기 13건 vs
        # "놓친 기회" 섹션 8건 — 같은 종목이 다른 숫자로 두 번 나와
        # 리포트 신뢰도 문제 유발). "놓친 기회"와 동일하게 len(net_5m)로 통일.
        lines.append(
            f"  {symbol}  신호 {len(net_5m):>3}건  "
            f"5분순수익 {avg:+.2f}%  MAE {mae:+.2f}%{flag}"
        )
        if high_risk:
            risk_symbols.append(symbol)

    if risk_symbols:
        lines.append("")
        lines.append("  ⚠️  고위험 종목 (조건검색 제외 검토):")
        for sym in risk_symbols:
            lines.append(f"      → {sym}")

    # ── 놓친 기회 (missed opportunity) ────────────────────────
    # replay 5분 순수익이 좋은데(평균 +0.5%↑) 실제로 매수 안 한 종목
    if actual_buys is not None:
        MIN_NET_5M = 0.5
        MIN_SIGNALS = 3
        missed = []
        for symbol, results in all_results.items():
            if symbol in actual_buys:
                continue
            net_5m = [r["net_5m"] for r in results if r["net_5m"] is not None]
            if len(net_5m) < MIN_SIGNALS:
                continue
            avg = sum(net_5m) / len(net_5m)
            if avg >= MIN_NET_5M:
                win = sum(1 for v in net_5m if v > 0)
                missed.append((symbol, len(net_5m), avg, win))
        if missed:
            missed.sort(key=lambda x: -x[2])
            lines.append("")
            lines.append("  💡 놓친 기회 (replay 양호 + 실제 미매수):")
            lines.append(f"      (기준: 5분순수익 평균 +{MIN_NET_5M}%+, 신호 {MIN_SIGNALS}건+)")
            for sym, cnt, avg, win in missed[:10]:
                lines.append(
                    f"      → {sym}  신호 {cnt}건  5분순수익 {avg:+.2f}%  승률 {win}/{cnt}"
                )
        else:
            lines.append("")
            lines.append("  💡 놓친 기회: 없음 (replay 양호한 미매수 종목 없음)")

    lines.append("─" * 60)
    return "\n".join(lines)


def load_actual_buys(target_date: date) -> set:
    """trades.csv에서 해당 날짜에 실제 매수한 종목 집합을 반환합니다."""
    trades_path = Path("logs/trades.csv")
    if not trades_path.exists():
        return set()
    buys = set()
    try:
        with trades_path.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                ts = r.get("timestamp", "")
                if ts[:10] != target_date.isoformat():
                    continue
                if r.get("side") == "BUY" and r.get("accepted") == "True":
                    buys.add(r.get("symbol", ""))
    except Exception:
        pass
    return buys


def _force_utf8_stdout() -> None:
    """Windows 콘솔 한글 깨짐 방지 — 직접 실행할 때만 적용.

    2026-08-07 (1J.2): 모듈 최상위에서 sys.stdout을 교체하면
    이 모듈을 import하는 쪽(테스트 등)의 stdout까지 닫혀
    ValueError: I/O operation on closed file이 발생합니다.
    """
    import io as _io, sys as _sys
    _sys.stdout = _io.TextIOWrapper(_sys.stdout.buffer, encoding="utf-8", errors="replace")


def main():
    _force_utf8_stdout()
    # 2026-08-07 (1J.3.1): fallback은 명시적으로 요청할 때만 허용.
    global MINUTE_BAR_COUNT, ANALYZER_MODE
    allow_simple = "--allow-simple-fallback" in sys.argv
    allow_config = "--allow-config-fallback" in sys.argv
    skip_errors = "--skip-analyzer-errors" in sys.argv
    for _flag in ("--allow-simple-fallback", "--allow-config-fallback",
                  "--skip-analyzer-errors"):
        while _flag in sys.argv:
            sys.argv.remove(_flag)
    MINUTE_BAR_COUNT = resolve_minute_bar_count(allow_fallback=allow_config)

    args        = sys.argv[1:]
    today       = date.today()
    target_date = date.fromisoformat(args[0]) if args else today
    filter_sym  = args[1].upper() if len(args) >= 2 else None

    date_dir = MINUTE_BARS_DIR / target_date.strftime("%Y%m%d")
    if not date_dir.exists():
        print(f"[ERROR] 저장된 분봉 없음: {date_dir}")
        sys.exit(1)

    csv_files = sorted(date_dir.glob("*.csv"))
    if filter_sym:
        csv_files = [f for f in csv_files if f.stem == filter_sym]
    if not csv_files:
        print("[ERROR] 해당 종목의 분봉 데이터 없음")
        sys.exit(1)

    analyzer = try_import_analyzer(allow_simple_fallback=allow_simple)
    stats = reset_replay_quality_stats()
    stats.analyzer_mode = "LIVE_MINUTE_ANALYZER" if analyzer is not None else "SIMPLE_FALLBACK"
    all_reports = []
    all_results: dict[str, list[dict]] = {}

    for csv_path in csv_files:
        symbol  = csv_path.stem
        bars    = load_bars(symbol, target_date)
        if not bars:
            continue
        results = run_replay(symbol, bars, analyzer, target_date,
                             minute_bar_count=MINUTE_BAR_COUNT,
                             quality_stats=stats,
                             skip_analyzer_errors=skip_errors)
        all_results[symbol] = results
        report  = format_report(symbol, results, target_date)
        print(report)
        all_reports.append(report)

    # 전 종목 요약
    if not filter_sym and len(all_results) > 1:
        actual_buys = load_actual_buys(target_date)
        summary = build_summary(all_results, actual_buys)
        print(summary)
        all_reports.append(summary)

    REPORTS_DIR.mkdir(exist_ok=True)
    fname = REPORTS_DIR / f"replay_{target_date.strftime('%Y%m%d')}.txt"
    # 2026-08-07 (1J.3.1): 데이터 품질을 결과와 함께 남깁니다.
    quality = "\n".join(build_quality_report(stats))
    print(quality)
    all_reports.append(quality)
    fname.write_text("\n\n".join(all_reports), encoding="utf-8")
    print(f"\n  → 저장: {fname}")


if __name__ == "__main__":
    main()
