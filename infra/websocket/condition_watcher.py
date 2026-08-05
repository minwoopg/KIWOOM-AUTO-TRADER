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

        # 2026-08-05 (GPT 코드리뷰 지적, VWAP shadow 조건검색식 출처
        # 문제 재현 확인): 키움 REAL 메시지에는 어느 조건식에서 편입
        # 됐는지 정보가 없어서, 기존 코드는 모든 실시간 편입/편출을
        # "첫 번째 seq"(next(iter(...)))에 임의로 귀속시키고 있었음
        # — 재현: seq1=돌파형A, seq2=눌림목_PR 상태에서 058610이
        # 실시간 편입되면, 실제 출처와 무관하게 항상 seq1(돌파형A)
        # 결과로 기록됨. 이 오귀속 정보로 조건검색식 기반 VWAP shadow
        # (is_pullback_condition 등)를 계산하면 통계 자체가 왜곡됨.
        #
        # 해결: 각 종목이 "확실한 출처"(CNSRREQ 초기 조회 결과)로
        # 알려진 것인지, "출처 불명"(REAL 실시간 이벤트로만 알려진
        # 것)인지 추적. reliable=False인 종목은 조건검색식 기반
        # shadow 판단(is_pullback_condition 등)에 쓰지 않고, 대신
        # PR/C(분봉 자체 분석값이라 이 문제와 무관)는 정상 계산.
        self._condition_source_reliable: dict[str, bool] = {}

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

        "__realtime_unknown__" 버킷(실시간 이벤트로만 알려져 어느
        조건식인지 불확실한 종목)은 제외 — 이 매핑은 "확정된 조건식
        이름"만 담아야 함.
        """
        mapping: dict[str, str] = {}
        for seq, symbols in self._symbols_by_seq.items():
            if seq == "__realtime_unknown__":
                continue
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

        "__realtime_unknown__" 버킷은 제외 — 어느 조건식인지
        불확실한 종목은 이 매핑에 아예 안 나타남(빈 튜플로 취급).
        """
        mapping: dict[str, list[str]] = {}
        for seq, symbols in self._symbols_by_seq.items():
            if seq == "__realtime_unknown__":
                continue
            cond_name = self._condition_names.get(seq, f"seq{seq}")
            for sym in symbols:
                mapping.setdefault(sym, []).append(cond_name)
        return {sym: tuple(names) for sym, names in mapping.items()}

    @property
    def symbol_condition_source_reliable(self) -> dict[str, bool]:
        """종목별로 조건식 출처가 신뢰 가능한지(CNSRREQ 초기 조회로 확정됐는지).

        2026-08-05 (GPT 코드리뷰 지적, VWAP shadow 조건검색식 출처
        문제 대응): True면 이 종목의 symbol_to_conditions 값이
        CNSRREQ(조건식별 초기 조회, 메시지에 정확한 seq 포함)로
        확정된 것 — 조건식 출처 기반 shadow 판단(is_pullback_
        condition 등)에 안전하게 쓸 수 있음. False 또는 이 dict에
        아예 없으면(딕셔너리에 없는 경우도 "신뢰 불가"로 취급해야
        함) 실시간 이벤트로만 알려져 정확한 조건식을 확정할 수
        없는 상태 — 이런 종목은 targets에는 정확히 포함되지만
        조건식 출처 기반 통계에는 쓰면 안 됨.
        """
        return dict(self._condition_source_reliable)

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
            # 2026-08-05: CNSRREQ 초기 조회 결과는 메시지 자체에
            # 정확한 seq가 포함되어 있어 출처가 확실함 — 이 seq에
            # 편입된 것으로 확인된 종목은 신뢰 가능으로 표시.
            for s in symbols:
                self._condition_source_reliable[s] = True

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

            # A 접두사를 떼고 6자리 숫자인지로 판정 (A 유무 혼용 대응)
            symbol = raw_code.lstrip("A")
            if not re.fullmatch(r"\d{6}", symbol):
                logger.debug(f"[COND] 제외: {raw_code} — 종목코드 형식 아님")
                continue

            # 2026-08-05 (GPT 코드리뷰 지적, 재현 확인): 키움 REAL
            # 메시지에는 어느 조건식에서 편입/편출됐는지 정보가 없음
            # (item['item']은 종목코드, item['name']은 '조건검색'으로
            # 고정 — 2026-06-26 확인). 기존 코드는 이걸 "첫 번째 seq"
            # (next(iter(...)))에 임의로 귀속시켰는데, 실제로는 어느
            # seq에서 온 이벤트인지 전혀 알 수 없어 조건검색식별 통계
            # (is_pullback_condition 등)를 심각하게 왜곡시켰고, 편출
            # 처리도 "실제로 다른 조건식에 남아있는 종목을 잘못
            # 첫 seq에서만 제거"할 위험이 있었음(재현: seq1=돌파형A,
            # seq2=눌림목_PR 상태에서 실시간 편입 이벤트가 오면 항상
            # seq1 결과로만 기록됨).
            #
            # 수정(GPT 권장 3번 방식 — 가장 보수적): 실시간 이벤트는
            # "이 종목이 어떤 조건식엔가 소속돼 있다/아니다"라는 전체
            # targets 갱신에만 쓰고, 어느 조건식인지는 확정하지 않음.
            # 전용 버킷("__realtime_unknown__")에 편입/편출을 반영해
            # _all_symbols(targets)는 정확히 유지하되, 이 버킷은
            # symbol_to_condition(s) 계산에서 제외 — 그 결과 이
            # 종목은 조건검색식 이름 없이 targets에만 잡히고, 다음
            # CNSRREQ 재조회 때 정확한 seq로 다시 확정됨.
            unknown_bucket = self._symbols_by_seq.setdefault("__realtime_unknown__", set())

            if action == "I":
                if symbol not in unknown_bucket and symbol not in self._all_symbols:
                    unknown_bucket.add(symbol)
                    # 2026-08-05: 출처를 확정할 수 없으므로 신뢰
                    # 불가로 명시 — 이미 CNSRREQ로 신뢰 가능하다고
                    # 표시된 종목이면 그 값을 덮어쓰지 않음(실시간
                    # 이벤트가 이미 확인된 종목의 신뢰도를 낮출
                    # 이유는 없음 — 다만 조건식 소속 자체는 이
                    # 버킷과 무관하게 이미 알고 있던 것을 유지).
                    self._condition_source_reliable.setdefault(symbol, False)
                    logger.info(f"[COND] [출처불명] 편입: {symbol} (실시간 이벤트, "
                                f"어느 조건식인지 불확실 — 다음 재조회 시 확정)")
                    self._notify()

            elif action == "D":
                # 편출은 종목이 소속된 모든 버킷(seq별 + unknown)에서
                # 제거 — "실제로 다른 조건식에 남아있는 종목을 잘못
                # 첫 seq에서만 제거"하던 기존 버그를 근본적으로 없앰:
                # 이제 특정 seq 하나를 임의로 골라 제거하지 않고,
                # 이 종목이 들어있는 모든 버킷에서 실제로 제거.
                removed_from_any = False
                for seq_key, sym_set in self._symbols_by_seq.items():
                    if symbol in sym_set:
                        sym_set.discard(symbol)
                        removed_from_any = True
                if removed_from_any:
                    self._condition_source_reliable.pop(symbol, None)
                    logger.info(f"[COND] [출처불명] 편출: {symbol} (실시간 이벤트) "
                                f"— targets에서 제거")
                    self._notify()

    def _notify(self) -> None:
        self.on_symbols_changed(sorted(self._all_symbols))
