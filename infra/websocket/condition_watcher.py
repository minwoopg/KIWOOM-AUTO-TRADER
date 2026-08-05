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
import re
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
        """종목 → 조건식 이름 매핑 (복수 조건식 편입 시 마지막 기준).

        2026-08-05 (GPT 코드리뷰 지적): 이 property는 여러 조건식에
        동시 편입된 종목의 정보를 하나로 뭉개버림 — 아래 dict 순회
        순서상 "마지막으로 처리된 seq"의 이름만 남는데, 이건
        _symbols_by_seq(dict)의 순회 순서에 의존하는 우연한 결과라
        실제로 "대표 조건식"을 의미 있게 고르는 게 아님. 기존
        호환을 위해 남겨두되, 신규 코드는 symbol_to_conditions
        (복수형, 전체 조건식 보존)를 사용해야 함.
        """
        mapping: dict[str, str] = {}
        for seq, symbols in self._symbols_by_seq.items():
            cond_name = self._condition_names.get(seq, f"seq{seq}")
            for sym in symbols:
                mapping[sym] = cond_name
        return mapping

    @property
    def symbol_to_conditions(self) -> dict[str, tuple[str, ...]]:
        """종목 → 편입된 모든 조건식 이름의 튜플 매핑.

        2026-08-05 (GPT 코드리뷰 지적, VWAP shadow 1단계): 한 종목이
        동시에 여러 조건식(예: "자동매매_돌파형A"와 "자동매매_
        눌림목_PR")에 편입될 수 있는데, 기존 symbol_to_condition은
        그중 하나만 남겨서 "이 종목이 실제로 눌림목 조건식에도
        들어와 있는지"를 정확히 판단할 수 없었음. 이 property는
        해당 종목이 편입된 *모든* 조건식 이름을 튜플로 보존 —
        순서는 seq 딕셔너리 순회 순서(결정적이지 않을 수 있음)라
        판단 로직에서는 순서에 의존하지 말고 in 연산자로만 사용할 것.
        """
        mapping: dict[str, list[str]] = {}
        for seq, symbols in self._symbols_by_seq.items():
            cond_name = self._condition_names.get(seq, f"seq{seq}")
            for sym in symbols:
                mapping.setdefault(sym, []).append(cond_name)
        return {sym: tuple(names) for sym, names in mapping.items()}

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
        symbols = []
        for d in data:
            jmcode = d.get("jmcode", "")
            code = jmcode.lstrip("A")
            if re.fullmatch(r"\d{6}", code):
                symbols.append(code)

        if seq in self._symbols_by_seq:
            self._symbols_by_seq[seq] = set(symbols)

        cond_name = self._condition_names.get(seq, seq)
        logger.info(f"[COND] [{cond_name}] 초기 결과: {len(symbols)}개 종목")
        for s in sorted(symbols):
            logger.info(f"[COND]   [{cond_name}] 편입: {s}")

        self._notify()

    def _on_realtime(self, msg: dict) -> None:
        data_list = msg.get("data") or []
        # ── 종목별로 처리 (seq는 data[i].item 필드에 있음) ──────────
        # 2026-06-24 REAL 메시지 구조 확인:
        #   msg_keys = ['data', 'trnm']  (최상위에 seq 없음)
        #   data[0]_keys = ['values', 'type', 'name', 'item']
        #   → seq는 각 item의 'item' 필드에 담겨 있음

        for item in data_list:
            values   = item.get("values", {})
            raw_code = values.get("9001", "")
            action   = values.get("843", "")

            # ── seq 귀속 ─────────────────────────────────────────
            # 키움 REAL 메시지에는 어느 조건식에서 편입됐는지 정보가 없음.
            # (item['item']은 종목코드, item['name']은 '조건검색'으로 고정 — 2026-06-26 확인)
            # 따라서 편입된 종목을 모든 단타 seq에 동시 귀속시킴.
            # → 조건검색식별 통계는 의미없어지나, 종목 편입/편출 자체는 정상 동작.
            seq = next(iter(self._symbols_by_seq)) if self._symbols_by_seq else None
            if seq is None:
                continue

            # A 접두사를 떼고 6자리 숫자인지로 판정 (A 유무 혼용 대응)
            symbol = raw_code.lstrip("A")
            if not re.fullmatch(r"\d{6}", symbol):
                logger.debug(f"[COND] 제외: {raw_code} — 종목코드 형식 아님")
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
