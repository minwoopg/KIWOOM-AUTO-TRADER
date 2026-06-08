from __future__ import annotations

"""조건검색 감시자 — 다중 조건식 동시 구독 지원.

여러 조건식을 동시에 구독해 편입/편출 종목을 실시간으로 targets에 반영합니다.

흐름:
    1. 로그인 성공 → CNSRLST로 조건식 목록 조회
    2. condition_seqs에 등록된 모든 seq에 CNSRREQ(실시간) 구독
    3. REAL 메시지 수신
       843=I → targets에 종목 추가
       843=D → 해당 조건식에서 편출 (다른 조건식 편입 유지)
    4. 종료 시 CNSRCLR로 모든 구독 해제
"""

import asyncio
import logging
from typing import Callable

from config.settings import WebSocketConfig
from infra.websocket.kiwoom_ws import KiwoomWebSocket

logger = logging.getLogger(__name__)


class ConditionWatcher:
    """다중 조건검색 실시간 구독을 담당하는 클래스입니다."""

    def __init__(
        self,
        config: WebSocketConfig,
        token: str,
        on_symbols_changed: Callable[[list[str]], None],
    ) -> None:
        self.config = config
        self.token  = token
        self.on_symbols_changed = on_symbols_changed

        # 조건식별 편입 종목 관리 {seq: set(symbols)}
        self._symbols_by_seq: dict[str, set[str]] = {}
        for seq in config.condition_seqs:
            self._symbols_by_seq[str(seq)] = set()

        # 조건식 이름 저장 {seq: name}
        self._condition_names: dict[str, str] = {}

        self._ws_client = KiwoomWebSocket(
            url=config.url,
            token=token,
            on_message=self._handle_message,
        )

    @property
    def _all_symbols(self) -> set[str]:
        """모든 조건식의 편입 종목 합집합입니다."""
        result: set[str] = set()
        for symbols in self._symbols_by_seq.values():
            result |= symbols
        return result

    @property
    def symbol_to_condition(self) -> dict[str, str]:
        """종목 → 조건식 이름 매핑 (복수 조건식 편입 시 마지막 기준)."""
        mapping: dict[str, str] = {}
        for seq, symbols in self._symbols_by_seq.items():
            cond_name = self._condition_names.get(seq, f"seq{seq}")
            for sym in symbols:
                mapping[sym] = cond_name
        return mapping

    async def start(self) -> None:
        await self._ws_client.start()

    async def stop(self) -> None:
        for seq in self.config.condition_seqs:
            logger.info(f"[COND] 조건검색 구독 해제 (seq={seq})")
            await self._ws_client.send({"trnm": "CNSRCLR", "seq": str(seq)})
        await self._ws_client.stop()

    # ── 메시지 처리 ──────────────────────────────────────────────

    async def _handle_message(self, msg: dict) -> None:
        trnm = msg.get("trnm", "")
        if trnm == "LOGIN":
            await self._on_login(msg)
        elif trnm == "CNSRLST":
            self._on_condition_list(msg)
        elif trnm == "CNSRREQ":
            self._on_initial_result(msg)
        elif trnm == "REAL":
            self._on_realtime(msg)
        else:
            logger.debug(f"[COND] 알 수 없는 메시지: {trnm}")

    async def _on_login(self, msg: dict) -> None:
        if msg.get("return_code") != 0:
            logger.error(f"[COND] WebSocket 로그인 실패: {msg.get('return_msg')}")
            return

        logger.info("[COND] WebSocket 로그인 성공")
        await self._ws_client.send({"trnm": "CNSRLST"})
        await asyncio.sleep(0.5)

        # 모든 조건식 구독
        for seq in self.config.condition_seqs:
            logger.info(f"[COND] 조건검색 구독 시작 (seq={seq})")
            await self._ws_client.send({
                "trnm": "CNSRREQ",
                "seq": str(seq),
                "search_type": "1",
                "stex_tp": "K",
            })
            await asyncio.sleep(0.3)  # 연속 요청 간격

    def _on_condition_list(self, msg: dict) -> None:
        conditions = msg.get("data") or []
        if not conditions:
            logger.warning("[COND] 등록된 조건검색식이 없습니다.")
            return
        logger.info(f"[COND] 사용 가능한 조건검색식 {len(conditions)}개:")
        subscribed = {str(s) for s in self.config.condition_seqs}
        for item in conditions:
            # item이 [seq, name] 리스트 또는 (seq, name) 튜플 형태
            try:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    seq, name = item[0], item[1]
                elif isinstance(item, dict):
                    seq = item.get("seq", item.get("no", ""))
                    name = item.get("name", item.get("condition_name", ""))
                else:
                    logger.warning(f"[COND] 알 수 없는 조건식 형식: {item}")
                    continue
            except Exception as e:
                logger.warning(f"[COND] 조건식 파싱 실패: {item} — {e}")
                continue
            marker = " ◀ 구독 중" if str(seq) in subscribed else ""
            logger.info(f"[COND]   [{seq}] {name}{marker}")
            if str(seq) in subscribed:
                self._condition_names[str(seq)] = name

    def _on_initial_result(self, msg: dict) -> None:
        if msg.get("return_code") != 0:
            logger.error(f"[COND] 조건검색 조회 실패: {msg}")
            return

        # 어느 조건식 결과인지 확인
        seq = str(msg.get("seq", ""))
        data = msg.get("data") or []
        symbols = [
            item["jmcode"].lstrip("A")
            for item in data
            if "jmcode" in item and item["jmcode"].startswith("A")
        ]

        if seq in self._symbols_by_seq:
            self._symbols_by_seq[seq] = set(symbols)

        cond_name = self._condition_names.get(seq, seq)
        logger.info(f"[COND] [{cond_name}] 초기 결과: {len(symbols)}개 종목")
        for s in sorted(symbols):
            logger.info(f"[COND]   [{cond_name}] 편입: {s}")

        self._notify()

    def _on_realtime(self, msg: dict) -> None:
        data_list = msg.get("data") or []
        seq = str(msg.get("seq", ""))

        for item in data_list:
            values   = item.get("values", {})
            raw_code = values.get("9001", "")
            action   = values.get("843", "")
            symbol   = raw_code.lstrip("A")

            if not raw_code.startswith("A"):
                logger.info(f"[COND] 제외: {raw_code} — ETF/인버스 등")
                continue
            if not symbol:
                continue

            target_set = self._symbols_by_seq.get(seq, set())

            cond_name = self._condition_names.get(seq, seq)

            if action == "I":
                if symbol not in target_set:
                    target_set.add(symbol)
                    self._symbols_by_seq[seq] = target_set
                    logger.info(f"[COND] [{cond_name}] 편입: {symbol}")
                    self._notify()

            elif action == "D":
                if symbol in target_set:
                    target_set.discard(symbol)
                    self._symbols_by_seq[seq] = target_set
                    still_in = any(
                        symbol in s
                        for k, s in self._symbols_by_seq.items() if k != seq
                    )
                    if still_in:
                        logger.info(f"[COND] [{cond_name}] 편출: {symbol} (다른 조건식 유지)")
                    else:
                        logger.info(f"[COND] [{cond_name}] 편출: {symbol} — targets에서 제거")
                    self._notify()

    def _notify(self) -> None:
        self.on_symbols_changed(sorted(self._all_symbols))
