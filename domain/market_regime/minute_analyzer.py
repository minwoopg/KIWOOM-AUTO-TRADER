from __future__ import annotations

"""분봉 분석기 (Minute Bar Analyzer) — v2.

변경 이력:
    v2: V자 반등 / 눌림목 재상승 감지 보완
        - 순서 검증: high_idx < low_idx (고점→저점→현재 순서 강제)
        - 저점 나이 제한: v_low_min_age ~ v_low_max_age 봉 이내
        - 추격매수 방지: 반등폭 상한 v_max_rebound_pct
        - 저점봉 거래량 spike 감지
        - 현재봉 거래대금 최소 기준 v_min_bar_amount
        - MA5 기울기 상승 확인
        - PR 조건: MA5>MA20 상태에서만 인정
        - PR 조건 완화: 4봉 연속 우상향 → 최근 N봉 중 3봉 이상 우상향
        - V자/PR 중복 점수 방지 (elif 처리는 전략 레이어에서)
"""

from dataclasses import dataclass
from domain.models import MinuteBar


@dataclass
class MinuteAnalysis:
    """분봉 분석 결과를 담는 객체입니다."""

    vwap: float
    price_above_vwap: bool
    low_rising: bool
    pullback_pct: float
    is_valid_pullback: bool
    change_rate_pct: float
    is_valid_change_rate: bool
    rebound_pct: float
    is_valid_rebound: bool
    trading_value: int
    is_valid_trading_value: bool
    day_high: int
    day_low: int
    is_valid_pulldown: bool
    ma5_above_ma20: bool

    # ── V자 반등 ────────────────────────────────────────────────
    is_v_rebound: bool          # V자 반등 완성 여부
    v_bottom_k: int             # 저점이 현재 봉 기준 몇 봉 전 (0=미감지)
    v_drop_pct: float           # 고점→저점 낙폭 (%)
    v_rise_pct: float           # 저점→현재 반등폭 (%)
    v_volume_ratio: float       # 반등 구간 / 하락 구간 거래량 비율
    v_bottom_spike: bool        # 저점봉 순간 거래량 급등 여부 (투매 확인용 보조)
    v_ma5_rising: bool          # MA5 기울기 상승 여부
    rebound_volume_spike: bool  # 현재 반등봉 거래량 급등 여부 (매수세 유입 핵심 지표)
    upside_to_recent_high_pct: float  # 현재가→최근 고점까지 상승 여력 (%)

    # ── 눌림목 재상승 ────────────────────────────────────────────
    is_pulldown_recovery: bool  # 눌림목 후 재상승 여부
    pr_low_turning: bool        # 저점 우상향 전환 여부
    pr_volume_expanding: bool   # 거래량 수축→팽창 전환 여부

    def score(self) -> int:
        """진입 타이밍 점수를 계산합니다 (0~5점)."""
        return sum([
            self.price_above_vwap,
            self.low_rising,
            self.is_valid_pullback,
            self.is_valid_change_rate or self.is_valid_rebound or self.is_valid_pulldown,
            self.is_valid_trading_value,
        ])

    def summary(self) -> str:
        """로그용 요약 문자열을 반환합니다."""
        v_tag  = (
            f"V자 {'✓' if self.is_v_rebound else '✗'}"
            f"(낙폭{self.v_drop_pct:+.1f}% 반등{self.v_rise_pct:+.1f}%"
            f" 거래량x{self.v_volume_ratio:.1f} spike:{'✓' if self.v_bottom_spike else '✗'}"
            f" 반등spike:{'✓' if self.rebound_volume_spike else '✗'}"
            f" MA5상승:{'✓' if self.v_ma5_rising else '✗'} {self.v_bottom_k}봉전"
            f" 여력{self.upside_to_recent_high_pct:+.1f}%)"
        )
        pr_tag = (
            f"눌림재상승 {'✓' if self.is_pulldown_recovery else '✗'}"
            f"(저점전환:{'✓' if self.pr_low_turning else '✗'}"
            f" 거래량팽창:{'✓' if self.pr_volume_expanding else '✗'})"
        )
        tags = [
            f"VWAP {'위✓' if self.price_above_vwap else '아래✗'}({self.vwap:,.0f})",
            f"저점 {'상승✓' if self.low_rising else '하락✗'}",
            f"눌림 {'적절✓' if self.is_valid_pullback else '불량✗'}({self.pullback_pct:+.1f}%)",
            f"등락 {'유효✓' if self.is_valid_change_rate else '무효✗'}({self.change_rate_pct:+.1f}%)",
            f"반등 {'유효✓' if self.is_valid_rebound else '무효✗'}({self.rebound_pct:+.1f}%)",
            f"눌림목C {'유효✓' if self.is_valid_pulldown else '무효✗'}(MA5>MA20:{'✓' if self.ma5_above_ma20 else '✗'})",
            v_tag,
            pr_tag,
            f"거래대금 {'충분✓' if self.is_valid_trading_value else '부족✗'}({self.trading_value//100_000_000}억)",
        ]
        return " | ".join(tags)


