from __future__ import annotations

"""주문 전 마지막 안전장치 역할을 하는 모듈.

전략이 BUY를 냈다고 해서 바로 주문하지 않고,
리스크 규칙에 위배되지 않는지 여기서 한번 더 점검합니다.
"""

import csv
import re
from datetime import date, datetime
from pathlib import Path

from config.settings import RiskConfig, TradingConfig
from domain.models import AccountBalance, OrderRequest, RuntimeState
from domain.service.pnl_calculator import calculate_realized_pnl_by_events, PnlCalculationError
from infra.storage.skip_reason import SkipReason


class DailyPnlUnavailableError(RuntimeError):
    """당일 실현손익을 신뢰성 있게 계산할 수 없을 때 발생합니다.

    2026-07-22: 기존엔 trades.csv가 없거나 파싱에 실패하면 손익을
    0원으로 간주해 손실 한도 체크를 그냥 통과시켰음(fail-open) —
    "안전 방향"이라는 기존 주석과 반대로, 실거래 자동매매 프로그램
    에서는 손실 한도 안전장치를 무력화하는 쪽이라 위험함(GPT 검토로
    발견). can_place_order()가 이 예외를 잡아 신규매수를 차단하는
    쪽(fail-close)으로 전환.
    """


def _parse_positive_int_or_raise(value, field_name: str) -> int:
    """숫자 필드를 양의 정수로 파싱합니다. 실패/0이하면 DailyPnlUnavailableError.

    2026-07-22 (4차 수정, GPT 코드리뷰): accepted=true인 행에서
    price/quantity가 0이거나 손상된 값이면 조용히 스킵하고 있었음
    (SELL price=0으로 재현 — 실제 손실 매도가 손익 계산에서 통째로
    빠져 당일 손실이 과소평가됨). _try_buy()/_try_sell()이 항상
    current_price를 채워서 로깅하는 구조라, accepted=true인 행에서
    price<=0/qty<=0은 정상적으로 발생할 이유가 없다고 판단해 오류로
    처리.
    """
    try:
        parsed = int(str(value).replace(",", "")) if value not in (None, "") else 0
    except (TypeError, ValueError) as exc:
        raise DailyPnlUnavailableError(f"{field_name} 파싱 실패: {value!r}") from exc
    if parsed <= 0:
        raise DailyPnlUnavailableError(f"{field_name} 유효하지 않음: {parsed}")
    return parsed


