from __future__ import annotations

"""조건검색 감시자 (Condition Watcher).

키움 WebSocket을 통해 조건검색을 구독하고,
편입/편출 종목을 실시간으로 targets 목록에 반영합니다.

흐름:
    1. 로그인 성공 → CNSRLST로 조건식 목록 조회
    2. 설정된 seq(조건식 번호)로 CNSRREQ(실시간) 구독
    3. REAL 메시지 수신
       843=I → targets에 종목 추가
       843=D → targets에서 종목 제거
    4. 종료 시 CNSRCLR로 구독 해제
"""

import asyncio
import logging
from typing import Callable

from config.settings import WebSocketConfig
from infra.websocket.kiwoom_ws import KiwoomWebSocket

logger = logging.getLogger(__name__)


class ConditionWatcher:
    """조건검색 실시간 구독을 담당하는 클래스입니다."""

    def __init__(
        self,
        config: WebSocketConfig,
        token: str,
        on_symbols_changed: Callable[[list[str]], None],
    ) -> None:
        """
        Parameters
        ----------
        config             : WebSocket 설정 (URL, 조건식 번호 등)
        token              : 키움 접근 토큰
        on_symbols_changed : 종목 목록이 바뀔 때 호출되는 콜백
                             trading_service가 이 콜백으로 targets를 갱신합니다
        """
        self.config = config
        self.token = token
        self.on_symbols_changed = on_symbols_changed

        # 현재 조건검색으로 편입된 종목 집합
        self._symbols: set[str] = set()

        self._ws_client = KiwoomWebSocket(
            url=config.url,
            token=token,
            on_message=self._handle_message,
        )

    async def start(self) -> None:
        """조건검색 감시를 시작합니다. main()에서 asyncio.create_task로 실행합니다."""
        await self._ws_client.start()

    async def stop(self) -> None:
        """구독을 해제하고 WebSocket 연결을 종료합니다."""
        logger.info(f"[COND] 조건검색 구독 해제 (seq={self.config.condition_seq})")
        await self._ws_client.send({
            "trnm": "CNSRCLR",
            "seq": str(self.config.condition_seq),
        })
        await self._ws_client.stop()

    # ── 메시지 처리 ──────────────────────────────────────────────

    async def _handle_message(self, msg: dict) -> None:
        """수신된 WebSocket 메시지를 종류별로 처리합니다."""
        trnm = msg.get("trnm", "")

        if trnm == "LOGIN":
            await self._on_login(msg)

        elif trnm == "CNSRLST":
            self._on_condition_list(msg)

        elif trnm == "CNSRREQ":
            # 최초 조회 결과 — 현재 조건 충족 종목 목록
            self._on_initial_result(msg)

        elif trnm == "REAL":
            # 실시간 편입/편출
            self._on_realtime(msg)

        else:
            logger.debug(f"[COND] 알 수 없는 메시지: {trnm}")

    async def _on_login(self, msg: dict) -> None:
        """로그인 결과 처리 — 성공 시 조건검색 구독 시작."""
        if msg.get("return_code") != 0:
            logger.error(f"[COND] WebSocket 로그인 실패: {msg.get('return_msg')}")
            return

        logger.info("[COND] WebSocket 로그인 성공")

        # 조건식 목록 조회
        logger.info("[COND] 조건검색 목록 조회 (CNSRLST)")
        await self._ws_client.send({"trnm": "CNSRLST"})

        # 잠깐 대기 후 실시간 구독 시작
        await asyncio.sleep(0.5)
        logger.info(f"[COND] 조건검색 구독 시작 (seq={self.config.condition_seq})")
        await self._ws_client.send({
            "trnm": "CNSRREQ",
            "seq": str(self.config.condition_seq),
            "search_type": "1",   # 1 = 조건검색 + 실시간 동시
            "stex_tp": "K",       # K = KRX
        })

    def _on_condition_list(self, msg: dict) -> None:
        """조건식 목록을 로그에 출력합니다."""
        conditions = msg.get("data", [])
        if not conditions:
            logger.warning("[COND] 등록된 조건검색식이 없습니다. HTS에서 조건식을 먼저 만들어주세요.")
            return

        logger.info(f"[COND] 사용 가능한 조건검색식 {len(conditions)}개:")
        for seq, name in conditions:
            marker = " ◀ 현재 사용 중" if str(seq) == str(self.config.condition_seq) else ""
            logger.info(f"[COND]   [{seq}] {name}{marker}")

    def _on_initial_result(self, msg: dict) -> None:
        """최초 조회 결과로 현재 조건 충족 종목 목록을 초기화합니다."""
        if msg.get("return_code") != 0:
            logger.error(f"[COND] 조건검색 조회 실패: {msg}")
            return

        data = msg.get("data", [])
        # jmcode 필드에서 종목코드 추출 ('A005930' → '005930')
        # 'A'로 시작하는 코드만 일반 주식으로 간주 (ETF, 인버스 등 제외)
        symbols = [
            item["jmcode"].lstrip("A")
            for item in data
            if "jmcode" in item and item["jmcode"].startswith("A")
        ]

        self._symbols = set(symbols)
        logger.info(f"[COND] 초기 조건검색 결과: {len(symbols)}개 종목")
        for s in sorted(symbols):
            logger.info(f"[COND]   편입 종목: {s}")

        self._notify()

    def _on_realtime(self, msg: dict) -> None:
        """실시간 편입/편출 메시지를 처리합니다."""
        data_list = msg.get("data", [])

        for item in data_list:
            values = item.get("values", {})
            raw_code = values.get("9001", "")
            action   = values.get("843", "")   # I=삽입(편입), D=삭제(편출)
            symbol   = raw_code.lstrip("A")

            # 'A'로 시작하지 않으면 ETF/인버스 등으로 간주하고 제외
            if not raw_code.startswith("A"):
                logger.info(f"[COND] 제외: {raw_code} — 일반 주식 아님 (ETF/인버스 등)")
                continue

            if not symbol:
                continue

            if action == "I":
                if symbol not in self._symbols:
                    self._symbols.add(symbol)
                    logger.info(f"[COND] 편입: {symbol} — targets에 추가됩니다")
                    self._notify()

            elif action == "D":
                if symbol in self._symbols:
                    self._symbols.discard(symbol)
                    logger.info(f"[COND] 편출: {symbol} — targets에서 제거됩니다")
                    self._notify()

    def _notify(self) -> None:
        """현재 종목 목록을 콜백으로 전달합니다."""
        self.on_symbols_changed(sorted(self._symbols))
