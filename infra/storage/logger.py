from __future__ import annotations

"""앱 로그, 거래 로그, 시그널 로그를 관리하는 모듈."""

import csv
import logging
import logging.handlers
import os
import shutil
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
    # ── MACD 상태 관측 필드 (2026-08-04, GPT 코드리뷰 지시, 2차 개정) ──
    # 배경: trades.csv의 entry_reason 텍스트 파싱으로 "MACD 데드
    # 3건 전부 손실"을 확인했으나, 이건 실제 체결된 극소수 케이스
    # (10건)에만 있는 정보 — 매수로 안 이어진 수만 건의 HOLD/SKIP
    # 판단에는 MACD 상태가 전혀 기록되지 않아, "새 게이트를 넣으면
    # 몇 건이 추가로 막혔을지"를 과거 데이터로 계산할 방법이 없었음
    # (재현 확인: signal_log.csv 원본 컬럼에 macd/macd_signal 자체가
    # 없음). 아래 필드는 이후 관측을 쌓기 위한 것 — 신호 판단 로직
    # 자체는 전혀 바꾸지 않고 순수 관측치만 기록.
    #
    # 2026-08-04 (2차 GPT 코드리뷰 지적): 1차 구현의 macd_golden/
    # macd_dead 명칭이 실제 cross 이벤트(골든/데드 크로스가 방금
    # 일어났는지)가 아니라 "지금 macd > macd_signal인 상태"를
    # 뜻했음에도 크로스처럼 들려 오해 소지가 있었음 — macd_above_
    # signal로 이름 변경. 또한 would_be_blocked_if_macd_dead_
    # required 필드 하나가 "최소 5점 요구"(min-score-5, 기존
    # chasing_overheated 확장판)와 "점수 무관 완전 차단"(hard gate,
    # 원래 검증 대상)을 뭉뚱그리고 있어서 둘로 분리. 이 두 필드와
    # chasing_overheated는 legacy_buy_candidate(전략이 실제로 BUY를
    # 반환한 경우)일 때만 계산 — HOLD였던 판단을 "차단"이라고
    # 부르는 건 counterfactual의 정의 자체가 안 맞음.
    "macd",                 # MACD 원시값
    "macd_signal",          # MACD Signal 원시값
    "macd_above_signal",    # macd > macd_signal (True/False/빈값=지표없음).
                             # "골든크로스"가 아니라 "지금 이 상태"를 뜻함.
    "macd_hist_direction",  # 히스토그램 방향(+1 확대/-1 축소/0 보합)
    "legacy_buy_candidate", # 이번 폴링에서 전략이 실제로 BUY를 반환했는지
                             # (entry_watch/stale데이터 차단 등을 이미 거친
                             # 이 함수 호출 시점의 signal 기준)
    "latest_bar_timestamp", # 이 판단에 쓰인 분봉 데이터의 최신 timestamp
    "chasing_overheated_applies",  # chasing_overheated 게이트가 이 장세에
                                     # 실제로 존재하는지(BreakoutStrategy/
                                     # BULLISH 전용 — NEUTRAL 등에는 로직
                                     # 자체가 없어 False)
    "chasing_overheated_condition",  # 기존 게이트 발동 조건(당일등락≥3%+
                                       # MACD데드) 자체의 충족 여부.
                                       # 2026-08-04 (2차 GPT 코드리뷰 지적,
                                       # 재현 확인): 기존 게이트가 실제로
                                       # 차단한 사례는 이미 signal=HOLD로
                                       # 나오므로, 이 필드는 legacy_buy_
                                       # candidate와 무관하게(BUY든 HOLD든)
                                       # 계산 — applies=True일 때만 유효값.
    "would_block_existing_chasing_gate",  # chasing_overheated_condition이
                                            # 충족됐고 score<5이면 기존
                                            # 게이트가 실제로 차단했을지.
                                            # 이것도 BUY/HOLD와 무관하게
                                            # 계산 — "기존 게이트가 몇 건을
                                            # 막았는가"를 집계하기 위함.
    "would_block_macd_dead_min_score5",         # (min-score-5, 신규 가상
                                                  # 게이트) MACD 데드면 최소
                                                  # 5점 요구였다면 이번 BUY
                                                  # 후보가 막혔을지 —
                                                  # legacy_buy_candidate=True
                                                  # 일 때만 계산.
    "would_block_macd_above_signal_required",   # (hard gate, 신규 가상
                                                  # 게이트) MACD가 Signal
                                                  # 이하이면 점수와 무관하게
                                                  # 완전 차단이었다면 이번
                                                  # BUY 후보가 막혔을지 —
                                                  # legacy_buy_candidate=True
                                                  # 일 때만 계산. 원래
                                                  # 검증하려던 대상.
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

        # 2026-08-04 (GPT 코드리뷰 지시): 기존엔 self.file_path를
        # "w" 모드로 직접 열어 덮어쓰고 있었음 — 53MB급 실서버
        # signal_log.csv를 재작성하는 도중 프로세스가 죽거나 디스크
        # 문제가 생기면 원본 데이터가 통째로 유실될 위험이 있었음
        # (재작성이 절반만 끝난 상태로 파일이 잘리는 경우, 이전
        # 내용도 새 내용도 온전하지 않게 됨). 다음 두 가지로 방어:
        # (1) 재작성 전 원본을 .bak으로 복사(실패해도 원본은 그대로
        #     남아있어 최소한 데이터 유실은 없음).
        # (2) 임시 파일에 전부 쓴 뒤 os.replace()로 원자적 교체 —
        #     os.replace는 같은 파일시스템 안에서 단일 시스템 콜로
        #     완료되므로, 교체 도중에 프로세스가 죽어도 원본 파일이
        #     "절반만 쓰인 상태"로 남는 일이 없음(교체 전이면 원본
        #     그대로, 교체 후면 새 파일 그대로 — 중간 상태가 없음).
        backup_path = self.file_path.with_suffix(self.file_path.suffix + ".bak")
        try:
            shutil.copy2(self.file_path, backup_path)
            logger.info(f"[SIGNAL_LOG] 마이그레이션 전 백업 생성: {backup_path}")
        except OSError as exc:
            logger.warning(
                f"[SIGNAL_LOG] 백업 생성 실패 — 마이그레이션 중단(원본 보호 우선): {exc}"
            )
            return

        tmp_path = self.file_path.with_suffix(self.file_path.suffix + ".tmp")
        try:
            # 기존 데이터를 스트리밍으로 한 행씩 읽어 임시 파일에 바로
            # 씀 — old_rows를 리스트로 통째로 메모리에 올리지 않아
            # 53MB급 파일도 메모리 부담 없이 처리.
            row_count = 0
            with self.file_path.open("r", newline="", encoding="utf-8-sig") as src, \
                    tmp_path.open("w", newline="", encoding="utf-8") as dst:
                reader = csv.DictReader(src)
                writer = csv.DictWriter(dst, fieldnames=SIGNAL_FIELDS, extrasaction="ignore")
                writer.writeheader()
                for old_row in reader:
                    for field in SIGNAL_FIELDS:
                        old_row.setdefault(field, "")
                    writer.writerow(old_row)
                    row_count += 1
                # 2026-08-04 (GPT 코드리뷰 선택 개선): close() 전에
                # flush + fsync로 OS 캐시가 아니라 실제 디스크에
                # 기록됨을 보장 — os.replace() 자체는 이미 원자적
                # 이지만, 그 직전 tmp 파일의 내용이 디스크에 아직
                # 안 쓰인 상태에서 정전 등 강한 장애가 나면 replace
                # 후에도 빈 파일이나 일부만 쓰인 파일이 될 위험이
                # 있음(일반적인 프로세스 종료에는 이미 안전했지만,
                # 더 강한 내구성을 위해 추가).
                dst.flush()
                os.fsync(dst.fileno())

            os.replace(tmp_path, self.file_path)
            logger.info(
                f"[SIGNAL_LOG] 헤더 마이그레이션 완료 — {row_count:,}행 재작성, "
                f"컬럼 {len(existing_header)}개 → {len(SIGNAL_FIELDS)}개 "
                f"(백업: {backup_path})"
            )
        except Exception as exc:
            logger.error(
                f"[SIGNAL_LOG] 마이그레이션 중 예외 발생 — 원본 파일은 아직 "
                f"교체 전이라 온전함(임시 파일만 불완전할 수 있음): {exc}"
            )
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            raise

    def append(self, row: dict[str, Any]) -> None:
        """시그널 로그 한 줄을 추가합니다."""
        with self.file_path.open("a", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(fp, fieldnames=SIGNAL_FIELDS, extrasaction="ignore")
            for field in SIGNAL_FIELDS:
                row.setdefault(field, "")
            writer.writerow(row)


# ── entry_watch_shadow.csv ───────────────────────────────────────────────────
# entry_watch가 SELL을 낸 시점의 반사실적(counterfactual) 효과를 측정하기
# 위한 전용 로그. "entry_watch가 개입 안 했다면 어떻게 됐을지"를 각
# 체크포인트(5/10/20분)마다 실제로 계속 관찰해 별도로 기록한다.
# (2026-07-22: GPT 검토에서 지적된 "정적 관찰과 개입 효과는 다르다"는
#  문제에 대응 — 기존엔 SELL 시점 이후를 전혀 추적하지 않았음)

SHADOW_FIELDS = [
    "trigger_at",           # entry_watch가 SELL을 낸 시각
    "symbol",
    "trigger_type",         # 급락청산 / VWAP이탈청산 / 최소수익미달청산
    "entry_price",          # 매수 평균단가
    "trigger_price",        # entry_watch 청산 체결가(추정 — 청산 시점 current_price)
    "actual_pnl_pct",       # entry_watch 개입으로 실제 확정된 손익률(청산가 기준)
    "checkpoint_min",       # 이 행이 몇 분 시점 관찰인지 (5/10/20)
    "checkpoint_price",     # 그 시점 실제 가격 (entry_watch 없었다면의 시세)
    "counterfactual_pnl_pct",  # 그 시점까지 계속 보유했다면의 손익률
    "entry_watch_effect_pct",  # counterfactual_pnl_pct - actual_pnl_pct
                                # 양수 = entry_watch가 손실을 줄임(도움됨)
                                # 음수 = entry_watch가 좋은 거래를 잘라냄(손해)
]


class EntryWatchShadowLogger:
    """entry_watch 반사실적 비교 로그를 CSV로 남기는 로거입니다."""

    def __init__(self, file_path: str) -> None:
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.file_path.exists():
            with self.file_path.open("w", newline="", encoding="utf-8") as fp:
                writer = csv.DictWriter(fp, fieldnames=SHADOW_FIELDS)
                writer.writeheader()

    def append(self, row: dict[str, Any]) -> None:
        """반사실적 비교 로그 한 줄을 추가합니다."""
        with self.file_path.open("a", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(fp, fieldnames=SHADOW_FIELDS, extrasaction="ignore")
            for field in SHADOW_FIELDS:
                row.setdefault(field, "")
            writer.writerow(row)


# ── position_lifecycle.csv ───────────────────────────────────────────────────
# 포지션 5단계 상태머신(shadow, 2026-07-22)의 모든 상태 전이를 기록.
# 정상 전이(FLAT->BUY_PENDING->OPEN->...)와 이상 전이(부분체결/거부/
# 미반영/불변조건위반)를 전부 남겨서, shadow 검증 기간 동안 상태머신이
# 실제로 어떻게 동작하는지 사후 분석할 수 있게 한다.
# (기존엔 POSITION_STATE_MISMATCH 위반 시에만 app.log에 CRITICAL로
#  한 줄 남았고, 정상 전이는 메모리에만 있다가 다음 전이에 덮어써져
#  전혀 추적할 수 없었음)

LIFECYCLE_FIELDS = [
    "timestamp",           # 전이 발생 시각
    "symbol",
    "event",                # BUY_REQUESTED / BUY_RESULT / SELL_REQUESTED /
                             # SELL_RESULT / SYNC / INVARIANT_VIOLATION
    "from_lifecycle",       # 전이 전 상태
    "to_lifecycle",         # 전이 후 상태
    "broker_quantity",      # 이 이벤트 시점의 브로커 잔고 수량(조회한 경우)
    "pending_quantity",     # 진행 중이던 주문의 요청 수량
    "known_quantity",       # 상태머신이 마지막으로 확인한 수량
    "detail",                # PARTIAL_FILL / SELL_REJECTED / BUY_REJECTED 등
                             # last_error 값 또는 불변조건 위반 메시지
]


class PositionLifecycleLogger:
    """포지션 상태머신의 모든 전이를 CSV로 남기는 로거입니다."""

    def __init__(self, file_path: str) -> None:
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.file_path.exists():
            with self.file_path.open("w", newline="", encoding="utf-8") as fp:
                writer = csv.DictWriter(fp, fieldnames=LIFECYCLE_FIELDS)
                writer.writeheader()

    def append(self, row: dict[str, Any]) -> None:
        """상태 전이 로그 한 줄을 추가합니다."""
        with self.file_path.open("a", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(fp, fieldnames=LIFECYCLE_FIELDS, extrasaction="ignore")
            for field in LIFECYCLE_FIELDS:
                row.setdefault(field, "")
            writer.writerow(row)