class MinuteAnalyzer:
    """분봉 데이터를 분석하는 클래스입니다."""

    def __init__(
        self,
        min_trading_value: int = 1_000_000_000,
        pullback_min_pct: float = -7.0,
        pullback_max_pct: float = -1.0,
        change_rate_min: float = 2.0,
        change_rate_max: float = 18.0,
        low_rising_bars: int = 3,
        rebound_min_pct: float = 2.0,
        # V자 파라미터
        v_bottom_lookback: int = 30,
        v_low_min_age: int = 1,
        v_low_max_age: int = 5,
        v_drop_threshold_pct: float = -3.0,
        v_rebound_threshold_pct: float = 2.0,
        v_max_rebound_pct: float = 6.0,
        v_volume_ratio: float = 1.5,
        v_min_bar_amount: int = 30_000_000,
        v_bottom_spike_ratio: float = 1.5,
        v_ma5_slope_bars: int = 3,
    ) -> None:
        self.min_trading_value       = min_trading_value
        self.pullback_min_pct        = pullback_min_pct
        self.pullback_max_pct        = pullback_max_pct
        self.change_rate_min         = change_rate_min
        self.change_rate_max         = change_rate_max
        self.low_rising_bars         = low_rising_bars
        self.rebound_min_pct         = rebound_min_pct
        self.pulldown_min_pct        = -8.0
        self.pulldown_max_pct        = -1.0
        self.v_bottom_lookback       = v_bottom_lookback
        self.v_low_min_age           = v_low_min_age
        self.v_low_max_age           = v_low_max_age
        self.v_drop_threshold_pct    = v_drop_threshold_pct
        self.v_rebound_threshold_pct = v_rebound_threshold_pct
        self.v_max_rebound_pct       = v_max_rebound_pct
        self.v_volume_ratio          = v_volume_ratio
        self.v_min_bar_amount        = v_min_bar_amount
        self.v_bottom_spike_ratio    = v_bottom_spike_ratio
        self.v_ma5_slope_bars        = v_ma5_slope_bars

    # ── 내부 유틸 ────────────────────────────────────────────────

    def _ma5_rising(self, bars: list[MinuteBar], slope_bars: int) -> bool:
        """현재 MA5가 직전 MA5보다 높은지 확인합니다 (기울기 상승 전환).

        V자 반등 구간 특성상 직전 구간까지 MA5가 하락할 수 있으므로
        slope_bars 전체 구간 비교 대신 현재 vs 직전 1봉 비교로 완화합니다.
        """
        closes = [b.close_price for b in bars if b.close_price > 0]
        if len(closes) < 6:
            return False
        ma5_cur  = sum(closes[-5:]) / 5
        ma5_prev = sum(closes[-6:-1]) / 5
        return ma5_cur > ma5_prev

    # ── V자 반등 감지 ────────────────────────────────────────────

    def _detect_v_rebound(
        self, bars: list[MinuteBar], current_price: int, vwap: float,
        ma5_above_ma20: bool, ma5_rising: bool,
    ) -> tuple[bool, int, float, float, float, bool, bool]:
        """V자 반등 패턴을 감지합니다.

        반환: (is_v, bottom_k, drop_pct, rise_pct, vol_ratio, bottom_spike, ma5_rising)

        핵심 설계 원칙:
            - Look-ahead Bias 방지: 현재 봉(bars[-1])은 저점 탐색에서 제외.
              현재 봉 = "반등 확인 봉" 역할.
            - 순서 검증: 탐색 구간 내 high_idx < low_idx 강제
              (고점 발생 → 저점 발생 → 현재 반등 순서가 깨지면 V자 아님)
            - 저점 나이 제한: v_low_min_age ~ v_low_max_age 봉 이내
            - 추격매수 방지: 반등폭 상한 v_max_rebound_pct
            - 현재봉 거래대금 최소 기준
            - 저점봉 순간 거래량 spike 감지
            - VWAP 회복 + MA5 기울기 상승 필수
        """
        n = len(bars)
        lookback = min(self.v_bottom_lookback, n - 2)
        if lookback < 3:
            return False, 0, 0.0, 0.0, 0.0, False, ma5_rising

        # 탐색 구간: 현재 봉 제외 (Look-ahead Bias 방지)
        search_bars = bars[-(lookback + 1):-1]

        # ── 저점 위치 탐색 ──────────────────────────────────────
        low_idx = min(range(len(search_bars)), key=lambda i: search_bars[i].low_price)
        bottom_price = search_bars[low_idx].low_price
        # 현재 봉 기준 몇 봉 전인지
        bottom_k = len(search_bars) - low_idx

        # 저점 나이 제한 (너무 오래된 저점, 너무 직전 저점 제외)
        if not (self.v_low_min_age <= bottom_k <= self.v_low_max_age):
            return False, bottom_k, 0.0, 0.0, 0.0, False, ma5_rising

        # ── 순서 검증: high_idx < low_idx ──────────────────────
        pre_bars = search_bars[:low_idx]
        if not pre_bars:
            return False, bottom_k, 0.0, 0.0, 0.0, False, ma5_rising
        high_idx = max(range(len(pre_bars)), key=lambda i: pre_bars[i].high_price)
        h_pre = pre_bars[high_idx].high_price
        # high_idx는 반드시 low_idx보다 앞에 있어야 함 (구조상 이미 보장)
        # pre_bars = search_bars[:low_idx] 이므로 high_idx < low_idx 자동 성립
        if h_pre <= 0 or h_pre <= bottom_price:
            return False, bottom_k, 0.0, 0.0, 0.0, False, ma5_rising

        # ── 낙폭 계산 ───────────────────────────────────────────
        drop_pct = (bottom_price - h_pre) / h_pre * 100  # 음수

        # ── 반등폭 계산 (상한/하한 동시 체크) ───────────────────
        if bottom_price <= 0:
            return False, bottom_k, drop_pct, 0.0, 0.0, False, ma5_rising
        rise_pct = (current_price - bottom_price) / bottom_price * 100

        # ── 거래량 비율 ─────────────────────────────────────────
        fall_bars_seg  = search_bars[:low_idx + 1]           # 하락 구간 + 저점봉
        rise_bars_seg  = list(search_bars[low_idx + 1:]) + [bars[-1]]  # 반등 구간 + 현재봉

        avg_fall_vol = sum(b.volume for b in fall_bars_seg) / len(fall_bars_seg) if fall_bars_seg else 1
        avg_rise_vol = sum(b.volume for b in rise_bars_seg) / len(rise_bars_seg) if rise_bars_seg else 0
        vol_ratio = avg_rise_vol / avg_fall_vol if avg_fall_vol > 0 else 0.0

        # ── 저점봉 순간 거래량 spike ────────────────────────────
        # 저점봉 거래량 vs 전후 봉 평균 비교
        around = []
        if low_idx > 0:
            around.append(search_bars[low_idx - 1].volume)
        if low_idx < len(search_bars) - 1:
            around.append(search_bars[low_idx + 1].volume)
        avg_around = sum(around) / len(around) if around else search_bars[low_idx].volume
        bottom_spike = (
            search_bars[low_idx].volume > avg_around * self.v_bottom_spike_ratio
            if avg_around > 0 else False
        )

        # ── 현재봉 거래대금 최소 기준 ───────────────────────────
        cur_bar_amount = bars[-1].volume * current_price
        bar_amount_ok = cur_bar_amount >= self.v_min_bar_amount

        # ── VWAP 회복 + MA5 기울기 ──────────────────────────────
        vwap_ok = current_price > vwap

        # ── 최종 판정 + 실패 사유 집계 ─────────────────────────
        fail_reasons = []
        if drop_pct > self.v_drop_threshold_pct:
            fail_reasons.append(f"낙폭부족({drop_pct:+.1f}%,최소{self.v_drop_threshold_pct:+.1f}%)")
        if rise_pct < self.v_rebound_threshold_pct:
            fail_reasons.append(f"반등부족({rise_pct:+.1f}%,최소{self.v_rebound_threshold_pct:+.1f}%)")
        elif rise_pct > self.v_max_rebound_pct:
            fail_reasons.append(f"반등과다({rise_pct:+.1f}%,최대{self.v_max_rebound_pct:+.1f}%)")
        if vol_ratio < self.v_volume_ratio:
            fail_reasons.append(f"거래량부족(x{vol_ratio:.1f},최소x{self.v_volume_ratio:.1f})")
        if not vwap_ok:
            fail_reasons.append("VWAP미회복")
        if not (ma5_rising or ma5_above_ma20):
            fail_reasons.append("MA5조건미충족")
        if not bar_amount_ok:
            fail_reasons.append(f"거래대금부족({cur_bar_amount//100_000_000}억)")

        is_v = len(fail_reasons) == 0

        # 실패 사유를 v_fail_reasons에 저장 (외부에서 접근 가능)
        self._last_v_fail_reasons = fail_reasons if not is_v else []

        return is_v, bottom_k, drop_pct, rise_pct, vol_ratio, bottom_spike, ma5_rising

    # ── 눌림목 재상승 감지 ───────────────────────────────────────

    def _detect_pulldown_recovery(
        self, bars: list[MinuteBar], ma5_above_ma20: bool,
    ) -> tuple[bool, bool, bool]:
        """눌림목 후 재상승 패턴을 감지합니다.

        PR은 MA5>MA20 상승 추세 안에서만 인정합니다.

        저점 우상향 조건 완화:
            - 기존: 4봉 연속 우상향 (1분봉에서 너무 엄격)
            - 변경: 최근 5봉 중 4봉 이상 저점 우상향 (1봉 예외 허용)
        """
        # PR은 반드시 MA5>MA20 상태에서만
        if not ma5_above_ma20:
            return False, False, False

        if len(bars) < 6:
            return False, False, False

        # 저점 우상향: 최근 5봉 중 4봉 이상 우상향 (1분봉 노이즈 허용)
        recent = bars[-5:]
        rising_count = sum(
            1 for i in range(1, len(recent))
            if recent[i].low_price > recent[i - 1].low_price
        )
        low_turning = rising_count >= 4  # 4/4 또는 4/4중 1봉 예외

        # 거래량 수축→팽창: 직전 3봉 평균 대비 현재봉 1.2배 이상
        prev_vols = [b.volume for b in bars[-4:-1]]
        avg_prev_vol = sum(prev_vols) / len(prev_vols) if prev_vols else 0
        cur_vol = bars[-1].volume
        volume_expanding = cur_vol > avg_prev_vol * 1.2

        is_pr = low_turning and volume_expanding

        return is_pr, low_turning, volume_expanding

    # ── 메인 분석 ────────────────────────────────────────────────

    def analyze(self, bars: list[MinuteBar], prev_close: int) -> MinuteAnalysis | None:
        if len(bars) < self.low_rising_bars + 1:
            return None

        current_price = bars[-1].close_price
        if current_price <= 0 or prev_close <= 0:
            return None

        # ── VWAP ────────────────────────────────────────────────
        total_pv  = sum(((b.high_price + b.low_price + b.close_price) / 3) * b.volume for b in bars)
        total_vol = sum(b.volume for b in bars)
        vwap = total_pv / total_vol if total_vol > 0 else current_price
        price_above_vwap = current_price > vwap

        # ── 당일 고가/저가 ───────────────────────────────────────
        day_high = max(b.high_price for b in bars)
        day_low  = min(b.low_price  for b in bars)

        # ── 눌림목 ───────────────────────────────────────────────
        pullback_pct      = (current_price - day_high) / day_high * 100
        is_valid_pullback = self.pullback_min_pct <= pullback_pct <= self.pullback_max_pct

        # ── 분봉 저점 상승 ───────────────────────────────────────
        recent_lows = [bars[-(i + 1)].low_price for i in range(self.low_rising_bars)]
        low_rising  = all(recent_lows[i] > recent_lows[i + 1] for i in range(len(recent_lows) - 1))

        # ── 등락률 ───────────────────────────────────────────────
        change_rate_pct      = (current_price - prev_close) / prev_close * 100
        is_valid_change_rate = self.change_rate_min <= change_rate_pct <= self.change_rate_max

        # ── B조건: 당일 저점 대비 반등률 ────────────────────────
        rebound_pct      = (current_price - day_low) / day_low * 100 if day_low > 0 else 0.0
        is_valid_rebound = rebound_pct >= self.rebound_min_pct and price_above_vwap

        # ── 거래대금 ─────────────────────────────────────────────
        trading_value          = bars[-1].acc_volume * current_price
        is_valid_trading_value = trading_value >= self.min_trading_value

        # ── C조건: 상승 추세 중 눌림목 ──────────────────────────
        closes         = [b.close_price for b in bars if b.close_price > 0]
        ma5            = sum(closes[-5:])  / min(5,  len(closes)) if closes else 0
        ma20           = sum(closes[-20:]) / min(20, len(closes)) if closes else 0
        ma5_above_ma20 = ma5 > ma20
        is_valid_pulldown = (
            self.pulldown_min_pct <= change_rate_pct <= self.pulldown_max_pct
            and ma5_above_ma20
            and price_above_vwap
        )

        # ── MA5 기울기 ───────────────────────────────────────────
        ma5_rising = self._ma5_rising(bars, self.v_ma5_slope_bars)

        # ── V자 반등 감지 ────────────────────────────────────────
        is_v, v_bottom_k, v_drop_pct, v_rise_pct, v_vol_ratio, v_bottom_spike, _ = (
            self._detect_v_rebound(bars, current_price, vwap, ma5_above_ma20, ma5_rising)
        )

        # ── 반등봉 거래량 spike (현재봉 거래량 vs 당일 평균) ────
        # 저점봉 spike(투매 확인)와 달리 반등봉 spike는 매수세 유입을 확인하는 핵심 지표
        avg_daily_vol = sum(b.volume for b in bars) / len(bars) if bars else 1
        rebound_volume_spike = (
            bars[-1].volume > avg_daily_vol * 2.0  # 당일 평균의 2배 이상
        )

        # ── 최근 고점까지 상승 여력 ─────────────────────────────
        # 차단 조건으로 쓰지 않고 로그 기록용으로만 사용
        # 데이터 축적 후 필터화 여부 결정 예정
        recent_high = max(b.high_price for b in bars[-30:]) if len(bars) >= 30 else day_high
        upside_to_recent_high_pct = (
            (recent_high - current_price) / current_price * 100
            if current_price > 0 else 0.0
        )

        # ── 눌림목 재상승 감지 (MA5>MA20 상태에서만) ────────────
        is_pr, pr_low_turning, pr_volume_expanding = (
            self._detect_pulldown_recovery(bars, ma5_above_ma20)
        )

        return MinuteAnalysis(
            vwap=vwap,
            price_above_vwap=price_above_vwap,
            low_rising=low_rising,
            pullback_pct=pullback_pct,
            is_valid_pullback=is_valid_pullback,
            change_rate_pct=change_rate_pct,
            is_valid_change_rate=is_valid_change_rate,
            rebound_pct=rebound_pct,
            is_valid_rebound=is_valid_rebound,
            trading_value=trading_value,
            is_valid_trading_value=is_valid_trading_value,
            day_high=day_high,
            day_low=day_low,
            is_valid_pulldown=is_valid_pulldown,
            ma5_above_ma20=ma5_above_ma20,
            is_v_rebound=is_v,
            v_bottom_k=v_bottom_k,
            v_drop_pct=v_drop_pct,
            v_rise_pct=v_rise_pct,
            v_volume_ratio=v_vol_ratio,
            v_bottom_spike=v_bottom_spike,
            v_ma5_rising=ma5_rising,
            rebound_volume_spike=rebound_volume_spike,
            upside_to_recent_high_pct=round(upside_to_recent_high_pct, 2),
            is_pulldown_recovery=is_pr,
            pr_low_turning=pr_low_turning,
            pr_volume_expanding=pr_volume_expanding,
        )
