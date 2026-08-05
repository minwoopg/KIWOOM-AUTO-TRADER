from __future__ import annotations

"""프로그램 시작점.

실행 순서:
1. .env / settings.yaml 로드
2. 브로커 인증
3. TradingService 조립
4. websocket.enabled=true면 ConditionWatcher도 함께 실행
   - WebSocket 루프와 REST 루프를 asyncio로 병렬 실행
5. websocket.enabled=false면 기존 방식(수동 종목)으로 동작
"""

import asyncio
import logging
import sys
import os
import shutil
import time
from pathlib import Path

from config.settings import Settings, load_settings
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.risk.risk_manager import RiskManager
from domain.service.trading_service import TradingService
from domain.strategy.strategy_router import StrategyRouter
from infra.broker.kiwoom_broker import KiwoomBroker
from infra.broker.mock_broker import MockBroker
from infra.storage.logger import TradeCsvLogger, SignalCsvLogger, build_app_logger
from infra.storage.state_reconciler import StateReconciler
from infra.storage.state_store import JsonStateStore
from utils.time_utils import is_market_open, seconds_until_market_open
import json
from datetime import datetime


def load_dotenv(path: str = ".env") -> None:
    dotenv_path = Path(path)
    if not dotenv_path.exists():
        return
    for line in dotenv_path.read_text(encoding="utf-8").splitlines():
        if not line or line.strip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def build_broker(settings: Settings):
    if settings.broker.use_mock:
        return MockBroker()
    return KiwoomBroker(settings.broker)


def build_trading_service(settings, broker, app_logger, trade_logger, signal_logger, state_store):
    strategy_router   = StrategyRouter(settings.strategy)
    regime_classifier = MarketRegimeClassifier(settings.market_regime)
    risk_manager      = RiskManager(settings.trading, settings.risk, settings.storage.trade_log_file)
    return TradingService(
        settings=settings,
        broker=broker,
        strategy_router=strategy_router,
        regime_classifier=regime_classifier,
        risk_manager=risk_manager,
        app_logger=app_logger,
        trade_logger=trade_logger,
        signal_logger=signal_logger,
        state_store=state_store,
    )


# ── REST 루프 ────────────────────────────────────────────────────

