from __future__ import annotations

"""조건검색 감시자 — 다중 조건식 동시 구독 지원.

여러 조건식을 동시에 구독해 편입/편출 종목을 실시간으로 targets에 반영합니다.

흐름:
    1. 로그인 성공 → CNSRLST로 조건식 목록 조회
    2. condition_seqs에 등록된 모든 seq에 CNSRREQ(실시간) 구독
    3. REAL 메시지 수신
       843=I → targets에 종목 추가(어느 조건식인지는 불확실 —
               다음 CNSRREQ 재조회로 확정 전까지 조건식 출처
               기반 shadow 분석에서는 제외됨)
       843=D → targets에서 종목 완전 제거(2026-08-05 정정: 키움
               REAL 메시지에는 어느 조건식의 편출인지 정보가
               없어서, "그 종목이 소속된 모든 조건식에서 편출"
               되는 보수적 정책 — 실제로는 다른 조건식에 여전히
               남아있는 종목도 함께 제거될 수 있음. "정확한
               조건식별 편출"이 아니라 "출처불명 이벤트는 안전을
               위해 전체 제거"라는 원칙임)
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

        # 2026-08-05 (3차 GPT 코드리뷰 지적, P0-2 전면 재설계):
        # 기존엔 _symbols_by_seq 하나(+ __realtime_unknown__ 특수
        # 버킷)와 _condition_source_reliable(단순 dict, 누적 수정)
        # 로만 상태를 관리했는데, 다음 세 가지 문제가 재현으로
        # 확인됨:
        #
        # (A) 이미 CNSRREQ로 확정돼 reliable=True인 종목에 REAL I
        #     가 와도, "symbol not in self._all_symbols" 조건 때문에
        #     아무 처리도 안 하고 reliable=True가 그대로 유지됨 —
        #     그 REAL I가 실제로는 다른(아직 모르는) 조건식에서의
        #     신규 편입일 수 있는데도 출처를 안 바꿈.
        # (B) REAL D가 오면 그 종목이 들어있는 모든 seq 버킷에서
        #     한꺼번에 제거함 — 두 조건식에 동시 편입된 종목이 한
        #     조건식에서만 편출된 경우에도 전체가 삭제됨(부정확한
        #     "정확한 편출"이 아니라 "보수적 전체 제거" 정책임을
        #     인지하고 있어야 함).
        # (C) _on_initial_result()가 새 결과에 없는 종목의 stale
        #     reliability나 unknown marker를 정리하지 않음 — 재조회
        #     결과 특정 조건식에 더 이상 없다고 확인된 종목도 과거
        #     reliable=True가 그대로 남음.
        #
        # 해결: reliability를 "누적 dict"로 들고 다니지 않고, 매번
        # 아래 두 상태에서 그때그때 재계산.
        #   _confirmed_symbols_by_seq: CNSRREQ로 확정된 조건식별
        #     편입 종목(진짜 seq 키만, "__realtime_unknown__" 없음).
        #   _realtime_unresolved: REAL 이벤트로만 알려져 출처가
        #     아직 미확정인 종목의 집합 — I로 추가, D로 제거,
        #     CNSRREQ 재조회로 확정되면(같은 종목이 다시 오든 안
        #     오든) 제거.
        # reliable(symbol) = symbol이 confirmed 쪽에 있고
        #                     symbol이 unresolved에는 없음.
        # 이렇게 하면 "reliable을 True로 표시했다가 나중에 잘못된
        # False로 덮어쓸 실수" 자체가 구조적으로 불가능해짐 —
        # 매번 현재 상태에서 새로 계산하므로.
        self._confirmed_symbols_by_seq: dict[str, set[str]] = {}
        for seq in config.condition_seqs:
            self._confirmed_symbols_by_seq[str(seq)] = set()
        self._realtime_unresolved: set[str] = set()

        # 2026-08-06 (1E.9): 900003(이미 등록된 seq) 응답에 대해
        # CNSRCLR+재구독을 시도한 seq 기록 — 연결당 seq별 1회로
        # 제한해 재시도 무한 루프를 막습니다. 매 로그인마다 초기화.
        self._resubscribe_attempted: set[str] = set()

        # 조건식 이름 저장 {seq: name}
        self._condition_names: dict[str, str] = {}

        self._ws_client = KiwoomWebSocket(
            url=config.url,
            token=token,
            on_message=self._handle_message,
        )

    @property
    def _all_symbols(self) -> set[str]:
        """모든 조건식의 확정 편입 종목 + 출처 미확정 실시간 종목의 합집합입니다."""
        result: set[str] = set()
        for symbols in self._confirmed_symbols_by_seq.values():
            result |= symbols
        result |= self._realtime_unresolved
        return result

    @property
    def confirmed_symbols_by_seq(self) -> dict[str, set[str]]:
        """조건식(seq)별 확정 편입 종목의 복사본입니다.

        2026-08-06 (1E.9): `app/main.py`가 `_confirmed_symbols_by_seq`
        (private)를 직접 참조하던 것이 1E.7의 필드 개명 때
        `AttributeError` 재연결 루프를 일으켰고, 회귀 스위트는
        그 코드를 실행하지 않아 잡지 못했습니다. 외부에서 쓰는
        경로를 public property로 고정해, 앞으로 내부 필드명이
        바뀌어도 호출부가 깨지지 않도록 합니다. 호출부가 실수로
        내부 상태를 변경하지 못하도록 얕은 복사본을 돌려줍니다.
        """
        return {seq: set(syms) for seq, syms in self._confirmed_symbols_by_seq.items()}

    @property
    def realtime_unresolved_symbols(self) -> set[str]:
        """REAL 이벤트로만 알려져 조건식 출처가 미확정인 종목의 복사본입니다.

        2026-08-06 (1E.9): 이 종목들은 `symbol_condition_source_
        reliable`에서는 신뢰 불가로 취급되지만, **매매 감시 대상
        에서는 제외하면 안 됩니다** — 장중 조건검색 편입은 전부
        이 경로로 들어오기 때문입니다. 자세한 배경은
        `app/target_selection.compute_day_targets()` 주석 참고.
        """
        return set(self._realtime_unresolved)

    @property
    def symbol_to_condition(self) -> dict[str, str]:
        """종목 → 조건식 이름 매핑 (복수 조건식 편입 시 마지막 기준).

        2026-08-05 (GPT 코드리뷰 지적): 이 property는 여러 조건식에
        동시 편입된 종목의 정보를 하나로 뭉개버림 — 아래 dict 순회
        순서상 "마지막으로 처리된 seq"의 이름만 남는데, 이건
        _confirmed_symbols_by_seq(dict)의 순회 순서에 의존하는
        우연한 결과라 실제로 "대표 조건식"을 의미 있게 고르는 게
        아님. 기존 호환을 위해 남겨두되, 신규 코드는 symbol_to_
        conditions(복수형, 전체 조건식 보존)를 사용해야 함.

        출처 미확정(_realtime_unresolved) 종목은 제외 — 이 매핑은
        "확정된 조건식 이름"만 담아야 함.
        """
        mapping: dict[str, str] = {}
        for seq, symbols in self._confirmed_symbols_by_seq.items():
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

        출처 미확정(_realtime_unresolved) 종목은 제외 — 어느
        조건식인지 불확실한 종목은 이 매핑에 아예 안 나타남(빈
        튜플로 취급).
        """
        mapping: dict[str, list[str]] = {}
        for seq, symbols in self._confirmed_symbols_by_seq.items():
            cond_name = self._condition_names.get(seq, f"seq{seq}")
            for sym in symbols:
                mapping.setdefault(sym, []).append(cond_name)
        return {sym: tuple(names) for sym, names in mapping.items()}

    @property
    def symbol_condition_source_reliable(self) -> dict[str, bool]:
        """종목별로 조건식 출처가 신뢰 가능한지(CNSRREQ 초기 조회로 확정됐는지).

        2026-08-05 (3차 GPT 코드리뷰 지적, P0-2): 이 값은 더 이상
        누적 dict가 아니라, _confirmed_symbols_by_seq와 _realtime_
        unresolved 두 상태로부터 호출 시점에 매번 재계산됨 — True면
        이 종목의 symbol_to_conditions 값이 CNSRREQ(조건식별 초기
        조회, 메시지에 정확한 seq 포함)로 확정된 것이고 그 이후
        출처 불명 REAL 이벤트에 노출되지 않은 상태.

        주의: 이 딕셔너리는 confirmed_union(한 번이라도 CNSRREQ로
        확정된 적 있는 종목)만 키로 가짐 — REAL로만 알려져 한 번도
        확정된 적 없는 종목은 이 딕셔너리에 아예 없음(값이 False로
        들어있는 게 아니라 키 자체가 없음). 호출부는 반드시
        `.get(symbol, False)`처럼 기본값을 False로 명시해서 조회
        해야 함 — `.get(symbol)`만 쓰면 None이 반환되어 "신뢰
        불가(False)"와 "값 없음(None)"을 혼동하는 버그가 생김
        (재현 확인: 테스트에서 기본값 없이 조회했다가 None을
        받고 `is False` 비교가 실패했던 사례). 실제 호출부인
        TradingService._write_signal_log()는 `.get(symbol, False)`
        로 정확히 처리하고 있음.
        """
        confirmed_union: set[str] = set()
        for symbols in self._confirmed_symbols_by_seq.values():
            confirmed_union |= symbols
        return {
            sym: (sym not in self._realtime_unresolved)
            for sym in confirmed_union
        }

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
            await self._on_initial_result(msg)
        elif trnm == "REAL":
            self._on_realtime(msg)
        else:
            logger.debug(f"[COND] 알 수 없는 메시지: {trnm}")

    async def _on_login(self, msg: dict) -> None:
        if msg.get("return_code") != 0:
            logger.error(f"[COND] WebSocket 로그인 실패: {msg.get('return_msg')}")
            return

        logger.info("[COND] WebSocket 로그인 성공")
        # 2026-08-06 (1E.9): 새 연결이므로 재구독 시도 기록 초기화 —
        # 재연결 때마다 900003 회복을 다시 한 번 시도할 수 있게 함.
        self._resubscribe_attempted.clear()
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

    @staticmethod
    def _extract_seq_from_error(msg: dict) -> str:
        """오류 응답에서 대상 seq를 뽑아냅니다.

        2026-08-06 (1E.9, 실서버 로그 기준): 900003 응답에는 최상위
        `seq` 키가 없고 사람이 읽는 문구 안에만 들어있습니다.
            {'trnm': 'CNSRREQ', 'return_code': 900003,
             'return_msg': '이미 등록된 조건검색 일련번호입니다.(seq=3)'}
        최상위 seq를 우선 보고, 없으면 return_msg에서 파싱합니다.
        """
        seq = str(msg.get("seq", "") or "")
        if seq:
            return seq
        m = re.search(r"seq\s*=\s*(\d+)", str(msg.get("return_msg", "")))
        return m.group(1) if m else ""

    async def _on_initial_result(self, msg: dict) -> None:
        return_code = msg.get("return_code")
        if return_code != 0:
            # ── 900003: 이미 등록된 조건검색 일련번호 ──────────────
            # 2026-08-06 (1E.9, 실서버 8/6 09:10:05 로그로 확인):
            # 1E.8 이전의 재연결 루프가 서버 쪽에 구독을 남긴 채
            # 끊기면서, 재기동 후 seq3의 CNSRREQ가 900003으로
            # 실패했고 그대로 early return —
            # `_confirmed_symbols_by_seq["3"]`이 하루 종일 빈 채로
            # 남았습니다. CNSRREQ는 연결당 1회만 발송되므로 스스로
            # 회복될 경로가 없어, 해당 조건식이 통째로 죽습니다.
            # 이제 CNSRCLR로 기존 등록을 해제한 뒤 한 번만 재구독을
            # 시도합니다(연결당 seq별 1회 — 무한 루프 방지).
            if return_code == 900003:
                seq = self._extract_seq_from_error(msg)
                if seq and seq not in self._resubscribe_attempted:
                    self._resubscribe_attempted.add(seq)
                    logger.warning(
                        f"[COND] seq={seq} 이미 등록된 조건검색 일련번호 — "
                        f"CNSRCLR로 해제 후 재구독 시도(연결당 1회)"
                    )
                    await self._ws_client.send({"trnm": "CNSRCLR", "seq": seq})
                    await asyncio.sleep(0.3)
                    await self._ws_client.send({
                        "trnm": "CNSRREQ",
                        "seq": seq,
                        "search_type": "1",
                        "stex_tp": "K",
                    })
                    return
                logger.error(
                    f"[COND] seq={seq or '?'} 재구독까지 실패 — 이 조건식의 확정 "
                    f"편입 목록이 비어있게 됩니다(실시간 편입은 출처 미확정으로 "
                    f"계속 수신됨): {msg}"
                )
                return
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

        if seq in self._confirmed_symbols_by_seq:
            new_symbol_set = set(symbols)
            # 2026-08-05 (3차 GPT 코드리뷰 지적 P0-2 문제D, 재현
            # 확인): 이전엔 이 seq의 확정 결과만 덮어쓰고, 그
            # 결과로 확정된 종목의 unresolved 상태를 정리하지
            # 않았음 — 예를 들어 047040이 REAL로 unresolved에
            # 들어간 뒤, 이번 재조회 결과에 047040이 포함되면
            # (즉 실제로 이 조건식 소속이었다고 확정되면) unresolved
            # 에서 반드시 제거해야 reliable=True로 정확히 계산됨.
            # 재조회 결과에 없는 종목(이 seq에서는 사라졌다고
            # 확인된 것)은 이 seq 버킷에서만 빠지고, 다른 seq나
            # unresolved 상태는 그대로 유지 — "이 조건식에는 없다"
            # 는 것이 "완전히 알 수 없다"는 뜻은 아니므로.
            self._confirmed_symbols_by_seq[seq] = new_symbol_set
            self._realtime_unresolved -= new_symbol_set

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
            # 고정 — 2026-06-26 확인). 실시간 이벤트는 "이 종목이
            # 어떤 조건식엔가 소속돼 있다/아니다"라는 targets 갱신
            # 에만 쓰고, 어느 조건식인지는 절대 확정하지 않음.

            if action == "I":
                # 2026-08-05 (3차 GPT 코드리뷰 지적 P0-2 문제A, 재현
                # 확인): 이전엔 "symbol not in self._all_symbols"
                # 조건 때문에, 이미 CNSRREQ로 확정돼 reliable=True
                # 인 종목에 REAL I가 와도 아무 처리를 안 해서
                # reliable=True가 그대로 남았음 — 하지만 이 REAL I는
                # 실제로 다른(아직 모르는) 조건식에서의 신규 편입
                # 일 수 있어, 출처를 모르는 채로 True를 유지하는 건
                # 틀림. 이제 이미 알려진 종목이어도 REAL I가 오면
                # 무조건 _realtime_unresolved에 추가 — symbol_
                # condition_source_reliable() 계산식이 "confirmed
                # 이면서 unresolved에 없어야 True"이므로, 이 종목은
                # 즉시 reliable=False로 떨어짐(다음 CNSRREQ 재조회
                # 결과에 다시 나타나야 reliable=True로 복구됨).
                #
                # 2026-08-06 (1G, GPT 코드리뷰 지적 1번, 재현 확인):
                # 위 처리는 watcher 내부 상태만 바꿀 뿐 _notify()를
                # 호출하지 않아서, 이미 알려진 종목의 reliability가
                # True → False로 떨어져도 TradingService에는 전달되지
                # 않았음. update_targets()는 콜백으로만 갱신되므로
                # shadow 로그에 과거의 condition_source_reliable=True가
                # 계속 남게 됨(재현: 변경 후 watcher 내부는 False인데
                # on_symbols_changed 호출 0회).
                # → 종목 수가 변하지 않아도 **reliability 메타데이터가
                #   바뀌면 콜백을 호출**해야 함. unresolved에 처음
                #   추가되는 경우에만 notify하고, 동일 REAL I가 반복되면
                #   상태 변화가 없으므로 생략해서 불필요한 폴링 갱신을
                #   막음.
                was_unresolved = symbol in self._realtime_unresolved
                was_known = symbol in self._all_symbols
                self._realtime_unresolved.add(symbol)
                if not was_known:
                    logger.info(f"[COND] [출처불명] 편입: {symbol} (실시간 이벤트, "
                                f"어느 조건식인지 불확실 — 다음 재조회 시 확정)")
                elif not was_unresolved:
                    logger.info(f"[COND] [출처불명] {symbol} 실시간 이벤트 수신 — "
                                f"이미 알려진 종목이지만 이 이벤트의 정확한 출처는 "
                                f"불명(다른 조건식일 수 있음) — 신뢰도를 재확정 "
                                f"전까지 하향")
                if not was_unresolved:
                    self._notify()

            elif action == "D":
                # 2026-08-05 (3차 GPT 코드리뷰 지적 P0-2 문제B): 이
                # 종목이 들어있는 모든 버킷(seq별 confirmed +
                # unresolved)에서 제거 — 주의: 이건 "정확한 조건식별
                # 편출"이 아니라 "출처불명 D는 안전을 위해 전체
                # targets에서 제거하는 보수적 정책"임. 두 조건식에
                # 동시 편입된 종목이 한쪽에서만 편출된 경우에도
                # 이 종목 전체가 targets에서 사라짐 — REAL 메시지가
                # 어느 조건식의 D인지 알 방법이 없는 한, "실수로
                # 계속 감시하는 것"보다 "실수로 조기 제외하는 것"이
                # 더 안전하다는 판단(신규 진입 후보를 못 보는 게,
                # 편출됐어야 할 종목을 계속 신호 판단 대상으로
                # 남기는 것보다 낫다는 원칙).
                removed_from_any = False
                for seq_key, sym_set in self._confirmed_symbols_by_seq.items():
                    if symbol in sym_set:
                        sym_set.discard(symbol)
                        removed_from_any = True
                if symbol in self._realtime_unresolved:
                    self._realtime_unresolved.discard(symbol)
                    removed_from_any = True
                if removed_from_any:
                    logger.info(f"[COND] [출처불명] 편출: {symbol} (실시간 이벤트, "
                                f"보수적 전체 제거 정책) — targets에서 제거")
                    self._notify()

    def _notify(self) -> None:
        self.on_symbols_changed(sorted(self._all_symbols))
