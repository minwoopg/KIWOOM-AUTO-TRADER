from __future__ import annotations

"""키움 WebSocket 기본 클라이언트.

연결, 로그인, PING 응답, 재연결을 담당합니다.
조건검색 등 구체적인 기능은 이 클래스를 상속하거나 주입받아 사용합니다.
"""

import asyncio
import json
import logging
from typing import Callable, Awaitable

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)


class KiwoomWebSocket:
    """키움 WebSocket 연결을 관리하는 기본 클라이언트입니다."""

    def __init__(
        self,
        url: str,
        token: str,
        on_message: Callable[[dict], Awaitable[None]],
        reconnect_delay: float = 5.0,
    ) -> None:
        """
        Parameters
        ----------
        url             : WebSocket 서버 URL
        token           : 접근 토큰 (REST API와 동일한 토큰)
        on_message      : 메시지 수신 시 호출할 비동기 콜백
        reconnect_delay : 연결 끊김 후 재연결 대기 시간 (초)
        """
        self.url = url
        self.token = token
        self.on_message = on_message
        self.reconnect_delay = reconnect_delay

        self._ws = None
        self._running = False

    async def start(self) -> None:
        """WebSocket 연결을 시작하고 메시지를 수신합니다.
        연결이 끊기면 자동으로 재연결합니다.
        """
        self._running = True
        while self._running:
            try:
                await self._connect_and_run()
            except Exception as exc:
                if self._running:
                    logger.warning(f"[WS] 연결 끊김: {exc} — {self.reconnect_delay}초 후 재연결")
                    await asyncio.sleep(self.reconnect_delay)

    async def stop(self) -> None:
        """WebSocket 연결을 종료합니다."""
        self._running = False
        if self._ws:
            await self._ws.close()

    async def send(self, message: dict) -> None:
        """서버에 JSON 메시지를 전송합니다."""
        if self._ws is None:
            logger.warning("[WS] 연결이 없어 메시지를 보낼 수 없습니다.")
            return
        try:
            await self._ws.send(json.dumps(message))
            logger.debug(f"[WS] 전송: {message.get('trnm', message)}")
        except Exception as exc:
            logger.warning(f"[WS] 전송 실패: {exc}")

    # ── 내부 메서드 ──────────────────────────────────────────────

    async def _connect_and_run(self) -> None:
        """실제 WebSocket 연결 및 수신 루프입니다."""
        logger.info(f"[WS] 연결 시도: {self.url}")
        async with websockets.connect(self.url) as ws:
            self._ws = ws
            logger.info("[WS] 연결 성공 — 로그인 패킷 전송")

            await self.send({"trnm": "LOGIN", "token": self.token})

            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning(f"[WS] JSON 파싱 실패: {raw[:100]}")
                    continue

                trnm = msg.get("trnm", "")

                # PING은 그대로 돌려보냄 (연결 유지)
                if trnm == "PING":
                    await self.send(msg)
                    continue

                # 나머지는 콜백으로 전달
                await self.on_message(msg)
