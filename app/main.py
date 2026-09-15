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

from app.target_selection import compute_day_targets
from config.settings import Settings, load_settings
from domain.market_regime.classifier import MarketRegimeClassifier
from domain.risk.risk_manager import RiskManager
from domain.service.trading_service import TradingService
from domain.strategy.strategy_router import StrategyRouter
from infra.broker.kiwoom_broker import KiwoomBroker
from infra.broker.mock_broker import MockBroker
from infra.storage.logger import TradeCsvLogger, SignalCsvLogger, build_app_logger
from infra.storage.run_baseline import perform_run_baseline_startup
from infra.storage.state_reconciler import StateReconciler
from infra.storage.state_store import JsonStateStore
from infra.storage.process_lock import single_instance_lock
from utils.time_utils import is_market_open, seconds_until_market_open, now_local


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


def build_trading_service(settings, broker, app_logger, trade_logger, signal_logger, state_store, notifier=None):
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
        # 2026-09-11 (GPT reclosure): 시작 알림/계정 자체 점검에 쓰는
        # notifier와 매수/매도 알림에 쓰는 notifier가 서로 다른
        # KakaoNotifier 인스턴스면, 한쪽이 토큰을 갱신해도 다른 쪽은
        # 예전 토큰(카카오가 회전 시 폐기하는 refresh_token 포함)을
        # 그대로 들고 있다가 나중에 갱신 실패할 수 있음. 호출부(main.py)가
        # 하나의 notifier를 만들어 여기로 주입해 공유하게 함.
        notifier=notifier,
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
            now = now_local()
            if is_market_open() or settings.broker.use_mock:
                await trading_service.run_once()
            else:
                # A last order can fill after the order window closes.
                # Keep its state/side effects current without generating orders.
                trading_service.reconcile_after_market_close()
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
                # 2026-09-15 (180초 감시 공백 대응 3단계 — 관측 경로
                # 연결): 기존엔 이 180초를 한 번에 통째로 잤습니다 —
                # 그동안 run_once()가 전혀 호출되지 않아 보유 종목의
                # 손절·트레일링 판단이 완전히 멈췄습니다("180초 감시
                # 공백"). wait_out_balance_outage()는 총 대기시간(180초,
                # 기존과 동일)은 그대로 두되, 짧은 간격으로 쪼개 매
                # 구간마다 캐시된 잔고·시세만으로 청산 후보를 관측·
                # 기록합니다(TradingService.observe_exit_candidates_
                # during_outage() 참고) — 주문 제출·체결 확정·
                # highest_price 갱신은 여전히 하지 않습니다.
                await trading_service.wait_out_balance_outage()
            else:
                await asyncio.sleep(poll)


# ── 메인 ────────────────────────────────────────────────────────

async def async_main() -> None:
    load_dotenv()
    settings = load_settings()
    with single_instance_lock(Path(settings.storage.state_file).with_suffix(".lock")):
        await _run_application(settings)


