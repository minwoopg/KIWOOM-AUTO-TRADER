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


class MessageHandlerError(RuntimeError):
    """`on_message` 콜백 내부에서 발생한 애플리케이션 코드 오류입니다.

    2026-08-06 (1G, GPT 코드리뷰 지적 2번, 재현 확인): 기존
    `start()`는 모든 예외를 "연결 끊김"으로 취급해 5초 후 재연결
    했습니다. 그 결과 1E.8의 `AttributeError`(ConditionWatcher
    필드명 불일치)처럼 **네트워크와 무관한 결정적인 코드 오류**도
    무한 재연결 루프에 빠졌습니다 — 실서버 8/6 08:40~09:06에
    242회 반복됐고, 재현 테스트에서도 0.6초에 12회 재연결이
    확인됐습니다. 재시도해도 결과가 절대 달라지지 않는 오류이므로,
    이 예외로 감싸 재연결 루프를 건너뛰고 그대로 전파해
    `watcher_start_guarded()`가 프로세스를 비정상 종료시키도록
    합니다(장애가 조용히 감춰지는 대신 즉시 드러남).
    """


# 재연결로 회복 가능한 "진짜" 일시적 네트워크 오류들.
#
# 2026-08-06 (1G.1, GPT 코드리뷰 지적, 재현 확인): 1G에서는 여기에
# `websockets.exceptions.WebSocketException`과 `InvalidHandshake`를
# 넣었는데, 전자는 **모든 WebSocket 예외의 최상위 부모**라 좁히기가
# 통째로 무의미해졌음. 설치본(websockets 17.0.1) 기준 35개 하위
# 클래스가 전부 재연결 대상이 됨 — `ConnectionClosed`조차 이 부모를
# 상속하므로, 사실상 "WebSocket 관련 예외는 전부 재연결"이었음.
#
# 재현 결과(수정 전): InvalidURI / ProtocolError / PayloadTooBig 모두
# 반복 재연결. 즉 "명시된 네트워크 오류만 재연결"이라는 1G의 정책이
# 실제로는 성립하지 않았음.
#
# 아래 셋만 남김 — 재시도하면 결과가 달라질 수 있는 일시적 오류.
#   ConnectionClosed     : 서버가 연결을 끊음(정상/비정상 모두)
#   OSError              : ConnectionRefused/Reset, socket.gaierror 등
#   asyncio.TimeoutError : 응답 지연
#
# 나머지(InvalidURI, ProtocolError, PayloadTooBig, InvalidStatus,
# InvalidHandshake 계열 등)는 URL·인증·헤더·서버 설정·프로토콜
# 처리 문제라 재시도해도 같은 결과이므로 전파합니다.
RECOVERABLE_NETWORK_ERRORS = (
    ConnectionClosed,
    OSError,          # ConnectionRefusedError, socket.gaierror 등 포함
    asyncio.TimeoutError,
)


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
        logger.info("[WS] start() 진입 — 재연결 루프 시작")
        while self._running:
            try:
                await self._connect_and_run()
            except MessageHandlerError:
                # 2026-08-06 (1G): 애플리케이션 콜백의 코드 오류 —
                # 재연결해도 똑같이 실패하므로 루프를 벗어나 전파.
                raise
            except websockets.exceptions.InvalidHandshake:
                # 2026-08-06 (1G.1): 핸드셰이크 실패는 서버 일시 장애일
                # 수도 있지만 URL·인증 토큰·헤더·서버 설정 오류일 수도
                # 있음. 후자라면 무제한 재연결이 문제를 감추기만 하므로,
                # 우선 로그를 남기고 전파해 즉시 드러나게 함. 서버 5xx에
                # 한해 재시도하려면 상태코드 판별 + 제한 횟수 + 지수
                # 백오프를 별도로 구현해야 하며, 이번 단계 범위 밖.
                if self._running:
                    logger.exception("[WS] 핸드셰이크 실패 — URL·인증·서버 설정 오류 가능성")
                raise
            except RECOVERABLE_NETWORK_ERRORS as exc:
                if self._running:
                    logger.warning(f"[WS] 연결 끊김: {exc} — {self.reconnect_delay}초 후 재연결")
                    await asyncio.sleep(self.reconnect_delay)
            except Exception:
                # 2026-08-06 (1G): 분류되지 않은 예외도 재연결하지
                # 않고 전파 — 기존처럼 조용히 무한 반복되면 1E.8과
                # 같은 장애가 또 감춰짐. 재연결이 필요한 새 네트워크
                # 예외 유형이 발견되면 RECOVERABLE_NETWORK_ERRORS에
                # 명시적으로 추가하는 방향으로 대응.
                if self._running:
                    logger.exception("[WS] 재연결 대상이 아닌 예외로 중단 — 프로세스를 실패시킵니다")
                raise

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

        ws = await websockets.connect(self.url)
        self._ws = ws

        try:
            logger.info("[WS] 연결 성공 — 로그인 패킷 전송")
            await self.send({"trnm": "LOGIN", "token": self.token})

            while True:
                raw = await ws.recv()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning(f"[WS] JSON 파싱 실패: {raw[:100]}")
                    continue

                trnm = msg.get("trnm", "")

                if trnm == "PING":
                    await self.send(msg)
                    continue

                # 2026-08-06 (1G): 콜백 내부의 코드 오류를 네트워크
                # 끊김과 구분하기 위해 별도 예외로 감쌈. CancelledError는
                # 정상적인 종료 신호이므로 그대로 통과시켜야 함
                # (BaseException이라 아래 except Exception에 안 걸리지만
                # 의도를 명시).
                try:
                    await self.on_message(msg)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception(f"[WS] 메시지 처리 코드 오류 (trnm={trnm}): {exc}")
                    raise MessageHandlerError(
                        f"WebSocket 메시지 처리 실패 (trnm={trnm}): {exc}"
                    ) from exc

        finally:
            await ws.close()
            self._ws = None