async def trading_loop(trading_service: TradingService, settings: Settings, app_logger) -> None:
    """REST API 기반 매매 루프 (asyncio 버전)."""

    # ── 장 시작 전 대기 ──────────────────────────────────────
    wait_sec = seconds_until_market_open()
    if wait_sec > 0:
        wait_min = int(wait_sec // 60)
        app_logger.info(
            f"application ready — 장 시작까지 {wait_min}분 대기 중 (09:00 시작)"
        )
        await asyncio.sleep(wait_sec)
        app_logger.info("장 시작 — 매매 루프 시작")
        # 2026-07-20: reset_daily_loss_counts() 명시 호출 제거 — run_once()가
        # 매 폴링마다 날짜변경을 직접 감지해서 확실하게 호출하도록 바뀌어
        # 여기서 조건부로 호출하던 건 중복(그리고 프로세스가 이미 떠있던
        # 경우엔 이 분기 자체를 안 타서 리셋이 누락되던 버그의 원인이었음)
    else:
        app_logger.info("application started (장중 실행)")

    poll = settings.trading.poll_interval_seconds

    while True:
        try:
            from datetime import datetime as _dt
            now = _dt.now()
            if is_market_open() or settings.broker.use_mock:
                await trading_service.run_once()
            else:
                # 장 외 시간 — 대기 메시지 (분 단위로 한 번)
                if now.second < poll:
                    app_logger.info(
                        f"[WAIT] 장 외 시간 ({now.strftime('%H:%M')}) — "
                        f"09:00 장 시작까지 대기 중"
                    )
                # 리포트는 15:25 이후 생성 (마지막 체결 기록 완료 후)
                if now.hour > 15 or (now.hour == 15 and now.minute >= 25):
                    trading_service._run_end_of_day_tasks(now)
            await asyncio.sleep(poll)

        except (asyncio.CancelledError, KeyboardInterrupt):
            app_logger.info("application stopped by user")
            break

        except Exception as exc:
            app_logger.exception("unexpected error: %s", exc)
            msg = str(exc)
            if "http=429" in msg or "허용된 요청 개수를 초과" in msg:
                app_logger.warning("rate limit detected, backing off for 180 seconds")
                await asyncio.sleep(180)
            else:
                await asyncio.sleep(poll)


# ── 메인 ────────────────────────────────────────────────────────

async def async_main() -> None:
    load_dotenv()

    # 구버전 .pyc 캐시가 남아 AttributeError를 일으키는 것을 방지합니다.
    # 업데이트 후 첫 실행 시 자동으로 재컴파일됩니다.
    for cache_dir in Path(".").rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)

    settings = load_settings()

    app_logger   = build_app_logger(settings.storage.app_log_file, settings.app.log_level)
    print("=" * 50)
    print("  키움 자동매매 시스템 (단타) 시작")
    print(f"  app.log: {settings.storage.app_log_file}")
    print("=" * 50)
    trade_logger  = TradeCsvLogger(settings.storage.trade_log_file)
    signal_logger = SignalCsvLogger(settings.storage.signal_log_file)
    state_store  = JsonStateStore(settings.storage.state_file)

    broker = build_broker(settings)

    # ── 시작 시 429 재시도 래퍼 ─────────────────────────────
    async def _retry_on_429(fn, desc: str, max_retries: int = 10):
        """429 에러 발생 시 최대 max_retries회 재시도합니다."""
        for attempt in range(1, max_retries + 1):
            try:
                return fn()
            except Exception as e:
                msg = str(e)
                if "http=429" in msg or "허용된 요청 개수를 초과" in msg:
                    wait = min(30 * attempt, 180)  # 30초 → 60초 → ... → 최대 180초
                    app_logger.warning(
                        f"[STARTUP] {desc} 429 에러 "
                        f"({attempt}/{max_retries}회) — {wait}초 후 재시도"
                    )
                    await asyncio.sleep(wait)
                else:
                    raise
        raise RuntimeError(f"{desc} 최대 재시도 초과")

    await _retry_on_429(broker.authenticate, "인증")

    # ── 시작 시 state.json과 실제 잔고 동기화 ──────────────
    try:
        balance_init = await _retry_on_429(broker.get_account_balance, "잔고조회")
        reconciler   = StateReconciler(app_logger)
        state, highest_price = state_store.load()
        state, highest_price = reconciler.reconcile(state, highest_price, balance_init)
        state_store.save(state, highest_price)
        app_logger.info("[RECONCILE] state.json 동기화 완료")
    except Exception as e:
        app_logger.warning(f"[RECONCILE] 시작 시 state 동기화 실패: {e}")
        # 2026-07-22: 기존엔 모의/실전 구분 없이 경고 로그만 남기고
        # 그대로 매매를 시작했음 — 실제 보유 종목과 state.json이
        # 불일치한 채로(예: 실제로는 보유 중인데 로컬에는 없음, 또는
        # 그 반대) 신규매매를 시작할 위험이 있음(GPT 검토로 발견).
        # 실전투자에서는 이 상태로 시작하는 것 자체가 위험하므로 프로세스
        # 시작을 중단. 모의투자는 리스크가 없으므로 기존처럼 경고만
        # 남기고 진행(개발/디버깅 편의를 위해 완전히 막지는 않음).
        if not settings.broker.is_paper_trading:
            app_logger.critical(
                "[STARTUP_BLOCK] 실전투자 — 계좌 상태 동기화 실패로 "
                "안전하게 시작할 수 없습니다. 프로그램을 시작하지 않습니다."
            )
            raise RuntimeError(
                "실전투자 시작 시 잔고/state 동기화 실패 — 실제 보유"
                "종목과 로컬 상태가 불일치한 채로 매매를 시작하지 않도록"
                "의도적으로 중단합니다."
            ) from e

    trading_service = build_trading_service(
        settings, broker, app_logger, trade_logger, signal_logger, state_store
    )

    # ── 시작 알림 ────────────────────────────────────────────────
    from infra.notify.kakao_notifier import build_notifier
    from datetime import datetime as _dt_notify
    _notifier = build_notifier(settings)
    _now_str  = _dt_notify.now().strftime('%H:%M')
    _mode     = '모의투자' if settings.broker.is_paper_trading else '실전투자'
    _notifier.send(
        f"🚀 자동매매 시작\n"
        f"시각: {_now_str} | 모드: {_mode}\n"
        f"감시 종목: 조건검색식 {settings.websocket.condition_seqs}"
    )

    # ── WebSocket 조건검색 활성화 여부 ───────────────────────────
    if settings.websocket.enabled:
        from infra.websocket.condition_watcher import ConditionWatcher
        from infra.websocket.real_token import fetch_real_token

        # 수동 고정 종목 (settings.yaml의 targets)
        manual_symbols = settings.targets

        def on_symbols_changed(symbols: list[str]) -> None:
            # ── 단타 seq에 속한 종목만 추림 (스윙 seq 종목은 단타 targets에서 제외) ──
            # 어제 스윙 seq를 같은 WebSocket으로 합쳐 구독하면서, 콜백의 symbols에
            # 스윙용 종목까지 섞여 들어와 단타 targets를 오염시키는 문제가 있었음.
            day_seqs_set = {str(s) for s in settings.websocket.condition_seqs}
            day_symbols: list[str] = []
            for seq, syms in watcher._symbols_by_seq.items():
                if seq in day_seqs_set:
                    for s in syms:
                        if s not in day_symbols:
                            day_symbols.append(s)

            # 자동 제외된 종목은 재편입 차단
            excluded = trading_service.get_excluded_symbols()
            filtered = [s for s in day_symbols if s not in excluded]
            combined = list(dict.fromkeys(manual_symbols + filtered))
            limited  = combined[:settings.websocket.max_symbols]
            sym_to_cond = watcher.symbol_to_condition
            # 2026-08-05 (GPT 코드리뷰 지적, VWAP shadow 1단계):
            # 복수 조건식 편입 정보를 보존하기 위해 symbol_to_
            # conditions(복수형)도 함께 전달 — update_targets()가
            # 이걸 매 폴링마다 통째로 교체하므로, 편출된 종목의
            # 과거 조건식 이름이 잔존하지 않음.
            sym_to_conditions = watcher.symbol_to_conditions
            # 2026-08-05 (2차 GPT 코드리뷰 지적, 1번): 조건식 출처
            # 신뢰도도 함께 전달 — 실시간 이벤트로만 알려져 어느
            # 조건식인지 불확실한 종목은 이 값이 False가 되고,
            # VWAP shadow의 condition-source 기반 판단에서 제외됨.
            sym_to_reliable = watcher.symbol_condition_source_reliable
            trading_service.update_targets(limited, sym_to_cond, sym_to_conditions, sym_to_reliable)
            blocked = excluded & set(day_symbols)
            if blocked:
                app_logger.info(f"[COND] 제외 종목 재편입 차단: {sorted(blocked)}")
            # ── 조건검색식별 편입 현황 + final_targets 로그 ──
            seq_info = " | ".join(
                f"seq{seq}={len(syms)}종목"
                for seq, syms in watcher._symbols_by_seq.items()
            )
            app_logger.info(
                f"[COND_STATUS] {seq_info} | "
                f"excluded={len(blocked)}차단 | "
                f"final={len(limited)}종목: {limited}"
            )

            # ── 스윙용 검색식 결과만 분리해서 파일로 저장 ───────
            # main_swing.py(별도 프로세스)가 이 파일을 읽어 watchlist와 합침.
            swing_seqs_set = {str(s) for s in settings.websocket.swing_condition_seqs}
            if swing_seqs_set:
                swing_symbols: set[str] = set()
                for seq, syms in watcher._symbols_by_seq.items():
                    if seq in swing_seqs_set:
                        swing_symbols |= syms
                out_path = Path(settings.websocket.swing_condition_output)
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(
                    json.dumps({
                        "updated_at": datetime.now().isoformat(),
                        "symbols": sorted(swing_symbols),
                    }, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                app_logger.info(
                    f"[COND_SWING] 스윙용 검색식 결과 저장 "
                    f"({len(swing_symbols)}종목) → {out_path}"
                )

        # 조건검색은 실전 계좌 토큰으로 별도 발급
        app_logger.info("[COND] 실전 계좌 토큰 발급 중...")
        real_token = fetch_real_token(
            app_key=settings.websocket.app_key,
            secret_key=settings.websocket.secret_key,
        )
        app_logger.info("[COND] 실전 계좌 토큰 발급 완료")

        # ── 스윙용 조건검색 seq를 단타 구독 목록에 합침 ──────────
        # (같은 WebSocket 연결로 동시 구독 — 별도 연결 불필요)
        import dataclasses
        swing_seqs = settings.websocket.swing_condition_seqs
        combined_seqs = list(dict.fromkeys(
            list(settings.websocket.condition_seqs) + list(swing_seqs)
        ))
        ws_config_combined = dataclasses.replace(
            settings.websocket, condition_seqs=combined_seqs,
        )
        if swing_seqs:
            app_logger.info(
                f"[COND] 스윙용 검색식 seq={swing_seqs} 추가 구독 "
                f"(결과는 {settings.websocket.swing_condition_output}에 저장)"
            )

        watcher = ConditionWatcher(
            config=ws_config_combined,
            token=real_token,
            on_symbols_changed=on_symbols_changed,
        )

        seqs_str = ", ".join(str(s) for s in settings.websocket.condition_seqs)
        app_logger.info(
            f"[COND] 조건검색 모드 활성화 "
            f"(조건식 번호: {seqs_str})"
        )
        app_logger.info("[COND] 종목은 조건검색으로 자동 설정됩니다")

        async def watcher_start_guarded() -> None:
            # watcher가 조용히 죽는 문제 진단용 — 예외를 반드시 로그로 노출
            try:
                app_logger.info("[COND] watcher.start() 진입 — WebSocket 연결 시작")
                await watcher.start()
                # 2026-07-22 (GPT 코드리뷰): watcher.start()는 원래
                # 무한 재연결 루프라 정상 운영 중 스스로 반환하는 일이
                # 없어야 함 — 예상된 종료 경로는 오직 CancelledError
                # (task.cancel()로 명시적으로 취소되는 경우)뿐. 여기까지
                # 오는 건 내부 루프가 어떤 이유로 조용히 끝났다는 뜻이라
                # 실패로 간주 — 로그만 남기고 넘어가면 asyncio.wait의
                # done 집합에 "정상 완료"로 들어가 장애가 감춰짐.
                raise RuntimeError(
                    "ConditionWatcher가 예외 없이 예상보다 일찍 종료됐습니다"
                )
            except asyncio.CancelledError:
                app_logger.info("[COND] watcher 태스크 취소됨 — 종료 처리 중")
                raise
            except Exception as exc:
                app_logger.exception(f"[COND] watcher.start() 예외로 중단: {exc}")
                # 2026-07-22: 로그만 남기고 조용히 반환하고 있었음(GPT
                # 코드리뷰로 발견) — 그러면 asyncio.wait의 done 집합에는
                # watcher_task가 "정상 완료"로 들어가고, 뒤이은
                # trading_task 취소까지는 정확히 동작하지만, 운영체제
                # 입장에서는 프로세스가 정상 종료 코드(0)로 끝날 수 있어
                # 프로세스 관리자나 모니터링에서 장애 종료를 구분하기
                # 어려움. 예외를 다시 던져 task.exception()에 정확히
                # 남도록 함 — 아래 asyncio.wait 이후의
                # "for task in done: if task.exception()..." 로직이
                # 이 예외를 그대로 재전파해 main()까지 도달, 0이 아닌
                # 종료 코드로 끝나게 됨.
                raise

        # 2026-07-22: asyncio.gather는 모든 태스크가 끝나야 반환됨. 기존엔
        # trading_loop가 KeyboardInterrupt를 자체적으로 잡아 break로 조용히
        # 반환하는 구조라(위 111~113줄 참고), Ctrl+C를 눌러도 watcher 태스크는
        # gather 안에서 계속 살아남아 5초마다 재연결을 반복 — "application
        # stopped by user" 로그 이후에도 [WS] 연결 시도가 계속되던 원인.
        # asyncio.wait(FIRST_COMPLETED)로 바꿔서, 어느 한쪽이 먼저 끝나면
        # (정상/예외 불문) 나머지를 명시적으로 취소하고 정리한 뒤 반환하도록 함.
        trading_task = asyncio.create_task(trading_loop(trading_service, settings, app_logger))
        watcher_task = asyncio.create_task(watcher_start_guarded())

        done, pending = await asyncio.wait(
            {trading_task, watcher_task}, return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        # watcher가 소켓을 열어둔 채로 남지 않도록 명시적으로 정리
        # 2026-07-22 (5차 수정, GPT 코드리뷰): timeout 없이 대기하면
        # 내부 네트워크/소켓 문제로 stop()이 멈췄을 때 프로세스 종료
        # 자체가 지연될 수 있음 — 5초 제한.
        try:
            await asyncio.wait_for(watcher.stop(), timeout=5.0)
        except asyncio.TimeoutError:
            app_logger.warning("[COND] watcher.stop() 5초 초과 — 정리를 포기하고 계속 진행")
        except Exception as exc:
            app_logger.warning(f"[COND] watcher.stop() 중 예외 (무시): {exc}")

        # 먼저 끝난 태스크가 예외로 죽었다면 그 예외를 드러냄
        for task in done:
            if task.exception() is not None:
                raise task.exception()

    else:
        # 기존 방식: settings.yaml의 targets 그대로 사용
        app_logger.info(f"loaded targets: {settings.targets}")
        await trading_loop(trading_service, settings, app_logger)


def main() -> int:
    """프로그램 진입점. 반환값은 프로세스 종료 코드로 사용됩니다.

    2026-07-22 (GPT 코드리뷰로 발견): 기존엔 이 함수가 -> None이었고
    except Exception 블록이 print()만 하고 끝나서, watcher가 예외로
    죽어 async_main()까지 예외가 전파돼도 main()이 그걸 삼키고 정상
    반환 -> 프로세스 종료 코드가 0으로 남았음(7.19/7.24절에서 고친
    "watcher 예외 재전파"가 무의미해지는 결과). 프로세스 관리자나
    모니터링이 정상 종료와 장애 종료를 구분할 수 있도록 종료 코드를
    명시적으로 반환하고, if __name__ 블록에서 sys.exit()로 실제
    반영합니다.
    """
    exit_code = 0
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        print("\n[종료] Ctrl+C 감지 — 정상 종료 처리 중...")
    except Exception as exc:
        # 실전투자 시작 시 잔고동기화 실패(STARTUP_BLOCK)나 watcher
        # 장애로 인한 예외 등이 여기까지 올라옴 — app.log에는 이미
        # CRITICAL/exception으로 남지만, 콘솔에도 명확히 보이도록 출력.
        # 2026-07-22 (4차 수정, GPT 코드리뷰): async_main() 내부에서
        # 예상 못한 경로로 발생한 예외는 여기서 처음 잡힐 수도 있는데,
        # 그런 경우 print()만으로는 스택트레이스가 app.log에 안 남을
        # 수 있어 원인 추적이 어려움 — logging.exception()으로 한 번
        # 더 명시적으로 기록.
        logging.getLogger(__name__).exception("자동매매 프로그램 치명적 오류")
        print(f"\n[오류] 프로그램이 비정상 종료됐습니다: {exc}")
        exit_code = 1
    finally:
        # 파일 핸들러를 명시적으로 flush/close.
        # (Ctrl+C 시 close() 없이 종료되면 다음 실행 때 app.log에
        #  로그가 안 찍히는 것처럼 보이는 문제의 원인이었음)
        logging.shutdown()
        print("[종료] 로그 정리 완료. 프로그램을 종료합니다.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
