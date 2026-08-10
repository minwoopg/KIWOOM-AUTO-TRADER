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
PREV_CLOSE_PREV_DAY = "PREVIOUS_DATA_DAY_FILE"
PREV_CLOSE_SIGNAL_INFERRED = "SIGNAL_LOG_INFERRED"
PREV_CLOSE_UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class PrevCloseResult:
    value: int | None
    source: str
    confidence: str = "high"
    previous_data_date: date | None = None
    calendar_gap_days: int | None = None

    @property
    def available(self) -> bool:
        return self.value is not None


def resolve_prev_close(ctx: "ReplayDayContext", symbol: str,
                       load_bars_fn=None, minute_bars_dir=None,
                       target_date: date | None = None) -> PrevCloseResult:
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

    # 2순위 — 바로 직전 데이터 날짜 1곳만
    if load_bars_fn is not None and minute_bars_dir is not None and target_date is not None:
        from pathlib import Path as _P
        root = _P(minute_bars_dir)
        days = sorted(d.name for d in root.iterdir()
                      if d.is_dir() and d.name.isdigit() and len(d.name) == 8)
        key = target_date.strftime("%Y%m%d")
        prior = [d for d in days if d < key]
        if prior:
            previous_day = prior[-1]          # 바로 직전 데이터 날짜만
            if (root / previous_day / f"{symbol}.csv").exists():
                prev_date = datetime.strptime(previous_day, "%Y%m%d").date()
                pbars = load_bars_fn(symbol, prev_date)
                if pbars:
                    pctx = ReplayDayContext(pbars, prev_date, ctx.minute_bar_count)
                    tb = pctx.target_bars
                    if tb:
                        return PrevCloseResult(
                            bar_close(tb[-1]), PREV_CLOSE_PREV_DAY, "medium",
                            previous_data_date=prev_date,
                            calendar_gap_days=(target_date - prev_date).days,
                        )

    return PrevCloseResult(None, PREV_CLOSE_UNAVAILABLE, "none")
