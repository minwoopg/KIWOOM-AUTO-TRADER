from __future__ import annotations

"""앱 로그, 거래 로그, 시그널 로그를 관리하는 모듈."""

import csv
import logging
import logging.handlers
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

AppLogger = logging.Logger

# 단일 파일 최대 20MB, 최근 10개(최대 200MB)까지만 보관 후 압축 없이 순환.
# 기존엔 FileHandler 하나로 무한정 append만 해서 200MB까지 불어난 상태였음.
APP_LOG_MAX_BYTES = 20 * 1024 * 1024
APP_LOG_BACKUP_COUNT = 10


def build_app_logger(log_file: str, level: str = "INFO") -> AppLogger:
    """파일 기반 앱 로거를 생성합니다.

    프로젝트 전체 로그(app_logger + infra.* / domain.* 모듈 로거)를
    하나의 app.log로 모으기 위해, 파일 핸들러를 루트 로거에 붙입니다.
    그동안 condition_watcher / kiwoom_ws 의 [COND]/[WS] 로그가
    app.log에 안 찍히던 원인을 해결합니다.
    """

    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    # 루트 로거에 파일 핸들러를 붙여 모든 하위 로거의 로그를 한곳에 모음
    root_logger = logging.getLogger()
    root_logger.setLevel(level.upper())
    target_path = str(Path(log_file).resolve())
    already_has_file = any(
        isinstance(h, (logging.FileHandler, logging.handlers.RotatingFileHandler))
        and getattr(h, "baseFilename", "") == target_path
        for h in root_logger.handlers
    )
    if not already_has_file:
        root_file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=APP_LOG_MAX_BYTES,
            backupCount=APP_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        root_file_handler.setFormatter(formatter)
        root_logger.addHandler(root_file_handler)
        # 콘솔에는 로그를 흘리지 않음 — 전량 출력은 터미널을 뒤덮어 오히려
        # 실행 확인을 방해함. 실행 확인은 main.py의 시작 배너(print)로 대신함.

        # 외부 라이브러리의 과도한 로그는 억제
        logging.getLogger("websockets").setLevel(logging.WARNING)
        logging.getLogger("asyncio").setLevel(logging.WARNING)

    # app_logger는 별도 핸들러 없이 루트 핸들러로 전파(propagate)시켜 중복 방지
    logger = logging.getLogger("kiwoom_auto_trader")
    logger.setLevel(level.upper())
    logger.handlers.clear()      # 기존에 직접 붙인 핸들러 제거 (중복 방지)
    logger.propagate = True

    return logger


# ── trades.csv ───────────────────────────────────────────────────────────────
# 기존 필드 + 매수 당시 판단 근거 컨텍스트 필드

TRADE_FIELDS = [
    # 기존 필드
    "timestamp", "symbol", "side", "quantity", "price", "accepted", "message", "order_id",
    # 컨텍스트 필드 (매수 시 기록, 매도 시 exit_reason/hold_minutes 추가)
    "entry_strategy",        # 전략명 (breakout / neutral / hold)
    "market_regime",         # 장세 (BULLISH / NEUTRAL / UNKNOWN)
    "entry_score",           # 점수 (0~8)
    "entry_reason",          # 진입 사유 요약
    "is_v_rebound",          # V자 반등 여부
    "is_pulldown_recovery",  # 눌림목 재상승 여부
    "v_drop_pct",            # 낙폭 (%)
    "v_rise_pct",            # 반등폭 (%)
    "v_low_age",             # 저점 나이 (봉 수)
    "current_vs_vwap_pct",   # 현재가 vs VWAP (%)
    "volume_ratio",          # 반등 구간 거래량 비율
    "bar_amount",            # 현재봉 거래대금 (원)
    "rebound_volume_spike",  # 반등봉 거래량 급등 여부 (매수세 유입 핵심)
    "v_bottom_spike",        # 저점봉 거래량 급등 여부 (투매 확인 보조)
    "upside_to_recent_high_pct",  # 현재가→최근 고점 상승 여력 (%)
    "exit_reason",           # 매도 사유 (손절/트레일링/추세꺾임/강제청산 등)
    "hold_minutes",          # 보유 시간 (분)
    "avg_buy_price",         # 잔고API 기준 평균매입단가 (손익 계산용)
    "condition_name",         # 조건검색식 이름
]


