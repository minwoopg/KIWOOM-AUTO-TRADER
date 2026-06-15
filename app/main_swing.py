"""
스윙 전략 독립 실행.

python -m app.main_swing
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def load_swing_settings():
    """settings_swing.yaml 로드."""
    import re
    import yaml

    yaml_path = Path("config/settings_swing.yaml")
    text = yaml_path.read_text(encoding="utf-8")

    # 환경변수 치환
    def replace_env(m):
        key = m.group(1)
        return os.environ.get(key, "")

    text = re.sub(r"\$\{([^}]+)\}", replace_env, text)
    return yaml.safe_load(text)


def build_logger(log_path: str, level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("swing")
    logger.setLevel(getattr(logging, level, logging.INFO))
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


async def main() -> None:
    load_dotenv()
    raw = load_swing_settings()

    # ── 로거 ────────────────────────────────────────────────────
    log_path = raw.get("swing_storage", {}).get("app_log", "logs/app_swing.log")
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    logger = build_logger(log_path)
    logger.info("=" * 50)
    logger.info("  스윙 전략 시작")
    logger.info("=" * 50)

    # ── 브로커 ──────────────────────────────────────────────────
    from config.settings import BrokerConfig
    from infra.broker.kiwoom_broker import KiwoomBroker

    broker_cfg = raw["broker"]
    broker = KiwoomBroker(
        config=BrokerConfig(
            provider         = broker_cfg.get("provider", "kiwoom"),
            use_mock         = broker_cfg.get("use_mock", True),
            base_url         = broker_cfg["base_url"],
            app_key          = broker_cfg["app_key"],
            secret_key       = broker_cfg["secret_key"],
            account_number   = broker_cfg.get("account_number", ""),
            is_paper_trading = broker_cfg.get("is_paper_trading", True),
        )
    )
    broker.authenticate()
    logger.info("[SWING] 브로커 인증 완료")

    # ── SwingAnalyzer ────────────────────────────────────────────
    from domain.swing.swing_analyzer import SwingAnalyzer

    cfg_entry = raw["swing_entry"]
    cfg_exit  = raw["swing_exit"]
    cfg_gap   = raw["swing_gap_down"]
    watchlist = raw.get("swing_watchlist", [])

    analyzer = SwingAnalyzer(
        ma10_dist_min               = cfg_entry["ma10_distance_min_pct"],
        ma10_dist_max               = cfg_entry["ma10_distance_max_pct"],
        require_ma20_rising         = cfg_entry["require_ma20_rising"],
        block_if_below_ma20         = cfg_entry["block_if_below_ma20"],
        max_52w_drawdown            = cfg_entry["max_drawdown_from_52w_high_pct"],
        day_rate_min                = cfg_entry["day_rate_min_pct"],
        day_rate_max                = cfg_entry["day_rate_max_pct"],
        min_trading_value           = cfg_entry["min_trading_value"],
        min_volume_ratio_20d        = cfg_entry["min_volume_ratio_20d"],
        block_if_close_near_low_pct = cfg_entry["block_if_close_near_low_pct"],
        min_score                   = cfg_entry["min_score"],
        watchlist                   = watchlist,
    )

    # ── SwingStrategy ────────────────────────────────────────────
    from domain.swing.swing_strategy import SwingStrategy

    strategy = SwingStrategy(
        no_cushion_stop_loss_pct       = cfg_exit["no_cushion_stop_loss_pct"],
        cushion_trigger_pct            = cfg_exit["cushion_trigger_profit_pct"],
        cushion_partial_drawdown_pct   = cfg_exit["cushion_partial_drawdown_pct"],
        cushion_partial_ratio          = cfg_exit["cushion_partial_sell_ratio"],
        cushion_full_drawdown_pct      = cfg_exit["cushion_full_drawdown_pct"],
        take_profit_1_pct              = cfg_exit["take_profit_1_pct"],
        take_profit_1_ratio            = cfg_exit["take_profit_1_sell_ratio"],
        after_tp_trailing_pct          = cfg_exit["after_take_profit_trailing_drawdown_pct"],
        exit_if_below_ma5              = cfg_exit["exit_if_below_daily_ma5"],
        time_stop_days                 = cfg_exit["time_stop_days"],
        time_stop_min_profit_pct       = cfg_exit["time_stop_if_profit_below_pct"],
        gap_down_watch_pct             = cfg_gap["gap_down_watch_pct"],
        gap_down_immediate_pct         = cfg_gap["gap_down_immediate_pct"],
        gap_down_wait_minutes          = cfg_gap["wait_minutes_after_open"],
    )

    # ── 카카오 알림 ──────────────────────────────────────────────
    from infra.notify.kakao_notifier import KakaoNotifier

    kakao_cfg = raw.get("kakao", {})
    notifier = KakaoNotifier(
        access_token  = kakao_cfg.get("access_token",  ""),
        refresh_token = kakao_cfg.get("refresh_token", ""),
        rest_api_key  = kakao_cfg.get("rest_api_key",  ""),
    )

    # ── SwingService ────────────────────────────────────────────
    from domain.service.swing_service import SwingService

    class _SwingSettings:
        swing_entry   = type("E", (), cfg_entry)()
        swing_storage = type("S", (), raw["swing_storage"])()

    service = SwingService(
        broker   = broker,
        analyzer = analyzer,
        strategy = strategy,
        notifier = notifier,
        settings = _SwingSettings(),
        app_logger = logger,
    )

    mode = "실매수" if cfg_entry["trade_enabled"] else "관찰모드"
    notifier.send(f"📊 [스윙 전략 시작] {mode}\n감시: {watchlist}")
    logger.info(f"[SWING] 모드: {mode}")

    # ── 메인 루프 ────────────────────────────────────────────────
    # 감시 종목: watchlist를 기본으로 사용
    # 추후 조건검색식 연동 가능
    symbols = list(watchlist)

    logger.info(f"[SWING] 감시 종목: {symbols}")

    poll = 60  # 60초마다 체크
    while True:
        try:
            now = datetime.now()

            # 장 시간 외 스킵
            if now.hour < 9 or (now.hour >= 15 and now.minute >= 25):
                await asyncio.sleep(poll)
                continue

            # 15:10~15:20 진입 스캔
            now_str = now.strftime("%H:%M")
            if cfg_entry["start_time"] <= now_str <= cfg_entry["end_time"]:
                service.run_entry_scan(symbols)

            # 보유 포지션 매도 체크 (장중 상시)
            if 9 <= now.hour < 15:
                service.run_exit_check(date.today())

            await asyncio.sleep(poll)

        except KeyboardInterrupt:
            logger.info("[SWING] 사용자 종료")
            break
        except Exception as e:
            logger.error(f"[SWING] 루프 오류: {e}", exc_info=True)
            await asyncio.sleep(poll)


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
