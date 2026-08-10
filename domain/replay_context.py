"""리플레이 시간축 공용 컨텍스트 (2026-08-07, 1J.3단계)

배경 — 리플레이가 실제 봇과 다른 전략을 돌리고 있었음
----------------------------------------------------
분봉 CSV에는 전일 꼬리 봉이 정상적으로 포함됩니다(실시간 봇이 최근
60봉을 받으면서 전일 봉이 섞이기 때문). 실측:

    분봉 CSV                1,781개
    대상일 외 날짜 봉 포함     920개 (51.7%)
    1분 초과 gap 존재         821개 (46.1%)
    5분 이상 gap 존재         272개 (15.3%)
    예: data/minute_bars/20260623/005930.csv 첫 봉 = 20260622142200

문제는 리플레이가 이것을 다루는 방식이었습니다.

1) **전일 봉을 entry candidate로 평가** — `for i in range(5, len(bars))`
   가 전일 봉까지 순회해, 6/23 리플레이가 6/22 봉에서 BUY 신호를
   만들 수 있었음.
2) **live 60봉 vs replay 누적 전체** — 실제 `TradingService`는
   `broker.get_minute_bars(count=cfg.minute_bar_count)`(=60)를
   `MinuteAnalyzer`에 넘기는데, 리플레이는 `bars[:i]`라 오후에는
   200~400봉이 들어갔음. `MinuteAnalyzer`는 전달된 bars 전체로
   VWAP·day_high·day_low를 계산하므로 **다른 전략**이 됨.
3) **현재봉 누락** — `window=bars[:i]`, `current=bars[i]`라
   analyzer는 i-1까지만 보고 i 가격으로 진입. live는 최신 현재봉을
   포함한 60봉을 분석하므로 1봉 시차.
4) **prev_close가 파일 첫 봉** — `bars[0].close_price`는 전일
   "첫 저장봉"이지 전일 종가가 아님. 등락률 A조건에 직접 들어감.
5) **"5분 후"가 실제로는 "5개 봉 후"** — `idx = i + m`. gap이
   46.1% 파일에 있으므로 8분·15분·35분 후가 될 수 있음.

이 모듈은 위를 한 곳에서 바로잡아 replay_runner / v_drop / crash /
pullback이 **같은 시간축**을 쓰도록 합니다.

정책 (명시적 고정)
-----------------
- **candidate**: `cntr_tm` 날짜가 target_date인 봉에서만.
- **analysis window**: 현재봉 포함 최근 `minute_bar_count`개.
- **prev_close**: target_date 이전 마지막 봉의 close. 없으면
  `None`(임의 추정 금지) — 호출부가 skip/N-A 처리.
- **horizon 가격**: `entry_dt + m분` 시각 **이하의 가장 최신 봉**을
  mark-to-market으로 사용하되, 그 봉이 target 시각보다
  `MAX_STALENESS_MINUTES` 이상 오래됐으면 `None`.
  실제 경과 시간을 `actual_elapsed_minutes`로 함께 반환.
- **MFE/MAE**: `entry_dt < bar_dt <= entry_dt + N분` 범위. 날짜
  경계를 넘지 않음.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Sequence

# horizon 가격으로 인정할 최대 지연(분). target 시각 이하의 최신 봉을
# 쓰되, 이보다 오래 끊긴 봉이면 "그 시점 가격을 모른다"고 보고 None.
MAX_STALENESS_MINUTES = 3


def parse_bar_dt(cntr_tm: str) -> datetime | None:
    """`YYYYMMDDHHmmSS` 또는 `HHmmSS` 형식을 datetime으로."""
    t = str(cntr_tm or "").strip()
    try:
        if len(t) >= 14:
            return datetime.strptime(t[:14], "%Y%m%d%H%M%S")
        if len(t) == 6:
            return datetime.strptime(t, "%H%M%S")
    except ValueError:
        return None
    return None


def floor_to_minute(dt: datetime) -> datetime:
    """초·마이크로초를 버립니다.

    로그 timestamp가 `09:23:00.608`이고 분봉이 `09:23:00`이면
    단순 `>=` 비교로 09:24 봉이 선택되던 문제(1J.3 P0-5) 때문에
    분 단위로 내림한 뒤 매칭합니다.
    """
    return dt.replace(second=0, microsecond=0)



def _field(bar: Any, *names: str) -> Any:
    """봉 객체의 필드명을 흡수합니다.

    2026-08-07 (1J.3): 분석기마다 봉 클래스가 달라 필드명이
    다릅니다(replay_runner: close_price / crash: close). 공용
    컨텍스트가 어느 쪽에서도 동작하도록 후보를 순서대로 봅니다.
    """
    for n in names:
        v = getattr(bar, n, None)
        if v is not None:
            return v
    return None


def bar_close(bar: Any) -> Any:
    return _field(bar, "close_price", "close")


def bar_high(bar: Any) -> Any:
    return _field(bar, "high_price", "high")


def bar_low(bar: Any) -> Any:
    return _field(bar, "low_price", "low")


@dataclass
class HorizonPrice:
    """horizon 시점 가격과 실제 경과 시간."""

    price: int | None
    actual_elapsed_minutes: float | None
    bar_dt: datetime | None

    @property
    def available(self) -> bool:
        return self.price is not None


class ReplayDayContext:
    """하루치 분봉에 대한 시간축 컨텍스트.

    `all_bars`에는 전일 꼬리 봉이 포함될 수 있습니다 — 지표 history
    로는 쓰되 candidate로는 쓰지 않습니다.
    """

    def __init__(self, all_bars: Sequence[Any], target_date: date,
                 minute_bar_count: int = 60):
        self.all_bars = list(all_bars)
        self.target_date = target_date
        self.minute_bar_count = int(minute_bar_count)
        self._dts: list[datetime | None] = [parse_bar_dt(getattr(b, "cntr_tm", ""))
                                            for b in self.all_bars]

    # ── 날짜 경계 ────────────────────────────────────────────────
    def bar_dt(self, index: int) -> datetime | None:
        return self._dts[index] if 0 <= index < len(self._dts) else None

    def is_target_bar(self, index: int) -> bool:
        """이 봉이 target_date 소속인가 (= candidate 자격이 있는가)."""
        dt = self.bar_dt(index)
        return dt is not None and dt.date() == self.target_date

    @property
    def target_indices(self) -> list[int]:
        """candidate로 평가할 수 있는 인덱스 목록."""
        return [i for i in range(len(self.all_bars)) if self.is_target_bar(i)]

    @property
    def target_bars(self) -> list[Any]:
        return [self.all_bars[i] for i in self.target_indices]

    # ── prev_close ──────────────────────────────────────────────
    @property
    def previous_close(self) -> int | None:
        """target_date 이전 마지막 봉의 close. 없으면 None.

        `bars[0].close_price`는 전일 *첫* 저장봉이라 전일 종가가
        아닙니다. 이전 날짜 봉이 아예 없으면 임의 추정하지 않고
        None을 돌려 호출부가 skip/N-A로 처리하게 합니다.
        """
        for i in range(len(self.all_bars) - 1, -1, -1):
            dt = self.bar_dt(i)
            if dt is not None and dt.date() < self.target_date:
                return bar_close(self.all_bars[i])
        return None

    @property
    def previous_close_available(self) -> bool:
        return self.previous_close is not None

    # ── analysis window ─────────────────────────────────────────
    def analysis_window(self, current_index: int) -> list[Any]:
        """live와 동일한 "현재봉 포함 최근 minute_bar_count개".

        live: `get_minute_bars(count=60)` → 최신 60봉(현재봉 포함)
        기존 replay: `bars[:i]` → 현재봉 제외 + 개수 무제한
        """
        history_with_current = self.all_bars[: current_index + 1]
        return history_with_current[-self.minute_bar_count:]

    # ── horizon / MFE·MAE ───────────────────────────────────────
    def price_at_horizon(self, entry_dt: datetime, minutes: int) -> HorizonPrice:
        """`entry_dt + minutes` 시점의 mark-to-market 가격.

        target 시각 **이하**의 가장 최신 target_date 봉을 씁니다.
        그 봉이 target 시각보다 `MAX_STALENESS_MINUTES` 이상
        오래됐으면 가격을 모른다고 보고 None.
        """
        target = entry_dt + timedelta(minutes=minutes)
        best_i = None
        for i in self.target_indices:
            dt = self._dts[i]
            if dt is None or dt <= entry_dt:
                continue
            if dt <= target:
                best_i = i
            else:
                break
        if best_i is None:
            return HorizonPrice(None, None, None)
        dt = self._dts[best_i]
        staleness = (target - dt).total_seconds() / 60.0
        if staleness > MAX_STALENESS_MINUTES:
            return HorizonPrice(None, None, None)
        elapsed = (dt - entry_dt).total_seconds() / 60.0
        return HorizonPrice(bar_close(self.all_bars[best_i]),
                            elapsed, dt)

    def bars_between(self, entry_dt: datetime, end_dt: datetime) -> list[Any]:
        """`entry_dt < bar_dt <= end_dt` 범위의 target_date 봉."""
        out = []
        for i in self.target_indices:
            dt = self._dts[i]
            if dt is not None and entry_dt < dt <= end_dt:
                out.append(self.all_bars[i])
        return out

    def mfe_mae(self, entry_dt: datetime, entry_price: float,
                minutes: int = 20) -> tuple[float, float]:
        """clock-time 기준 MFE/MAE(%). 봉 개수가 아니라 시간 범위."""
        window = self.bars_between(entry_dt, entry_dt + timedelta(minutes=minutes))
        if not window or not entry_price:
            return 0.0, 0.0
        highs = [bar_high(b) or 0 for b in window]
        lows = [bar_low(b) or 0 for b in window]
        mfe = (max(highs) - entry_price) / entry_price * 100
        mae = (min(lows) - entry_price) / entry_price * 100
        return mfe, mae

    # ── 이벤트 시각 → 봉 매칭 ────────────────────────────────────
    def index_at_or_after(self, when: datetime) -> int | None:
        """`when` 이후(같은 분 포함)의 첫 target_date 봉 인덱스.

        `when`은 분 단위로 내림해서 비교합니다 — 로그의
        `09:23:00.608`이 `09:23:00` 봉과 매칭되도록(1J.3 P0-5).
        **날짜를 버리고 시각만 비교하지 않습니다.**
        """
        floored = floor_to_minute(when)
        for i in self.target_indices:
            dt = self._dts[i]
            if dt is not None and dt >= floored:
                return i
        return None


# ── prev_close 복원 (1J.3.1) ─────────────────────────────────────
# 실측: 누적 분봉 1,781 symbol-day 중 같은 CSV에서 prev_close를
# 얻을 수 있는 건 920개(51.7%)뿐. 나머지를 전부 제외하면 51일
# 분석이 표본의 절반을 잃습니다. 직전 거래일 폴더까지 보면
# 69.7%까지 올라갑니다(실측).
#
# 중요: 추정값을 **조용히** 쓰지 않습니다. 어디서 왔는지
# (source)와 신뢰도(confidence)를 항상 함께 반환해 리포트에
# 노출합니다.
PREV_CLOSE_SAME_FILE = "SAME_FILE_PRETARGET"
# 2026-08-07 (1J.3.2): 이름 정정. 실제 거래소 캘린더를 쓰지 않으므로
# "trading day"라고 단정할 수 없음 — 우리가 아는 건 "데이터가 있는
# 직전 날짜"뿐입니다.
PREV_CLOSE_PREV_DAY = "PREVIOUS_DATA_DAY_EOD"
PREV_CLOSE_PREV_DAY_PARTIAL = "PREVIOUS_DATA_DAY_PARTIAL"   # 사용 금지 표시용

# 2026-08-07 (1J.3.4, 재현 확인): 이전 데이터 날짜 파일의 마지막 봉이
# 곧 전일 종가라는 보장이 없습니다. MinuteBarSaver는 그 종목을 감시할
# 때 받은 60봉만 병합 저장하므로, 전날 오전에 잠깐 감시했다면 파일도
# 오전에 끝납니다.
#
# 실측 (PREVIOUS_DATA_DAY 후보 321건의 이전 파일 마지막 봉):
#   12:00 이전   154건 / 12:01~14:00  47건 / 14:01~14:59  31건
#   15:00~15:14   19건 / 15:15 이후   70건
#   → 251건(78%)이 15:15 이전. 예: 5/29 파일 마지막 09:03
#
# 반면 same-file pretarget(당일 API가 받아 저장한 전일 봉)의 마지막
# 시각은 15:35가 868건, 15:30이 46건으로 장 마감부까지 이어집니다.
# 따라서 EOD 판정 기준을 15:30으로 잡습니다(보수적).
EOD_CUTOFF_HHMM = "1530"
PREV_CLOSE_SIGNAL_INFERRED = "SIGNAL_LOG_INFERRED"
PREV_CLOSE_UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class PrevCloseResult:
    value: int | None
    source: str
    confidence: str = "high"
    previous_data_date: date | None = None
    calendar_gap_days: int | None = None
    # 1J.3.4: signal_log 역산 시 근거를 함께 기록
    n_rows: int | None = None
    spread_pct: float | None = None

    @property
    def available(self) -> bool:
        return self.value is not None


def _previous_day_eod_bars(symbol: str, target_date: date, load_bars_fn,
                           minute_bars_dir, minute_bar_count: int):
    """직전 데이터 날짜 파일이 **장 마감부까지** 있으면 그 target 봉들을.

    (bars, prev_date, is_eod, last_hhmm) 반환. 파일이 없으면 (None, ...).
    """
    from pathlib import Path as _P
    root = _P(minute_bars_dir)
    days = sorted(d.name for d in root.iterdir()
                  if d.is_dir() and d.name.isdigit() and len(d.name) == 8)
    prior = [d for d in days if d < target_date.strftime("%Y%m%d")]
    if not prior or not (root / prior[-1] / f"{symbol}.csv").exists():
        return None, None, False, None
    prev_date = datetime.strptime(prior[-1], "%Y%m%d").date()
    pbars = load_bars_fn(symbol, prev_date)
    if not pbars:
        return None, prev_date, False, None
    tb = ReplayDayContext(pbars, prev_date, minute_bar_count).target_bars
    if not tb:
        return None, prev_date, False, None
    last = str(getattr(tb[-1], "cntr_tm", ""))[8:12]
    return tb, prev_date, (last >= EOD_CUTOFF_HHMM), last


def infer_prev_close_from_signal_log(symbol: str, target_date: date,
                                     signal_rows) -> PrevCloseResult:
    """signal_log의 price/change_rate_pct로 live의 previous_close를 역산.

        prev_close ≈ price / (1 + change_rate_pct / 100)

    live는 minute CSV가 아니라 시세 API의 `market_price.previous_close`
    를 쓰므로, 잘 검증된 역산값이 장중에 끝난 전일 파일보다 오히려
    실제 값에 가깝습니다. 다만 이상치가 있으므로 **엄격한 일관성
    조건**을 통과할 때만 사용합니다.
      - 유효 행 5개 이상
      - (max-min)/median <= 0.2%
    """
    vals = []
    for r in signal_rows:
        try:
            price = float(r.get("price") or 0)
            chg = float(r.get("change_rate_pct"))
        except (TypeError, ValueError):
            continue
        denom = 1.0 + chg / 100.0
        if price <= 0 or denom <= 0:
            continue
        vals.append(price / denom)
    if len(vals) < SIGNAL_INFER_MIN_ROWS:
        return PrevCloseResult(None, PREV_CLOSE_UNAVAILABLE, "none",
                               n_rows=len(vals))
    vals.sort()
    median = vals[len(vals) // 2]
    spread = (vals[-1] - vals[0]) / median * 100 if median else 999.0
    if spread > SIGNAL_INFER_MAX_SPREAD_PCT:
        return PrevCloseResult(None, PREV_CLOSE_UNAVAILABLE, "none",
                               n_rows=len(vals), spread_pct=spread)
    return PrevCloseResult(int(round(median)), PREV_CLOSE_SIGNAL_INFERRED,
                           "high", n_rows=len(vals), spread_pct=spread)


SIGNAL_INFER_MIN_ROWS = 5
SIGNAL_INFER_MAX_SPREAD_PCT = 0.2


def resolve_prev_close(ctx: "ReplayDayContext", symbol: str,
                       load_bars_fn=None, minute_bars_dir=None,
                       target_date: date | None = None,
                       signal_rows=None) -> PrevCloseResult:
    """우선순위에 따라 prev_close를 복원합니다.

    1순위 동일 CSV의 target_date 이전 마지막 close (confidence=high)
    2순위 **바로 직전 데이터 날짜** 폴더의 동일 종목 마지막 당일 close
          (confidence=medium)
    3순위 (미구현) signal_log price/change_rate_pct 역산 — 이상치가
          있어 무조건 쓰면 안 되므로 별도 조사 후 도입
    4순위 UNAVAILABLE

    ── 2026-08-07 (1J.3.2, 재현 확인) ──
    1J.3.1의 2순위는 파일을 찾을 때까지 **과거를 무제한으로 거슬러**
    올라갔습니다. 실측 결과 2홉 이상 거슬러 간 것이 280건이었고
    최대 **36 데이터 날짜 전** 종가를 전일 종가로 사용했습니다:

        1홉 전  322건  ← 유일하게 허용
        2홉 전   97건 / 3홉 53건 / ... / 36홉 1건   (오염 280건)

    그 결과 보고했던 coverage 85.4% 중 15.7%p가 오염된 표본이었고,
    등락률·A조건이 완전히 틀어질 수 있었습니다. **틀린 데이터로
    85%를 채우는 것보다 정확한 70%가 낫습니다.**

    이제 **바로 직전 데이터 날짜 딱 하나만** 확인하고, 거기에 해당
    종목 파일이 없으면 UNAVAILABLE입니다.

    주말·공휴일을 건너뛰면 calendar gap이 3~4일일 수 있으므로 gap
    자체로 오류 판정하지 않고 `calendar_gap_days`로 기록만 합니다.
    """
    # 1순위
    v = ctx.previous_close
    if v is not None:
        return PrevCloseResult(v, PREV_CLOSE_SAME_FILE, "high")

    # 2순위 — signal_log 역산 (엄격한 일관성 통과 시)
    # 1J.3.4: 장중에 끝난 전일 파일보다 이쪽이 live의 실제
    # previous_close에 더 가깝습니다.
    if signal_rows:
        inferred = infer_prev_close_from_signal_log(symbol, target_date, signal_rows)
        if inferred.available:
            return inferred

    # 3순위 — 바로 직전 데이터 날짜 1곳, **EOD까지 있는 경우만**
    if load_bars_fn is not None and minute_bars_dir is not None and target_date is not None:
        tb, prev_date, is_eod, last = _previous_day_eod_bars(
            symbol, target_date, load_bars_fn, minute_bars_dir, ctx.minute_bar_count)
        if tb:
            if not is_eod:
                # 장중에 끝난 파일 — 마지막 가격이 전일 종가가 아님
                return PrevCloseResult(
                    None, PREV_CLOSE_PREV_DAY_PARTIAL, "none",
                    previous_data_date=prev_date,
                    calendar_gap_days=(target_date - prev_date).days)
            return PrevCloseResult(
                bar_close(tb[-1]), PREV_CLOSE_PREV_DAY, "medium",
                previous_data_date=prev_date,
                calendar_gap_days=(target_date - prev_date).days)

    return PrevCloseResult(None, PREV_CLOSE_UNAVAILABLE, "none")


# ── history 완전성 (1J.3.3) ──────────────────────────────────────
# prev_close 숫자만 복원하고 60봉 history는 복원하지 않으면,
# 같은 09:05에도 live는 60봉(전일 오후 + 당일), replay는 6봉을
# analyzer에 넣게 됩니다. VWAP·고저가·pullback·V/PR·MA5·거래량
# 평균이 전부 달라지므로, 1J.5에서 낮은 일치율이 나와도 그게
# 계산식 차이인지 history 결손인지 구분할 수 없습니다.
#
# 실측(누적 1,781 symbol-day): full-window candidate 비율 61.4%.
# 2026-08-07 (1J.3.4): pretarget이 1봉만 있어도 COMPLETE였으므로
# 명칭이 과했음. 실제 full 여부는 is_full_window()가 판정합니다.
HISTORY_SAME_FILE_COMPLETE = "SAME_FILE_HISTORY_PRESENT"
HISTORY_PREVIOUS_DAY_RECONSTRUCTED = "PREVIOUS_DAY_RECONSTRUCTED"
HISTORY_INCOMPLETE_INTRADAY = "INCOMPLETE_INTRADAY"
# 장초 시작이지만 이전 파일이 장중에 끝나 붙일 수 없는 경우
HISTORY_INCOMPLETE_PREVIOUS_DAY = "INCOMPLETE_PREVIOUS_DAY"

# 장 시작 부근으로 인정할 첫 봉 시각. 이보다 늦게 시작한 파일은
# 그 앞의 **당일** 봉을 모르는 것이므로 전일 봉으로 채우면 안 됩니다.
MARKET_OPEN_TOLERANCE_HHMM = "0902"


@dataclass(frozen=True)
class ReplayContextBuildResult:
    """컨텍스트와 그 출처를 함께 반환합니다 (1J.3.4).

    prepend한 봉이 `ctx.all_bars`에 들어가면 `resolve_prev_close(ctx)`
    를 다시 불렀을 때 `SAME_FILE_PRETARGET / high`로 **둔갑**합니다.
    수익률 숫자는 그대로지만 1J.5에서 source별 fidelity를 볼 때
    잘못된 결론이 나므로, 처음 결정한 provenance를 덮어쓰지 않고
    함께 돌려줍니다.
    """

    ctx: "ReplayDayContext"
    history_status: str
    prev_close: PrevCloseResult

    def __iter__(self):
        # 기존 (ctx, history_status) 언패킹 하위호환
        return iter((self.ctx, self.history_status))


def build_day_context(symbol: str, bars, target_date: date,
                      minute_bar_count: int = 60,
                      load_bars_fn=None, minute_bars_dir=None,
                      signal_rows=None) -> ReplayContextBuildResult:
    """history 완전성을 고려해 컨텍스트를 만듭니다.

    A) same-file에 pretarget이 있으면 그대로 → SAME_FILE_HISTORY_PRESENT
    B) 첫 봉이 장 시작 부근(<=09:02)이고 pretarget이 없으며,
       직전 데이터 날짜 파일이 **EOD까지 존재**하고 tail이 충분하면
       필요한 만큼만 prepend → PREVIOUS_DAY_RECONSTRUCTED
       (EOD가 아니면 → INCOMPLETE_PREVIOUS_DAY, 붙이지 않음)
    C) 첫 봉이 장중이면 아무것도 채우지 않음 → INCOMPLETE_INTRADAY

    2026-08-07 (1J.3.4): B에 EOD 검증을 추가했습니다. 실측상 직전
    파일의 78%가 15:15 이전에 끝나며, 예컨대 7/30 파일이 10:08에
    끝났는데 그 tail을 7/31 09:01의 live history로 붙이면 실제 live가
    본 7/30 장 마감 직전 60봉과 전혀 다른 데이터가 됩니다.
    """
    ctx = ReplayDayContext(bars, target_date, minute_bar_count)
    if ctx.previous_close is not None:
        return ReplayContextBuildResult(
            ctx, HISTORY_SAME_FILE_COMPLETE,
            PrevCloseResult(ctx.previous_close, PREV_CLOSE_SAME_FILE, "high"))

    # same-file에 없으면 외부 경로로 prev_close를 먼저 확정(=provenance 고정)
    pc = resolve_prev_close(ctx, symbol, load_bars_fn, minute_bars_dir,
                            target_date, signal_rows)

    ti = ctx.target_indices
    if not ti:
        return ReplayContextBuildResult(ctx, HISTORY_INCOMPLETE_INTRADAY, pc)
    first_dt = ctx.bar_dt(ti[0])
    if first_dt is None or first_dt.strftime("%H%M") > MARKET_OPEN_TOLERANCE_HHMM:
        return ReplayContextBuildResult(ctx, HISTORY_INCOMPLETE_INTRADAY, pc)
    if load_bars_fn is None or minute_bars_dir is None:
        return ReplayContextBuildResult(ctx, HISTORY_INCOMPLETE_INTRADAY, pc)

    tail, prev_date, is_eod, _last = _previous_day_eod_bars(
        symbol, target_date, load_bars_fn, minute_bars_dir, minute_bar_count)
    if not tail:
        return ReplayContextBuildResult(ctx, HISTORY_INCOMPLETE_INTRADAY, pc)
    if not is_eod:
        # 장중에 끝난 전일 파일 — 붙이면 또 다른 오염
        return ReplayContextBuildResult(ctx, HISTORY_INCOMPLETE_PREVIOUS_DAY, pc)

    need = max(0, minute_bar_count - 1)
    if len(tail) < need:
        return ReplayContextBuildResult(ctx, HISTORY_INCOMPLETE_PREVIOUS_DAY, pc)

    merged = list(tail[-need:]) + list(bars)
    return ReplayContextBuildResult(
        ReplayDayContext(merged, target_date, minute_bar_count),
        HISTORY_PREVIOUS_DAY_RECONSTRUCTED, pc)


def is_full_window(ctx: "ReplayDayContext", index: int) -> bool:
    """이 시점의 analyzer window가 live와 같은 크기인가."""
    return len(ctx.analysis_window(index)) >= ctx.minute_bar_count
