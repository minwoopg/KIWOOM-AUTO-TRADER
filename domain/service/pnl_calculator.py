# -*- coding: utf-8 -*-
"""FIFO 방식 매매 손익 계산 공통 모듈 (2026-07-22).

배경: daily_reporter.py와 analyze_trades.py는 7/16(CHANGELOG 7.6절)에
"전체 매수 평균가를 매도 수량에 곱하는" 부정확한 방식에서 진짜 FIFO
로트매칭으로 이미 수정됐는데, RiskManager._calc_daily_realized_pnl()은
그 수정 대상에서 빠져 있었음(GPT 검토로 발견). 신규 매수를 실제로
막는 손실 한도 판정이 부정확한 계산에 의존하고 있어서, 재진입·부분
매도가 섞인 날에는 실제로는 손실 한도를 초과했는데도 계산상 미달로
나와 매수를 계속 허용할 위험이 있었음.

daily_reporter.py의 _fifo_match()와 완전히 동일한 알고리즘을 재사용
가능한 형태로 옮겨왔다. daily_reporter.py는 이번엔 그대로 두고
RiskManager만 이 모듈을 사용(반환 타입이 달라 완전 통합은 회귀
위험이 커서 보류 — 알고리즘은 동일해 계산 결과는 일치함).

2026-07-22 (2차 수정, GPT 코드리뷰): 최초 버전은 다음 두 문제가
있었음(둘 다 실전 데이터로 재현 확인):
  1) 당일 매수 기록이 없는 종목의 매도(전일 이월 포지션 손절 등)가
     통째로 무시됨 -> 실제 손실이 있어도 0원으로 계산됨
  2) 매도 수량이 매수 수량보다 많으면(이월/외부매매/로그누락 등)
     초과분이 조용히 버려짐 -> 손실이 과소평가됨
둘 다 "계산을 조용히 누락하는" 유형이라 fail-open과 실질적으로
같은 위험 — trades.csv의 avg_buy_price(매도 시점 기록된 평균단가)
를 이월 원가로 사용해 매칭을 시도하고, 그래도 남는 수량이 있으면
PnlCalculationError를 발생시켜 상위(RiskManager)가 신규매수를
차단하도록 함.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass
from datetime import datetime


class PnlCalculationError(RuntimeError):
    """손익을 정확히 계산할 수 없을 때 발생합니다 (미매칭 매도수량 등).

    RiskManager는 이 예외를 DailyPnlUnavailableError로 감싸 신규매수를
    차단합니다 — 계산이 부정확할 수 있는 상태에서 손실 한도를
    "통과"로 판정하는 것보다 매수를 막는 쪽이 안전하다는 원칙(7.22절
    fail-close와 동일한 원칙)을 손익 계산 내부에도 일관되게 적용.
    """


@dataclass
class FifoMatch:
    """매도 1건이 FIFO로 매칭된 결과."""
    buy_price: float   # 가중평균 매수가
    sell_price: int
    qty: int
    buy_row: dict | None  # 첫 매수 row (이월 매칭이면 None)
    sell_row: dict         # 매도 row (참고용)
    is_carryover: bool = False  # 이월 포지션(avg_buy_price) 매칭 여부


def fifo_match(
    buy_list: list[dict], sell_list: list[dict], *, strict: bool = True,
) -> list[FifoMatch]:
    """매수 로트를 시간순 FIFO로 소진하며 매도 건별 가중평균 매수가를 계산합니다.

    각 항목의 dict는 최소 "qty"(또는 "quantity")와 "price" 키를 가져야
    합니다. daily_reporter.py의 _fifo_match()와 동일한 알고리즘에
    이월 포지션 처리(avg_buy_price)와 미매칭 감지를 추가했습니다.

    strict=True(기본값)이고 매수 로트를 다 소진해도 매도 수량이
    남으면, 그 매도 row에 "avg_buy_price"가 있으면 이월 원가로
    나머지를 매칭합니다(is_carryover=True로 표시). avg_buy_price도
    없으면 PnlCalculationError를 발생시킵니다. strict=False면 기존
    동작(조용히 버림)을 유지 — 참고용/리포트용 호출에서 예외 없이
    최대한 계산하고 싶을 때 사용.
    """
    def _qty(row: dict) -> int:
        return int(row.get("qty", row.get("quantity", 0)) or 0)

    def _price(row: dict) -> int:
        return int(row.get("price", 0) or 0)

    def _avg_buy_price(row: dict) -> int:
        return int(row.get("avg_buy_price", 0) or 0)

    buy_queue = [[_qty(b), _price(b), b] for b in buy_list]
    matched: list[FifoMatch] = []
    for sell in sell_list:
        sell_price = _price(sell)
        sell_qty = _qty(sell)
        if sell_price <= 0 or sell_qty <= 0:
            continue
        remaining, consumed_cost, consumed_qty, first_buy = sell_qty, 0, 0, None
        while remaining > 0 and buy_queue:
            lot = buy_queue[0]
            lot_qty, lot_price, lot_row = lot
            if lot_price <= 0 or lot_qty <= 0:
                buy_queue.pop(0)
                continue
            if first_buy is None:
                first_buy = lot_row
            take = min(remaining, lot_qty)
            consumed_cost += take * lot_price
            consumed_qty += take
            lot[0] -= take
            remaining -= take
            if lot[0] <= 0:
                buy_queue.pop(0)

        if consumed_qty > 0:
            matched.append(FifoMatch(
                buy_price=consumed_cost / consumed_qty,
                sell_price=sell_price,
                qty=consumed_qty,
                buy_row=first_buy,
                sell_row=sell,
            ))

        # 매수 큐를 다 소진했는데 매도 수량이 남은 경우 (이월 포지션,
        # 로그 누락, 외부 매매 등)
        if remaining > 0:
            avg_buy = _avg_buy_price(sell)
            if avg_buy > 0:
                matched.append(FifoMatch(
                    buy_price=avg_buy,
                    sell_price=sell_price,
                    qty=remaining,
                    buy_row=None,
                    sell_row=sell,
                    is_carryover=True,
                ))
            elif strict:
                symbol = sell.get("symbol", "?")
                raise PnlCalculationError(
                    f"미매칭 매도수량: symbol={symbol}, sell_qty={sell_qty}, "
                    f"unmatched={remaining} (avg_buy_price도 없어 이월"
                    f"원가로도 계산 불가)"
                )
            # strict=False면 기존처럼 조용히 버림(참고용 호출 대비)

    return matched


def calculate_realized_pnl_grouped_legacy(
    buys_by_symbol: dict[str, list[dict]],
    sells_by_symbol: dict[str, list[dict]],
    *, strict: bool = True,
) -> int:
    """종목별 매수/매도 목록(시간순 아님)을 받아 전체 실현손익(원)을 FIFO로 계산합니다.

    ⚠️ 이름에 "legacy"가 붙은 이유(2026-07-22, 4차 GPT 코드리뷰):
    이 함수는 매수/매도를 종목별로 미리 분리한 리스트를 받으므로,
    "매도 시점 이후에 일어난 매수"까지 매칭에 포함될 수 있습니다
    (합성 데이터로 재현 확인). "리포트니까 시간순서 오류가 허용된다"는
    건 잘못된 가정이라는 지적을 받아들여 함수명에 이 한계를 명시함
    — 실시간이든 리포트든, 시점이 중요한 계산에는 반드시
    calculate_realized_pnl_by_events()를 사용해야 합니다.

    현재 운영 코드(RiskManager) 어디서도 이 함수를 호출하지 않습니다.
    daily_reporter.py는 이번 리팩터링 범위 밖이라 자체
    `_fifo_match()` 구현을 그대로 유지하고 있으며, 이 함수와는 무관
    합니다. 이 함수는 과거 호환 및 참고용으로만 남겨둡니다 — 새 코드
    에서 이 함수를 다시 쓰지 마세요.

    각 row dict는 최소 "qty"(또는 "quantity")와 "price" 키를 가져야 합니다.
    """
    total_pnl = 0
    for symbol, sell_list in sells_by_symbol.items():
        buy_list = buys_by_symbol.get(symbol, [])
        for sell in sell_list:
            sell.setdefault("symbol", symbol)
        matches = fifo_match(buy_list, sell_list, strict=strict)
        for m in matches:
            total_pnl += int((m.sell_price - m.buy_price) * m.qty)
    return total_pnl


# 2026-07-22 (7차 수정, GPT 코드리뷰): 단순 별칭(calculate_realized_
# pnl_fifo = calculate_realized_pnl_grouped_legacy)이었는데, 이러면
# 누군가 이 이름으로 새로 호출해도 아무 경고 없이 "시간순서 미보장"
# 함수가 조용히 쓰일 수 있음(7.21절에서 실제로 문제를 일으켰던
# 바로 그 함수). 호출 시점에 DeprecationWarning을 내도록 얇은
# 래퍼로 전환 — 하위 호환(같은 인자로 같은 결과)은 유지하되, 이
# 이름으로 부르면 눈에 띄게 경고하도록 함.
def calculate_realized_pnl_fifo(
    buys_by_symbol: dict[str, list[dict]],
    sells_by_symbol: dict[str, list[dict]],
    *, strict: bool = True,
) -> int:
    """calculate_realized_pnl_grouped_legacy의 하위 호환용 별칭 (deprecated).

    시간순서를 보장하지 않는 레거시 함수입니다 — 실시간이든 리포트든
    시점이 중요한 계산에는 calculate_realized_pnl_by_events를 쓰세요.
    """
    warnings.warn(
        "calculate_realized_pnl_fifo는 시간순서를 보장하지 않는 레거시 "
        "함수입니다(매도 시점 이후 매수가 매칭에 섞일 수 있음). "
        "calculate_realized_pnl_by_events를 사용하세요.",
        DeprecationWarning,
        stacklevel=2,
    )
    return calculate_realized_pnl_grouped_legacy(
        buys_by_symbol, sells_by_symbol, strict=strict
    )


def _parse_positive_int(value, field_name: str) -> int:
    """숫자 필드를 정수로 안전하게 변환합니다. 실패 시 PnlCalculationError.

    2026-07-22 (3차 수정, GPT 코드리뷰): avg_buy_price 등이 손상된
    값("invalid_value" 등)이면 기존엔 bare int()가 ValueError를
    그대로 던져서 RiskManager의 PnlCalculationError catch를 우회해
    버렸음(표준 차단 사유로 기록되지 않고 그 폴링만 조용히 스킵됨).
    파싱 실패를 항상 PnlCalculationError로 통일.
    """
    try:
        parsed = int(str(value).replace(",", "")) if value not in (None, "") else 0
    except (TypeError, ValueError) as exc:
        raise PnlCalculationError(f"{field_name} 파싱 실패: {value!r}") from exc
    return parsed


def calculate_realized_pnl_by_events(
    events: list[dict],
    *, strict: bool = True,
) -> int:
    """거래 이벤트를 실제 발생 시간순으로 처리하며 FIFO 실현손익(원)을 계산합니다.

    2026-07-22 (3차 수정, GPT 코드리뷰): calculate_realized_pnl_fifo()는
    매수/매도를 종목별로 미리 분리해 넘기므로, 매도 시점 이후의 미래
    매수까지 그 매도에 매칭될 수 있는 구조적 결함이 있었음(합성
    데이터로 재현: 10시 매수 50주 -> 11시 매도 100주(50주는 실제로는
    이월분이어야 함) -> 12시 재매수 50주 순서일 때, 12시 매수가
    11시 매도에 쓰여버림). 이 함수는 이벤트를 반드시 timestamp
    오름차순으로 정렬해 하나씩 순서대로 적용 — 매도 시점에는 그
    시점까지 발생한 매수만 큐에 존재.

    각 이벤트 dict는 "timestamp"(ISO 문자열), "side"("BUY"/"SELL"),
    "symbol", "qty"(또는 "quantity"), "price", 매도 이벤트라면 선택적
    "avg_buy_price"를 가져야 합니다.

    이월+당일매수가 섞인 상태에서 매도 수량이 그 시점까지의 매수
    잔량을 초과하면(예: 이월 100주+당일매수 100주 상태에서 200주
    매도 — avg_buy_price가 이월분만의 단가가 아니라 혼합 평균이라
    임의로 적용하면 부정확해짐, GPT 지적) opening snapshot 없이는
    정확한 계산이 불가능하므로 strict=True면 PnlCalculationError를
    발생시켜 fail-close합니다. 이월 없이(당일 매수 전무) 매도만
    있는 단순 케이스는 기존처럼 avg_buy_price를 그대로 이월 원가로
    사용 — 그 경우엔 전체 평균=이월 원가이므로 정확함.

    2026-07-22 (4차 수정, GPT 코드리뷰): 같은 종목의 "순수 이월
    매도"(당일 매수가 전혀 없는 상태의 매도)가 여러 번 나오면,
    실제 opening position 수량을 모르는 상태에서 avg_buy_price를
    반복 적용해 손익을 중복 계산할 위험이 있음(예: 이월 100주를
    두 번의 매도 주문으로 나눠 접수했는데 둘 다 avg_buy_price를
    그대로 적용하면 실제보다 큰 수량을 판 것처럼 계산됨). strict=True
    면 종목당 순수 이월 매도는 1회만 신뢰하고, 두 번째부터는
    PnlCalculationError로 fail-close.
    """
    sortable = []
    for ev in events:
        try:
            # 2026-07-22 (6차 수정, GPT 코드리뷰): 호출자(RiskManager)가
            # 이제 정규화된 timestamp를 넘기지만, 이 함수를 직접 호출하는
            # 다른 경로(테스트, 향후 다른 호출자)까지 대비해 여기서도
            # 방어적으로 strip — "둘 다 넣어도 과하지 않다"는 지적을
            # 받아들여 이중 방어.
            ts = datetime.fromisoformat(str(ev["timestamp"]).strip())
        except (KeyError, ValueError) as exc:
            if strict:
                raise PnlCalculationError(f"이벤트 timestamp 파싱 실패: {ev!r}") from exc
            continue
        sortable.append((ts, ev))
    sortable.sort(key=lambda pair: pair[0])

    buy_queues: dict[str, list[list]] = {}
    carryover_sell_seen: set[str] = set()
    total_pnl = 0

    for _ts, ev in sortable:
        symbol = ev.get("symbol", "?")
        side = ev.get("side")
        price = _parse_positive_int(ev.get("price", 0), "price")
        qty = _parse_positive_int(ev.get("qty", ev.get("quantity", 0)), "qty")
        if price <= 0 or qty <= 0:
            # 2026-07-22 (4차 수정, GPT 코드리뷰): strict=True를 표방하면서도
            # price/qty가 0이하면 조용히 continue하고 있었음 — accepted=true
            # 행에서 이런 값은 정상적으로 나올 이유가 없으므로(RiskManager
            # 쪽에서 이미 걸러지지만, 이 함수를 직접 호출하는 다른 경로
            # 대비 방어적으로 동일 원칙 적용) strict면 오류로 처리.
            if strict:
                raise PnlCalculationError(
                    f"유효하지 않은 거래값: symbol={symbol}, side={side}, "
                    f"price={price}, qty={qty}"
                )
            continue

        if side == "BUY":
            buy_queues.setdefault(symbol, []).append([qty, price])
            continue
        if side != "SELL":
            if strict:
                raise PnlCalculationError(f"알 수 없는 거래방향: {side!r} (symbol={symbol})")
            continue

        queue = buy_queues.setdefault(symbol, [])
        had_existing_buys_before_this_sell = len(queue) > 0
        remaining, consumed_cost, consumed_qty = qty, 0, 0
        while remaining > 0 and queue:
            lot = queue[0]
            lot_qty, lot_price = lot
            if lot_price <= 0 or lot_qty <= 0:
                queue.pop(0)
                continue
            take = min(remaining, lot_qty)
            consumed_cost += take * lot_price
            consumed_qty += take
            lot[0] -= take
            remaining -= take
            if lot[0] <= 0:
                queue.pop(0)

        if consumed_qty > 0:
            total_pnl += price * consumed_qty - consumed_cost

        if remaining > 0:
            avg_buy = _parse_positive_int(ev.get("avg_buy_price", 0), "avg_buy_price")
            if had_existing_buys_before_this_sell:
                # 2026-07-22 GPT 지적: 이 시점 이전에 이미 당일 매수가
                # 있었는데 그 잔량을 다 쓰고도 매도가 남았다는 건, 이월
                # 물량과 당일 매수가 섞인 상태 — avg_buy_price는 매도
                # 시점 기준 "이월+당일매수 혼합 평균"일 가능성이 높아
                # 남은 수량에 그대로 적용하면 부정확함(opening snapshot
                # 없이는 이월분만의 단가를 분리할 수 없음). 안전하게
                # fail-close.
                if strict:
                    raise PnlCalculationError(
                        f"이월+당일매수 혼합 포지션: symbol={symbol}, "
                        f"매도수량={qty}, 미매칭={remaining} — opening "
                        f"position 원가 스냅샷 없이는 avg_buy_price를 "
                        f"신뢰할 수 없어 계산을 거부합니다"
                    )
            elif avg_buy > 0:
                # 당일 매수가 전혀 없던 순수 이월 전량매도 — avg_buy_price가
                # 곧 이월 원가와 같으므로 안전하게 적용 가능. 단, 같은
                # 종목의 순수 이월 매도가 이미 한 번 있었다면(중복
                # 감지) opening position 수량을 모르는 상태에서 반복
                # 적용은 위험 — 두 번째부터는 fail-close.
                if symbol in carryover_sell_seen:
                    if strict:
                        raise PnlCalculationError(
                            f"동일 종목 순수 이월 매도가 여러 번 기록됨: "
                            f"symbol={symbol} — 실제 체결수량을 확인할 "
                            f"수 없어 계산을 거부합니다"
                        )
                else:
                    carryover_sell_seen.add(symbol)
                    total_pnl += (price - avg_buy) * remaining
            elif strict:
                raise PnlCalculationError(
                    f"미매칭 매도수량: symbol={symbol}, sell_qty={qty}, "
                    f"unmatched={remaining} (avg_buy_price도 없어 이월"
                    f"원가로도 계산 불가)"
                )

    return total_pnl