class RiskManager:
    """주문 가능 여부를 검사하는 리스크 관리자입니다."""

    def __init__(
        self,
        trading_config: TradingConfig,
        risk_config: RiskConfig,
        trade_log_file: str = "logs/trades.csv",
    ) -> None:
        self.trading_config = trading_config
        self.risk_config = risk_config
        self._trade_log_file = Path(trade_log_file)

    # ── 일일 실현 손익 계산 ───────────────────────────────────────

    def _calc_daily_realized_pnl(self, target_date: date | None = None) -> int:
        """trades.csv에서 당일 실현 손익(원)을 시간순 FIFO 방식으로 계산합니다.

        2026-07-22 (3차 수정, GPT 코드리뷰): 이전 버전은 매수/매도를
        종목별로 미리 분리한 리스트로 만들어 FIFO 계산기에 넘겼는데,
        이러면 "매도 시점 이후에 일어난 매수"까지 그 매도에 매칭될
        수 있는 구조적 결함이 있었음(합성 데이터로 재현 확인 —
        10시 매수 -> 11시 매도(이월분 포함이어야 함) -> 12시 재매수
        순서일 때 12시 매수가 11시 매도에 쓰여버림). 이제 모든 거래를
        이벤트 리스트로 모아 실제 timestamp 순으로 정렬한 뒤
        calculate_realized_pnl_by_events()에 통째로 넘겨 시간순으로
        처리 — 매도 시점에는 그 시점까지 발생한 매수만 큐에 존재.

        - accepted=true인 행의 price/quantity가 0이거나 손상되면
          손익 계산을 신뢰할 수 없으므로 DailyPnlUnavailableError로
          fail-close합니다 (2026-07-22 5차 수정, GPT 코드리뷰 —
          이전엔 "price=0인 행은 체결가 미기록으로 간주하고 조용히
          제외"했는데, 실제 손실 매도 행이 손상되면 그 손실이 손익
          계산에서 통째로 빠져 당일 손실이 과소평가될 위험이 있었음).
        - 파일이 없거나 읽기 오류 시 DailyPnlUnavailableError를 발생시킵니다
          (fail-close — 계산을 신뢰할 수 없으면 신규매수를 막는 쪽이 안전).
          TradeCsvLogger가 프로세스 시작 시 헤더만 있는 빈 파일을 미리
          생성하므로, 정상 운영 중에는 이 파일이 존재하지 않는 상황
          자체가 이례적임(GPT 지적) — "정상적으로 아직 거래가 없는
          경우"와 구분하지 않고 둘 다 fail-close로 통일.
        """
        target_date = target_date or date.today()

        if not self._trade_log_file.exists():
            raise DailyPnlUnavailableError(
                f"trades.csv를 찾을 수 없습니다: {self._trade_log_file} "
                f"(TradeCsvLogger가 시작 시 빈 파일을 미리 생성하므로, "
                f"정상 운영 중이라면 이 상황 자체가 이례적입니다)"
            )

        events: list[dict] = []

        try:
            with self._trade_log_file.open(encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)

                # 2026-07-22 (5차 수정, GPT 코드리뷰): 빈 파일/헤더 누락/
                # 필수 컬럼 누락도 여전히 조용히 0원 처리되고 있었음
                # (별도 검증으로 재현). 헤더 자체를 신뢰할 수 없으면
                # 파일 내용을 신뢰할 수 없으므로 즉시 fail-close.
                # 2026-07-22 (6차 수정, GPT 코드리뷰): avg_buy_price는
                # 이월 손익 계산(순수 이월 매도)에 실제로 쓰이는 핵심
                # 필드인데 필수 헤더에서 빠져 있었음 — 실제 TradeCsvLogger
                # (infra/storage/logger.py의 TRADE_FIELDS)는 항상 이
                # 컬럼을 포함하므로 필수로 강제해도 정상 운영에는 영향
                # 없음. 헤더 중복(예: "price"가 두 번 나오는 손상된
                # 헤더)도 DictReader가 조용히 뒤 값으로 덮어써버리므로
                # 별도 검증 추가.
                required_headers = {
                    "timestamp", "symbol", "side", "quantity", "price",
                    "accepted", "avg_buy_price",
                }
                fieldnames = reader.fieldnames
                if not fieldnames:
                    raise DailyPnlUnavailableError(
                        "trades.csv가 비어 있거나 헤더가 없습니다"
                    )
                if len(fieldnames) != len(set(fieldnames)):
                    duplicates = sorted(
                        {name for name in fieldnames if fieldnames.count(name) > 1}
                    )
                    raise DailyPnlUnavailableError(
                        f"trades.csv 중복 헤더: {duplicates}"
                    )
                missing = required_headers - set(fieldnames)
                if missing:
                    raise DailyPnlUnavailableError(
                        f"trades.csv 필수 헤더 누락: {sorted(missing)}"
                    )

                # 2026-07-22 (4차 수정, GPT 코드리뷰): strict=True를 표방
                # 하면서도 실제로는 accepted=true인 행의 timestamp/side/
                # price/qty가 손상돼도 continue로 조용히 스킵하고
                # 있었음 — SELL price=0처럼 실제 손실 매도 행이
                # 손상되면 그 매도 자체가 손익 계산에서 통째로 빠져
                # 당일 손실이 과소평가되는데도 조용히 0원으로 처리됨
                # (합성 CSV로 재현 확인). accepted=true인 행에 한해
                # 필드 검증을 엄격하게 전환 — accepted가 "true"도
                # "false"도 아닌 손상된 값 자체도 이례적이므로 오류로
                # 처리. "false"(정상적인 주문 거부)는 여전히 정상
                # 스킵 — 이건 손상이 아니라 정당한 값이므로.
                #
                # 2026-07-22 (5차 수정, GPT 코드리뷰): 4차 수정에서
                # "timestamp 파싱 실패는 날짜를 모르니 안전하게 스킵"
                # 으로 처리했는데, 이게 "오늘 행인데 timestamp만
                # 우연히 손상된 경우"까지 조용히 누락시키는 새로운
                # fail-open이었음(합성 데이터로 재현: 오늘 손실 SELL의
                # timestamp만 깨졌더니 그 손실이 통째로 빠지고 0원
                # 처리됨). 근본 원인은 logs/trades.csv에 4~5월 필드가
                # 밀린 손상 행 274건이 실제로 남아있던 것 — 이번에
                # logs/archive/trades_legacy_corrupted_until_20260722.csv
                # 로 분리하고 logs/trades.csv는 정상 행만 남도록 정리함
                # (analyze_trades.py 등 분석 스크립트는 archive를 별도로
                # 읽을 수 있음, RiskManager는 정리된 trades.csv만 사용).
                # 파일이 깨끗해졌으므로 GPT 제안대로 timestamp 파싱
                # 실패를 다시 무조건 fail-close로 되돌림 — "몰라서
                # 스킵"이 아니라 "모르면 차단"이 원칙에 맞음.
                for line_no, row in enumerate(reader, start=2):
                    # 2026-07-22 (5차 수정 중 추가 발견): timestamp
                    # 필드 앞뒤에 공백이 섞인 행이 실제로 존재함(예:
                    # "  2026-07-06T14:35:53.775081" — CSV 필드 내
                    # 특수문자로 인한 파싱 밀림으로 추정). 이런 행이
                    # accepted=false(정상 거부)인 경우까지 포함해서
                    # 무조건 fail-close하면, 파일을 순회하는 도중
                    # target_date와 무관하게 그 즉시 함수 전체가 실패해
                    # 모든 날짜 조회가 막힘(실제 재현). 흔한 공백 오염은
                    # strip()으로 방어하고, 그래도 파싱이 안 되는
                    # "진짜" 손상만 fail-close.
                    ts_raw = (row.get("timestamp") or "").strip()
                    try:
                        ts = datetime.fromisoformat(ts_raw)
                    except (ValueError, TypeError) as exc:
                        raise DailyPnlUnavailableError(
                            f"{line_no}행 timestamp 손상: {row.get('timestamp')!r}"
                        ) from exc
                    if ts.date() != target_date:
                        continue

                    accepted_raw = (row.get("accepted") or "").strip().lower()
                    if accepted_raw not in ("true", "false"):
                        raise DailyPnlUnavailableError(
                            f"{line_no}행({target_date} 소속) accepted 값 손상: "
                            f"{row.get('accepted')!r}"
                        )
                    if accepted_raw == "false":
                        continue

                    side = (row.get("side") or "").strip().upper()
                    if side not in ("BUY", "SELL"):
                        raise DailyPnlUnavailableError(
                            f"{line_no}행 side 값 손상: {row.get('side')!r}"
                        )

                    symbol = (row.get("symbol") or "").strip()
                    # 2026-07-22 (5차 수정, GPT 코드리뷰): symbol이 비어
                    #있지 않은지만 검사했었음 — 필드가 밀린 손상 행이
                    # 우연히 "비어있지 않은 문자열"을 symbol 자리에 갖게
                    # 되면 이 검사를 통과해버릴 수 있음. 국내 종목코드는
                    # 항상 6자리 숫자(실제 logs/trades.csv 전체 검증으로
                    # 확인)이므로 형식까지 검증.
                    if not re.fullmatch(r"\d{6}", symbol):
                        raise DailyPnlUnavailableError(
                            f"{line_no}행 symbol 형식 오류: {symbol!r}"
                        )

                    price_raw = _parse_positive_int_or_raise(
                        row.get("price"), f"{line_no}행 price"
                    )
                    qty_raw = _parse_positive_int_or_raise(
                        row.get("quantity"), f"{line_no}행 quantity"
                    )

                    events.append({
                        # 2026-07-22 (6차 수정, GPT 코드리뷰): 여기서
                        # row["timestamp"](원본, strip 안 된 값)를 그대로
                        # 넘기고 있었음 — 위에서 ts_raw로 strip해서 검증은
                        # 통과했지만, calculate_realized_pnl_by_events()가
                        # 이 원본값을 다시 파싱하려다 그대로 실패해서
                        # "strip 수정"이 실제로는 완성되지 않은 반쪽
                        # 수정이었음(GPT가 별도 실행으로 재현 확인).
                        # 이미 성공적으로 파싱된 ts(datetime 객체)를
                        # 표준 isoformat 문자열로 재생성해서 넘기면,
                        # 이후 어떤 재파싱 시도에도 안전함.
                        "timestamp": ts.isoformat(),
                        "side": side,
                        "symbol": symbol,
                        "price": price_raw,
                        "qty": qty_raw,
                        "avg_buy_price": row.get("avg_buy_price", ""),
                    })
        except DailyPnlUnavailableError:
            # 2026-07-22 (5차 수정, GPT 코드리뷰): 위 블록 안에서 이미
            # DailyPnlUnavailableError로 명확히 분류한 예외를 여기서
            # 다시 "trade_log_file 파싱 실패: 23행 price 유효하지 않음"
            # 처럼 이중으로 감싸고 있었음 — 메시지가 중첩돼 원인 파악이
            # 흐려짐. 이미 분류된 예외는 그대로 전파.
            raise
        except (OSError, UnicodeError, csv.Error) as exc:
            # 2026-07-22 (7차 수정, GPT 코드리뷰): 기존엔 bare
            # except Exception이라 프로그래밍 버그(예: 로직 오류로
            # 인한 TypeError/AttributeError 등)까지 전부 "CSV 손상"
            # 으로 뭉개서 DailyPnlUnavailableError로 보이게 만들었음
            # — fail-close 자체는 안전 방향이지만, 진짜 코드 버그를
            # 데이터 오류로 착각하게 만들어 원인 파악을 어렵게 함.
            # 파일 열기 실패(OSError)/인코딩 오류/CSV 파싱 오류처럼
            # "정말 파일 문제"인 경우만 여기서 DailyPnlUnavailableError
            # 로 감싸고, 그 외 예상 못한 예외는 그대로 올라가 stack
            # trace가 정확히 보이도록 함.
            raise DailyPnlUnavailableError(
                f"trade_log_file 파싱 실패: {exc}"
            ) from exc

        try:
            return calculate_realized_pnl_by_events(events, strict=True)
        except PnlCalculationError as exc:
            # 미매칭 매도수량, 이월+당일매수 혼합, 필드 파싱 실패 등 —
            # 손익을 정확히 계산할 수 없으므로 fail-close
            raise DailyPnlUnavailableError(str(exc)) from exc

    # ── 주문 가능 여부 검사 ───────────────────────────────────────

    def can_place_order(
        self,
        order: OrderRequest,
        balance: AccountBalance,
        state: RuntimeState,
    ) -> tuple[bool, str]:
        """주문이 가능한지 검사하고, 불가능하면 이유를 함께 돌려줍니다."""

        # 종목당 하루 1회 진입 제한
        if not self.trading_config.allow_multiple_entries_per_symbol_per_day:
            if order.symbol in state.bought_symbols_today:
                return False, SkipReason.ALREADY_HOLDING

        # 최대 보유 종목 수 제한
        current_symbols = {position.symbol for position in balance.positions}
        if order.symbol not in current_symbols and len(current_symbols) >= self.trading_config.max_positions:
            return False, SkipReason.MAX_POSITIONS

        # 최소 현금 버퍼 유지
        estimated_amount = (order.price or 0) * order.quantity
        if balance.cash - estimated_amount < self.risk_config.min_cash_buffer:
            return False, SkipReason.RISK_LIMIT

        # 주문 금액 상한 제한
        if estimated_amount > self.risk_config.max_order_amount:
            return False, SkipReason.RISK_LIMIT

        # ── 일일 최대 손실 한도 ──────────────────────────────────
        try:
            daily_pnl = self._calc_daily_realized_pnl()
        except DailyPnlUnavailableError as exc:
            # 2026-07-22: 기존엔 계산 실패 시 0원으로 간주해 매수를 계속
            # 허용(fail-open)했음 — 이 검사 자체가 무력화되는 셈이라
            # 위험. 계산을 못 믿으면 신규매수를 막는 쪽(fail-close)으로
            # 전환. 이미 보유 중인 포지션의 손절/트레일링 판단(_try_sell
            # 경로)에는 영향 없음 — 여기는 오직 "신규 매수 허용 여부"만
            # 판단하는 지점이므로 매도는 계속 정상 동작함.
            return False, f"{SkipReason.DAILY_PNL_UNAVAILABLE} ({exc})"
        if daily_pnl <= -abs(self.risk_config.max_daily_loss_amount):
            return (
                False,
                f"{SkipReason.DAILY_LOSS_LIMIT} "
                f"({daily_pnl:,}원 / 한도 -{abs(self.risk_config.max_daily_loss_amount):,}원)",
            )

        # ── 연속 손절 한도 ───────────────────────────────────────
        max_consec = getattr(self.risk_config, "max_consecutive_losses", 0)
        if max_consec > 0 and state.consecutive_losses >= max_consec:
            return (
                False,
                f"{SkipReason.CONSECUTIVE_LOSS_LIMIT} "
                f"({state.consecutive_losses}회 연속 손절 / 한도 {max_consec}회)",
            )

        return True, "ok"