async def _run_application(settings: Settings) -> None:

    # 구버전 .pyc 캐시가 남아 AttributeError를 일으키는 것을 방지합니다.
    # 업데이트 후 첫 실행 시 자동으로 재컴파일됩니다.
    for cache_dir in Path(".").rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)

    app_logger   = build_app_logger(settings.storage.app_log_file, settings.app.log_level)
    print("=" * 50)
    print("  키움 자동매매 시스템 (단타) 시작")
    print(f"  app.log: {settings.storage.app_log_file}")
    print("=" * 50)

    # ── B01 (2026-09-11, 개선 체크리스트 0단계): 실행 기준선 기록 ──────
    # run_id/git_sha/git_dirty/effective_config_hash/모의·실전 구분과
    # redacted 설정 스냅샷을 시작 시각에 한 번 기록합니다. 실패해도
    # (git 없음, 디스크 오류 등) 시작을 절대 막지 않도록 전체를
    # best-effort로 처리합니다 — 순수 관측 기능이 매매 시작 자체를
    # 막는 건 본말전도이기 때문입니다. 2026-09-11 (B01 v2 재검토):
    # 원래 이 블록이 여기 그대로 있었는데, 단위 테스트가 가능하도록
    # infra/storage/run_baseline.py의 perform_run_baseline_startup()으로
    # 옮겼습니다(로직 변경 없음, 위치만 이동 — 그 함수의 docstring에
    # config snapshot 저장 실패 시 [CONFIG_SNAPSHOT_MISSING] 경고를
    # 남기는 보완 내용이 있습니다).
    perform_run_baseline_startup(settings, app_logger)

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

    # 2026-09-11 (GPT reclosure): notifier를 여기서 한 번만 만들어
    # TradingService(매수/매도 알림)와 시작 알림/계정 자체 점검이
    # 반드시 같은 KakaoNotifier 인스턴스(=같은 토큰 상태)를 공유하게
    # 합니다. 두 인스턴스로 나뉘면 한쪽이 토큰을 갱신해도 다른 쪽은
    # 예전 토큰(카카오가 회전 시 폐기하는 refresh_token 포함)을 그대로
    # 들고 있다가 나중에 갱신 실패할 수 있습니다.
    from infra.notify.kakao_notifier import build_notifier, send_startup_notification_async
    _notifier = build_notifier(settings)

    trading_service = build_trading_service(
        settings, broker, app_logger, trade_logger, signal_logger, state_store,
        notifier=_notifier,
    )

    # ── 시작 알림 ────────────────────────────────────────────────
    from datetime import datetime as _dt_notify
    _now_str  = _dt_notify.now().strftime('%H:%M')
    _mode     = '모의투자' if settings.broker.is_paper_trading else '실전투자'

    # 2026-09-11 (GPT reclosure): 카카오 알림 대상 계정 자체 점검(여러
    # 사람이 각자 컴퓨터에서 각자 토큰으로 돌리는 상황 대비)을 매매/
    # WebSocket 시작과 완전히 분리된 백그라운드 스레드로 실행합니다.
    # 원래는 여기서 동기식으로 verify_account()를 호출했는데, 그 HTTP
    # 호출(최악 401→refresh→재조회 시 최대 약 15초)이 trading startup
    # critical path를 지연시킬 수 있다는 지적을 받아 분리했습니다 —
    # 이 프로젝트는 장중 재시작이 실제로 종종 발생하므로 이론상
    # 문제가 아닙니다. 실패해도 fail-open, 매매 루프에는 영향 없음.
    send_startup_notification_async(
        _notifier, app_logger, _mode, _now_str, settings.websocket.condition_seqs
    )

    # ── WebSocket 조건검색 활성화 여부 ───────────────────────────
    if settings.websocket.enabled:
        from infra.websocket.condition_watcher import ConditionWatcher
        from infra.websocket.real_token import fetch_real_token

        # 수동 고정 종목 (settings.yaml의 targets)
        manual_symbols = settings.targets

        def on_symbols_changed(symbols: list[str]) -> None:
            # ── 단타 감시 종목 계산 ──────────────────────────────
            # 2026-08-06 (1E.9): 이 계산은 `app/target_selection.py`의
            # 순수 함수로 분리됨 — (a) 1E.8에서 드러난 "app/main.py의
            # 콜백은 어떤 테스트도 실행하지 않는다"는 사각지대를 없애고,
            # (b) 출처 미확정(실시간 편입) 종목이 targets에서 통째로
            # 누락되던 P0 결함을 고치기 위함. 상세 배경은 해당 모듈의
            # compute_day_targets() 주석 참고.
            excluded = trading_service.get_excluded_symbols()
            selection = compute_day_targets(
                confirmed_symbols_by_seq=watcher.confirmed_symbols_by_seq,
                realtime_unresolved=watcher.realtime_unresolved_symbols,
                day_seqs=settings.websocket.condition_seqs,
                manual_symbols=manual_symbols,
                excluded_symbols=excluded,
                max_symbols=settings.websocket.max_symbols,
            )
            day_symbols = selection.day_symbols
            limited = selection.final_targets
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
            blocked = selection.blocked
            if blocked:
                app_logger.info(f"[COND] 제외 종목 재편입 차단: {sorted(blocked)}")
            # ── 조건검색식별 편입 현황 + final_targets 로그 ──
            seq_info = " | ".join(
                f"seq{seq}={len(syms)}종목"
                for seq, syms in sorted(watcher.confirmed_symbols_by_seq.items())
            )
            app_logger.info(
                f"[COND_STATUS] {seq_info} | "
                f"unresolved={len(selection.unresolved_used)}종목 | "
                f"excluded={len(blocked)}차단 | "
                f"eligible_condition_count={selection.eligible_condition_count} "
                f"selected_condition_count={selection.selected_condition_count} "
                f"truncated_condition_count={selection.truncated_condition_count} | "
                f"final={len(limited)}종목: {limited}"
            )
            # 2026-08-06 (1G, GPT 코드리뷰 지적 3번): max_symbols(10)에서
            # 수동 targets(4)를 뺀 자리만 조건검색 종목이 차지하므로
            # 실제 감시 가능한 조건검색 종목은 최대 6개뿐. 상한을 넘기면
            # 종목코드 오름차순으로 앞쪽만 남아, 나중에 편입된 종목이
            # 코드가 크다는 이유로 잘림 — 이 편향이 entry_quality_shadow
            # 표본에도 그대로 반영되므로 발생 즉시 로그로 남겨 크기를
            # 정량 확인할 수 있게 함(선택 로직 변경은 별도 단계).
            if selection.truncated_condition_count:
                app_logger.warning(
                    f"[COND_TRUNCATE] max_symbols={settings.websocket.max_symbols} 상한으로 "
                    f"조건검색 종목 {selection.truncated_condition_count}개가 잘렸습니다"
                    f"(감시 가능 {selection.eligible_condition_count}개 중 "
                    f"{selection.selected_condition_count}개 선택) — "
                    f"현재 선택 기준은 종목코드 오름차순이라 shadow 표본이 편향될 수 있음"
                )

        # 조건검색은 실전 계좌 토큰으로 별도 발급
        app_logger.info("[COND] 실전 계좌 토큰 발급 중...")
        real_token = fetch_real_token(
            app_key=settings.websocket.app_key,
            secret_key=settings.websocket.secret_key,
        )
        app_logger.info("[COND] 실전 계좌 토큰 발급 완료")

        # 2026-08-06 (1F): 스윙 전략 폐기로 스윙 seq 병합 로직 제거 —
        # 구독하는 조건검색식은 전부 단타용이므로 settings.websocket을
        # 그대로 사용합니다(dataclasses.replace 불필요).
        watcher = ConditionWatcher(
            config=settings.websocket,
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