class TradeCsvLogger:
    """주문/체결 결과를 CSV 파일로 남기는 로거입니다."""

    def __init__(self, file_path: str) -> None:
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.file_path.exists():
            with self.file_path.open("w", newline="", encoding="utf-8") as fp:
                writer = csv.DictWriter(fp, fieldnames=TRADE_FIELDS)
                writer.writeheader()

    def append(self, row: dict[str, Any]) -> None:
        """거래 로그 한 줄을 CSV 파일에 추가합니다."""
        with self.file_path.open("a", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(fp, fieldnames=TRADE_FIELDS, extrasaction="ignore")
            row.setdefault("price", 0)
            # 컨텍스트 필드 기본값 (매도 행이거나 컨텍스트 없는 경우)
            for field in TRADE_FIELDS[8:]:
                row.setdefault(field, "")
            writer.writerow(row)


# ── signal_log.csv ───────────────────────────────────────────────────────────
# 매수·보류 불문 모든 시그널 판단을 기록 → 파라미터 검증의 근거 데이터

SIGNAL_FIELDS = [
    "timestamp",         # 판단 시각
    "symbol",            # 종목코드
    "price",             # 현재가
    "regime",            # 장세
    "score",             # 점수
    "signal",            # BUY / HOLD / SKIP (전략 판단)
    "final_decision",    # 실제 결과: BUY / HOLD / BLOCKED
    "order_block_reason",# 차단 사유: AFTER_1450 / REENTRY_COOLDOWN / RISK_LIMIT 등
    "condition_name",    # 조건검색식 이름 (자동매매_돌파형A / 눌림목_PR / V자_BV)
    "skip_reason",       # HOLD/SKIP 사유 (전략 reason 그대로)
    "detected_patterns", # 감지된 패턴 목록 (V / PR / A / B / C)
    "is_v_rebound",
    "is_pulldown_recovery",
    "v_drop_pct",
    "v_rise_pct",
    "v_low_age",
    "current_vs_vwap_pct",
    "volume_ratio",
    "bar_amount",
    "rebound_volume_spike",
    "rebound_volume_ratio",
    "change_rate_pct",
    "v_bottom_spike",
    "upside_to_recent_high_pct",
    "ma5_above_ma20",
    "v_fail_reason",      # V자 실패 사유 (V_FAIL_DROP_TOO_SMALL 등)
    # ATR / 볼린저 지표 (로그 전용 — 매수 차단 없이 기록)
    "atr_14",             # ATR(14) 절대값 (원화)
    "atr_14_pct",         # ATR(14) / 현재가 × 100 (%)
    "bb_percent_b",       # 볼린저 %B (0=하단, 1=상단)
    "bb_bandwidth_pct",   # 볼린저 밴드폭 (%)
    "bb_position",        # 볼린저 위치 (LOWER_ZONE / MID_UPPER 등)
]


class SignalCsvLogger:
    """모든 시그널 판단 결과를 signal_log.csv에 기록합니다.

    매수된 종목뿐 아니라 탈락된 종목도 기록하여
    추후 파라미터 검증의 근거 데이터로 활용합니다.
    """

    def __init__(self, file_path: str) -> None:
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.file_path.exists():
            with self.file_path.open("w", newline="", encoding="utf-8") as fp:
                writer = csv.DictWriter(fp, fieldnames=SIGNAL_FIELDS)
                writer.writeheader()
            logger.info(f"[SIGNAL_LOG] 신규 생성: {self.file_path} ({len(SIGNAL_FIELDS)}개 컬럼)")
        else:
            self._migrate_header_if_needed()

    def _migrate_header_if_needed(self) -> None:
        """
        기존 CSV의 헤더에 새 필드(atr_14 등)가 없으면 헤더를 갱신합니다.

        extrasaction='ignore' 때문에, 헤더에 없는 컬럼은 조용히 버려집니다.
        따라서 SIGNAL_FIELDS에 컬럼을 추가해도 기존 파일은 그대로면
        새 데이터가 영영 안 들어갑니다. 이 함수가 그 격차를 메웁니다.

        주의: 기존 파일이 utf-8-sig(BOM 포함)로 쓰였을 수 있으므로
        반드시 utf-8-sig로 읽어야 첫 컬럼(timestamp)의 키가 BOM 때문에
        깨지지 않는다. (utf-8로 읽으면 '\ufefftimestamp'가 되어 값이 유실됨)
        """
        try:
            with self.file_path.open("r", newline="", encoding="utf-8-sig") as fp:
                reader = csv.reader(fp)
                existing_header = next(reader, [])
        except (StopIteration, OSError) as exc:
            logger.warning(f"[SIGNAL_LOG] 헤더 확인 실패 — 마이그레이션 건너뜀: {exc}")
            return

        # 헤더가 이미 최신이면 아무것도 안 함
        missing = [f for f in SIGNAL_FIELDS if f not in existing_header]
        if not missing:
            logger.info(f"[SIGNAL_LOG] 헤더 최신 상태 확인 ({len(existing_header)}개 컬럼) — 마이그레이션 불필요")
            return

        logger.info(
            f"[SIGNAL_LOG] 헤더 마이그레이션 시작 — 누락 컬럼 {len(missing)}개: {missing}"
        )

        # 기존 데이터를 모두 읽어서 DictReader로 파싱 (utf-8-sig로 BOM 제거)
        with self.file_path.open("r", newline="", encoding="utf-8-sig") as fp:
            old_rows = list(csv.DictReader(fp))

        # 새 헤더 + 기존 행(없는 컬럼은 빈 값)으로 전체 재작성
        with self.file_path.open("w", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(fp, fieldnames=SIGNAL_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for old_row in old_rows:
                for field in SIGNAL_FIELDS:
                    old_row.setdefault(field, "")
                writer.writerow(old_row)

        logger.info(
            f"[SIGNAL_LOG] 헤더 마이그레이션 완료 — {len(old_rows):,}행 재작성, "
            f"컬럼 {len(existing_header)}개 → {len(SIGNAL_FIELDS)}개"
        )

    def append(self, row: dict[str, Any]) -> None:
        """시그널 로그 한 줄을 추가합니다."""
        with self.file_path.open("a", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(fp, fieldnames=SIGNAL_FIELDS, extrasaction="ignore")
            for field in SIGNAL_FIELDS:
                row.setdefault(field, "")
            writer.writerow(row)
