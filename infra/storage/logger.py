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
    # ── VWAP shadow 관측 요약 필드 (2026-08-05, 1E.5단계) ────────
    # 상세 8개 would_block_* 조합은 logs/entry_quality_shadow.csv
    # (legacy BUY 후보만 기록)에 두고, 여기 signal_log.csv에는
    # 기존 분석기(analyze_signal_log.py 등)가 HOLD/SKIP 포함 전체
    # 판단에서 상태를 훑어볼 수 있도록 원시값과 상태값만 추가.
    "is_pr",                        # minute_analysis.is_pulldown_recovery
    "is_c",                         # minute_analysis.is_valid_pulldown
    "is_pullback_condition",        # 조건식 중 "눌림목" 포함 여부
    "condition_names",              # 전체 조건식(|로 연결)
    "rolling_vwap",
    "rolling_vwap_distance_pct",
    "session_vwap",
    "session_vwap_distance_pct",
    "session_metrics_ready",
    "session_readiness_reason",
]


def _migrate_csv_header_if_needed(file_path: Path, target_fields: list[str], log_prefix: str) -> None:
    """기존 CSV의 헤더에 target_fields의 새 필드가 없으면 헤더를 갱신합니다.

    2026-08-05 (GPT 코드리뷰 지적, P0-1): 원래 SignalCsvLogger 안의
    메서드였던 로직을 범용 함수로 추출 — EntryQualityShadowLogger
    (1E.5→1E.6에서 6개 필드 추가)도 같은 마이그레이션이 필요한데,
    이전엔 이 로거가 파일 존재 여부만 확인하고 헤더 스키마는 전혀
    비교하지 않았음. 재현 확인: 1E.5 시절 구형 헤더(32열)에 1E.6
    로거로 행을 추가하면 실제 데이터는 38열이 되어, csv.DictReader
    로 다시 읽을 때 초과된 6개 값이 row[None]으로 밀려나고 
    final_decision 같은 정상 필드가 None으로 파싱됨. 이 로거는
    entry_quality_guard_mode="off"일 때도 빈 헤더 파일을 생성하므로,
    1E.5 코드를 한 번이라도 실행했다면 실서버에 이미 구형 헤더
    파일이 있을 수 있어 — shadow를 켜는 순간 첫날부터 CSV 스키마가
    깨질 위험이 있었음.

    extrasaction='ignore' 때문에, 헤더에 없는 컬럼은 조용히 버려집니다.
    따라서 필드를 추가해도 기존 파일은 그대로면 새 데이터가 영영
    안 들어갑니다. 이 함수가 그 격차를 메웁니다.

    주의: 기존 파일이 utf-8-sig(BOM 포함)로 쓰였을 수 있으므로
    반드시 utf-8-sig로 읽어야 첫 컬럼의 키가 BOM 때문에 깨지지
    않는다. (utf-8로 읽으면 '\ufeff타임스탬프'가 되어 값이 유실됨)
    """
    try:
        with file_path.open("r", newline="", encoding="utf-8-sig") as fp:
            reader = csv.reader(fp)
            existing_header = next(reader, [])
    except (StopIteration, OSError) as exc:
        logger.warning(f"[{log_prefix}] 헤더 확인 실패 — 마이그레이션 건너뜀: {exc}")
        return

    # 헤더가 이미 최신이면 아무것도 안 함
    missing = [f for f in target_fields if f not in existing_header]
    if not missing:
        logger.info(f"[{log_prefix}] 헤더 최신 상태 확인 ({len(existing_header)}개 컬럼) — 마이그레이션 불필요")
        return

    logger.info(
        f"[{log_prefix}] 헤더 마이그레이션 시작 — 누락 컬럼 {len(missing)}개: {missing}"
    )

    # 2026-08-04 (GPT 코드리뷰 지시): 원본 파일을 "w" 모드로 직접
    # 덮어쓰면, 대용량 파일을 재작성하는 도중 프로세스가 죽거나
    # 디스크 문제가 생겼을 때 원본 데이터가 통째로 유실될 위험이
    # 있음(재작성이 절반만 끝난 상태로 파일이 잘리는 경우, 이전
    # 내용도 새 내용도 온전하지 않게 됨). 다음 두 가지로 방어:
    # (1) 재작성 전 원본을 .bak으로 복사(실패해도 원본은 그대로
    #     남아있어 최소한 데이터 유실은 없음).
    # (2) 임시 파일에 전부 쓴 뒤 os.replace()로 원자적 교체 —
    #     os.replace는 같은 파일시스템 안에서 단일 시스템 콜로
    #     완료되므로, 교체 도중에 프로세스가 죽어도 원본 파일이
    #     "절반만 쓰인 상태"로 남는 일이 없음(교체 전이면 원본
    #     그대로, 교체 후면 새 파일 그대로 — 중간 상태가 없음).
    backup_path = file_path.with_suffix(file_path.suffix + ".bak")
    try:
        shutil.copy2(file_path, backup_path)
        logger.info(f"[{log_prefix}] 마이그레이션 전 백업 생성: {backup_path}")
    except OSError as exc:
        logger.warning(
            f"[{log_prefix}] 백업 생성 실패 — 마이그레이션 중단(원본 보호 우선): {exc}"
        )
        return

    tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
    try:
        # 기존 데이터를 스트리밍으로 한 행씩 읽어 임시 파일에 바로
        # 씀 — 통째로 메모리에 올리지 않아 대용량 파일도 메모리
        # 부담 없이 처리.
        row_count = 0
        with file_path.open("r", newline="", encoding="utf-8-sig") as src, \
                tmp_path.open("w", newline="", encoding="utf-8") as dst:
            reader = csv.DictReader(src)
            writer = csv.DictWriter(dst, fieldnames=target_fields, extrasaction="ignore")
            writer.writeheader()
            for old_row in reader:
                for field in target_fields:
                    old_row.setdefault(field, "")
                writer.writerow(old_row)
                row_count += 1
            # flush + fsync로 OS 캐시가 아니라 실제 디스크에 기록됨을
            # 보장 — os.replace() 자체는 이미 원자적이지만, 그 직전
            # tmp 파일의 내용이 디스크에 아직 안 쓰인 상태에서 정전
            # 등 강한 장애가 나면 replace 후에도 빈 파일이나 일부만
            # 쓰인 파일이 될 위험이 있음.
            dst.flush()
            os.fsync(dst.fileno())

        os.replace(tmp_path, file_path)
        logger.info(
            f"[{log_prefix}] 헤더 마이그레이션 완료 — {row_count:,}행 재작성, "
            f"컬럼 {len(existing_header)}개 → {len(target_fields)}개 "
            f"(백업: {backup_path})"
        )
    except Exception as exc:
        logger.error(
            f"[{log_prefix}] 마이그레이션 중 예외 발생 — 원본 파일은 아직 "
            f"교체 전이라 온전함(임시 파일만 불완전할 수 있음): {exc}"
        )
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


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
            _migrate_csv_header_if_needed(self.file_path, SIGNAL_FIELDS, "SIGNAL_LOG")

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


# ── entry_quality_shadow.csv ─────────────────────────────────────────────────
# MACD/VWAP 진입 품질 shadow 관측 전용 로그 (2026-08-05, 1E.5단계, GPT
# 코드리뷰 지시). signal_log.csv(이미 53MB급, 모든 판단을 기록)와 달리
# legacy_buy_candidate=True(전략이 실제로 BUY를 반환한 경우)일 때만
# 기록 — 같은 분봉·같은 판단이 반복되면 한 번만 남긴다.
#
# 2026-08-05 (2차 GPT 코드리뷰 지적 2번, 재현 확인): 중복 방지 키가
# (symbol, latest_bar_timestamp, detected_patterns, score)만이라
# 같은 분봉 안에서 현재가가 움직여 게이트 상태(would_block_*)가
# 바뀌어도(예: 1.99%->2.01%로 임계값을 넘어감) 두 번째 관측이
# 조용히 버려지고 있었음(재현: 첫 관측 block=False로 append 성공,
# 두 번째 관측 block=True인데 같은 key라 append_if_new가 False를
# 반환해 기록 안 됨 -> 최종 CSV에는 첫 상태만 남음). 게이트 상태
# 자체(assessment_signature)를 키에 포함시켜, 같은 분봉이라도
# 판단이 바뀌면 새 행으로 남도록 수정.

ENTRY_QUALITY_SHADOW_FIELDS = [
    "timestamp",              # 기록 시각(KST)
    "symbol",
    "latest_bar_timestamp",   # 이 판단에 쓰인 분봉의 최신 timestamp(중복 방지 키)
    "detected_patterns",      # V/PR/A/B/C/D 패턴 조합(중복 방지 키)
    "score",                  # 8점 체계 점수(중복 방지 키)
    "regime",
    "condition_name",         # 대표 조건식(호환용)
    "condition_names",        # 전체 조건식(| 로 연결)
    "condition_source_reliable",  # 조건식 출처가 CNSRREQ로 확정됐는지
    # 2026-08-05 (2차 GPT 코드리뷰 지적 3번): 실제 진입 기준값 —
    # 이게 없으면 legacy_buy_candidate=True라도 실제로 주문됐는지,
    # DAILY_ENTRY_LIMIT/AFTER_1450/RISK_LIMIT 등 기존 규칙으로
    # 이미 차단된 후보인지 구분할 수 없고, 정확한 5·10·20분 후
    # 수익률 계산도 어려움.
    "current_price",
    "legacy_reason",
    "final_decision",
    "order_block_reason",
    # 2026-08-05 (3차 GPT 코드리뷰 지적 P1, 재현 확인): 기존
    # actual_order_submitted = final_decision=="BUY" 하나로는
    # "주문을 시도했다"와 "브로커가 실제로 접수했다"를 구분할 수
    # 없었음 — _try_buy()가 broker.place_order()를 호출한 뒤
    # result.accepted=False(브로커 거부)여도 명시적 block 사유를
    # 반환하지 않아 final_decision은 여전히 "BUY"로 남는 것을 재현
    # 확인. 다음 4개로 분리 — order_attempted는 "기존 리스크/제한
    # 규칙을 통과해 실제로 broker.place_order()를 호출했는지",
    # order_accepted는 "브로커가 그 주문을 실제로 접수했는지"로
    # 서로 다른 질문. order_attempted=False면 order_accepted/
    # order_id/order_message는 애초에 호출 자체가 없었으므로 빈 값.
    "order_attempted",
    "order_accepted",
    "order_id",
    "order_message",
    # MACD (1E 단계와 동일 계산 재사용)
    "macd",
    "macd_signal",
    "macd_above_signal",
    "would_block_macd_dead_min_score5",
    "would_block_macd_above_signal_required",
    # VWAP 상태
    "is_pr",
    "is_c",
    "is_pullback_condition",
    "is_pr_or_pullback_condition",
    "rolling_vwap",
    "rolling_vwap_distance_pct",
    "session_vwap",
    "session_vwap_distance_pct",
    "session_metrics_ready",
    "session_readiness_reason",
    "session_gate_eligible",
    # VWAP 가상 게이트 — rolling 기준
    "would_block_pr_only_rolling_vwap",
    "would_block_c_or_pr_rolling_vwap",
    "would_block_pullback_condition_rolling_vwap",
    "would_block_pr_or_pullback_condition_rolling_vwap",
    # VWAP 가상 게이트 — session 기준
    "would_block_pr_only_session_vwap",
    "would_block_c_or_pr_session_vwap",
    "would_block_pullback_condition_session_vwap",
    "would_block_pr_or_pullback_condition_session_vwap",
]

# 2026-08-06 (1I.1, GPT 코드리뷰 지적 4번): signature 로직을
# domain/shadow_signature.py로 추출해 분석기(analyze_shadow.py)와
# 공유합니다. 복제 구현은 향후 필드 추가 시 다시 어긋날 위험이
# 있어, 로거와 분석기가 같은 함수를 쓰도록 했습니다.
from domain.shadow_signature import (  # noqa: E402
    ASSESSMENT_SIGNATURE_FIELDS as _ASSESSMENT_SIGNATURE_FIELDS,
    entry_quality_shadow_key as _entry_quality_shadow_key,
)


class EntryQualityShadowLogger:
    """MACD/VWAP 진입 품질 shadow 관측을 legacy BUY 후보에만 기록하는 로거입니다.

    2026-08-05 (1E.5단계): 같은 (symbol, latest_bar_timestamp,
    detected_patterns, score, assessment_signature) 조합은 한 번만
    기록 — 같은 분봉에서 반복되는 폴링(10초 간격)이 매번 중복 행을
    만들지 않도록 함. assessment_signature(게이트 상태·최종 결정)
    가 바뀌면 같은 분봉이라도 새 행으로 기록됨(2차 GPT 코드리뷰
    지적 2번).

    2026-08-05 (2차 GPT 코드리뷰 지적 4번): 이 로거는 파일 생성
    시점에 기존 CSV가 있으면 그 안의 키를 전부 읽어 _seen_keys를
    복원함 — 장중 프로세스 재시작 시 같은 분봉·같은 판단이 다시
    기록되는 것을 방지. 전용 파일은 legacy BUY 후보만 담아 크기가
    작아(signal_log.csv와 달리) 매번 전체를 읽어도 부담이 크지 않음.

    2026-08-05 (2차 GPT 코드리뷰 지적 6번, 문서 정정): 이 생성자는
    entry_quality_guard_mode 설정과 무관하게 항상 헤더 파일을
    즉시 생성함(TradingService.__init__에서 다른 shadow 로거들과
    동일 패턴으로 항상 인스턴스화되므로) — "off"에서는 이 파일에
    행이 추가되지 않을 뿐, 빈 헤더만 있는 파일 자체는 생성됨.

    2026-08-05 (3차 GPT 코드리뷰 지적 P0-1, 재현 확인): 이전엔
    파일이 존재하면 헤더 스키마를 전혀 확인하지 않고 곧바로 키만
    복원했음 — 1E.5→1E.6에서 6개 필드(condition_source_reliable,
    current_price 등)가 추가됐는데, 이 로거는 entry_quality_
    guard_mode="off"일 때도 빈 헤더 파일을 생성하므로 1E.5 코드를
    한 번이라도 실행한 실서버에는 구형 32열 헤더 파일이 있을 수
    있었음. 그 상태에서 신형(38열) 로거로 행을 추가하면 csv.
    DictReader가 다시 읽을 때 초과 6개 값이 row[None]으로 밀려나고
    final_decision 등 정상 필드가 None으로 파싱되는 것을 재현
    확인. 이제 키 복원 전에 반드시 _migrate_csv_header_if_needed()
    로 헤더를 먼저 최신화.
    """

    def __init__(self, file_path: str) -> None:
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._seen_keys: set[tuple] = set()

        if self.file_path.exists():
            # 2026-08-05: 헤더 마이그레이션을 먼저 수행 — 구형
            # 헤더인 상태로 키를 복원하면 이후 append_if_new()가
            # 쓰는 신형 행과 열 구조가 어긋나는 문제를 방지.
            _migrate_csv_header_if_needed(self.file_path, ENTRY_QUALITY_SHADOW_FIELDS, "ENTRY_QUALITY_SHADOW")
            # 2026-08-05: 기존 파일이 있으면(재시작) 키를 복원 —
            # 파일이 크지 않으므로(legacy BUY 후보만 기록됨) 전체를
            # 읽어도 부담 없음. 헤더가 예상과 다르거나(과거 버전
            # 필드 구조) 파싱 오류가 나는 개별 행은 건너뛰고 계속
            # 진행 — 복원 실패가 로거 생성 자체를 막으면 안 됨.
            try:
                with self.file_path.open("r", newline="", encoding="utf-8") as fp:
                    reader = csv.DictReader(fp)
                    for row in reader:
                        try:
                            self._seen_keys.add(_entry_quality_shadow_key(row))
                        except Exception:
                            continue
            except Exception as exc:
                logger.warning(
                    f"[ENTRY_QUALITY_SHADOW] 기존 파일에서 중복방지 키 복원 실패"
                    f"(무시하고 빈 상태로 계속 진행): {exc}"
                )
        else:
            with self.file_path.open("w", newline="", encoding="utf-8") as fp:
                writer = csv.DictWriter(fp, fieldnames=ENTRY_QUALITY_SHADOW_FIELDS)
                writer.writeheader()

    def append_if_new(self, row: dict[str, Any]) -> bool:
        """중복 키가 아니면 한 줄을 추가하고 True, 이미 기록된 키면 아무것도 안 하고 False를 반환합니다.

        2026-08-05 (3차 GPT 코드리뷰 지적 P2, 재현 확인): 이전엔
        _seen_keys.add(key)를 파일 쓰기 *전에* 실행했음 — 파일
        쓰기 도중 예외가 발생하면 그 행은 실제로 저장되지 않았는데
        키는 이미 소비된 상태가 되어, 같은 판단을 다음에 다시
        시도해도 "이미 기록됨"으로 오판해 영구히 누락되는 문제가
        있었음. writer.writerow()가 실제로 성공한 뒤에만 키를
        추가하도록 순서를 바꿈 — 쓰기 실패 시 다음 재시도가
        정상적으로 가능해짐.
        """
        key = _entry_quality_shadow_key(row)
        if key in self._seen_keys:
            return False

        with self.file_path.open("a", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(fp, fieldnames=ENTRY_QUALITY_SHADOW_FIELDS, extrasaction="ignore")
            for field in ENTRY_QUALITY_SHADOW_FIELDS:
                row.setdefault(field, "")
            writer.writerow(row)

        self._seen_keys.add(key)
        return True


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


# ── low_upside_shadow.csv ────────────────────────────────────────────────────
# Profitability Shadow v2 (2026-08-24). Profitability Sprint v1(analysis-only
# 도구, tools/profitability_sprint.py)이 사후 분석(9건, 8/20+8/21)에서 찾은
# "Low Upside skip" 후보를 실시간으로 표본을 쌓기 위한 shadow 관측 전용 로그.
#
# 중요: 이 로거는 BUY를 절대 차단하지 않습니다. legacy_buy_candidate=True
# (전략이 실제로 BUY를 반환한 경우)일 때 "만약 upside_to_recent_high_pct가
# 이 임계값 미만이면 걸렀을 것이다"라는 가상 판정만 기록합니다 — 실제 주문
# 흐름(_try_buy)에는 어떤 값도 참조·전달되지 않습니다.
#
# entry_quality_shadow.csv와 달리 entry_quality_guard_mode 설정과 완전히
# 무관하게 항상 기록됩니다(민우님 지시: "목적은 수익성 후보의 실시간 shadow
# 표본을 빠르게 쌓는 것" — 기존 VWAP shadow 실험 on/off와 별개 축이므로).
#
# 임계값 세 개(F1<1.00%/F2<0.50%/F3<0.25%)를 함께 기록하는 이유는
# Profitability Sprint v1 리뷰(민우님)에서 F1과 F2가 실거래 9건에서
# 우연히 동일한 6건을 제거했을 뿐(표본에 0.50~1.00% 구간 거래가 없었음)
# 이라는 게 확인되어, 앞으로 새 표본이 들어와도 세 후보를 동시에 비교할
# 수 있어야 하기 때문입니다. 주 후보는 F2(<0.50%)입니다.

LOW_UPSIDE_SHADOW_FIELDS = [
    "timestamp",                    # 기록 시각(KST)
    "symbol",
    "latest_bar_timestamp",         # 이 판단에 쓰인 분봉의 최신 timestamp(중복 방지 키)
    "detected_patterns",            # V/PR/A/B/C/D 패턴 조합(중복 방지 키)
    "score",                        # 8점 체계 점수(중복 방지 키)
    "condition_name",               # 대표 조건식(호환용)
    "upside_to_recent_high_pct",    # 현재가→최근 고점 상승 여력(%) — minute_analysis 원값
    "would_skip_low_upside_f1",     # upside < 1.00% (보조 비교용)
    "would_skip_low_upside_f2",     # upside < 0.50% (주 후보, 민우님 확정)
    "would_skip_low_upside_f3",     # upside < 0.25% (보조 비교용)
    "final_decision",               # 실제 결과: BUY / HOLD / BLOCKED
    "order_block_reason",
    # 2026-08-24 (Shadow v2 closure, 민우님 코드리뷰 지적): entry_quality_
    # shadow.csv에는 이미 있던 실제 주문 연결 정보가 이 CSV에는 빠져
    # 있었음 — "F2가 True였던 BUY 후보"와 "F2가 True였고 실제 accepted
    # 되어 돈이 들어간 거래"를 구분하지 못하면 다음 Sprint에서 timestamp
    # 기반 억지 조인이 필요해짐. _write_signal_log()가 이미 계산해 둔
    # order_attempt(OrderResult)를 그대로 반영 — BUY를 막는 데는 절대
    # 쓰이지 않고 순수 기록용.
    "order_attempted",
    "order_accepted",
    "order_id",
    # 2026-08-27 (Candidate A forward shadow, Profitability Sprint v1.2
    # historical 결과 승인 후 민우님 지시): Sprint v1.2가 8/20~8/25
    # historical 데이터로 F2(upside<0.50%) 대비 가장 강한 후보로 확인한
    # "Candidate A"(upside<0.50% AND rebound_volume_spike==False)를
    # 이 시점부터 조건 고정(더 이상 score/PR/VWAP/시간대 등 추가 조건
    # 탐색 안 함)하고, historical이 아닌 forward 표본을 실시간으로
    # 쌓기 위한 필드입니다. rebound_volume_spike는 이미 signal_log.csv
    # 에 있던 것과 동일한 minute_analysis 원시값(BUY를 절대 막지
    # 않음)이고, would_skip_low_upside_no_spike가 Candidate A 판정
    # 그 자체입니다. rebound_volume_spike가 결측(None)이면 False로
    # 추정하지 않고 두 필드 모두 빈 문자열로 남깁니다 — Sprint v1.2
    # 분석 도구의 "결측 feature는 skip 대상으로 추정하지 않는다"
    # 원칙과 production shadow가 동일해야 나중에 offline historical
    # 결과와 forward shadow 결과를 1:1로 비교할 수 있기 때문입니다
    # (domain/service/trading_service.py의 기록 지점 참고).
    "rebound_volume_spike",             # minute_analysis 원시값(True/False/빈값=결측)
    "would_skip_low_upside_no_spike",   # Candidate A 판정: upside<0.50% AND
                                          # rebound_volume_spike==False. spike가
                                          # 결측이면 빈 문자열(False로 추정 금지).
]


# 2026-08-24 (Shadow v2 재closure, 민우님 코드리뷰 P1 지적): 최초
# closure는 (symbol, latest_bar_timestamp, detected_patterns, score)만
# 키로 썼는데, "would_skip_*는 upside_to_recent_high_pct에만 의존하므로
# 충분하다"는 근거는 F1/F2/F3 판정 자체에는 맞지만, 같은 closure에서
# order_attempted/order_accepted/order_id를 CSV에 새로 추가하면서 더는
# 충분하지 않게 됐습니다. 같은 분봉 안에서 첫 폴링은 주문이 거부됐다가
# (order_accepted=False) 다음 폴링에서 재시도가 accepted(order_accepted=True,
# order_id=실제값)되는 경우, 기존 4필드 키로는 두 번째(accepted) 행이
# "중복"으로 버려져 실제로는 accepted된 거래인데 CSV에는 rejected 행만
# 남는 문제가 있었습니다 — "F2가 True였고 실제 accepted된 거래"를
# 추적한다는 이 필드 추가의 목적을 정확히 깨뜨리는 결함이었습니다.
# 이제 주문 상태 신호(final_decision/order_block_reason/order_attempted/
# order_accepted/order_id)를 키에 포함해, 같은 분봉이라도 주문 상태가
# 바뀌면(rejected→accepted, BLOCKED→accepted 등) 새 행으로 기록되고,
# 완전히 동일한 상태의 반복 폴링(10초 간격)은 계속 1행으로 유지됩니다.
#
# 2026-08-27 (Candidate A forward shadow): 위 rebound_volume_spike/
# would_skip_low_upside_no_spike 두 필드는 의도적으로 이 키에
# 포함하지 않았습니다 — 둘 다 이미 키에 있는 (symbol,
# latest_bar_timestamp)와 같은 minute_analysis 스냅샷에서 나온
# 결정론적 값이라, 같은 base+signature 키를 공유하는 행이라면
# 이 두 값도 항상 동일합니다. 순수 컬럼 추가이므로 dedup 계약
# (rejected→accepted 상태변화 보존, 동일 accepted 반복 폴링은 1행
# 유지)에는 영향이 없습니다.
LOW_UPSIDE_SHADOW_SIGNATURE_FIELDS: list[str] = [
    "final_decision",
    "order_block_reason",
    "order_attempted",
    "order_accepted",
    "order_id",
]


class LowUpsideShadowLogger:
    """Low Upside skip 후보(F1/F2/F3)의 실시간 shadow 관측을 기록하는 로거입니다.

    중복 방지 키 = base(symbol, latest_bar_timestamp, detected_patterns,
    score) + signature(LOW_UPSIDE_SHADOW_SIGNATURE_FIELDS 5개). base
    4개만으로는 같은 분봉 안에서 주문 상태가 바뀌는 경우(예: 거부 후
    재시도로 accepted)를 별도 행으로 남기지 못해, EntryQualityShadowLogger
    (domain/shadow_signature.py의 assessment_signature)와 같은 이유로
    signature를 추가했습니다. 값은 전부 str()로 정규화합니다 — 새로
    들어오는 row는 Python bool(True/False)이지만 재시작 시 CSV에서
    복원한 row는 문자열("True"/"False")이라 타입이 다르면 같은 논리적
    값인데 키가 어긋나는 문제를 방지하기 위함입니다.
    """

    def __init__(self, file_path: str) -> None:
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._seen_keys: set[tuple] = set()

        if self.file_path.exists():
            _migrate_csv_header_if_needed(self.file_path, LOW_UPSIDE_SHADOW_FIELDS, "LOW_UPSIDE_SHADOW")
            try:
                with self.file_path.open("r", newline="", encoding="utf-8") as fp:
                    reader = csv.DictReader(fp)
                    for row in reader:
                        try:
                            self._seen_keys.add(self._key(row))
                        except Exception:
                            continue
            except Exception as exc:
                logger.warning(
                    f"[LOW_UPSIDE_SHADOW] 기존 파일에서 중복방지 키 복원 실패"
                    f"(무시하고 빈 상태로 계속 진행): {exc}"
                )
        else:
            with self.file_path.open("w", newline="", encoding="utf-8") as fp:
                writer = csv.DictWriter(fp, fieldnames=LOW_UPSIDE_SHADOW_FIELDS)
                writer.writeheader()

    @staticmethod
    def _key(row: dict[str, Any]) -> tuple:
        base = (
            str(row.get("symbol", "")),
            str(row.get("latest_bar_timestamp", "")),
            str(row.get("detected_patterns", "")),
            str(row.get("score", "")),
        )
        signature = tuple(str(row.get(f, "")) for f in LOW_UPSIDE_SHADOW_SIGNATURE_FIELDS)
        return base + signature

    def append_if_new(self, row: dict[str, Any]) -> bool:
        """중복 키가 아니면 한 줄을 추가하고 True, 이미 기록된 키면 False."""
        key = self._key(row)
        if key in self._seen_keys:
            return False

        with self.file_path.open("a", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(fp, fieldnames=LOW_UPSIDE_SHADOW_FIELDS, extrasaction="ignore")
            for field in LOW_UPSIDE_SHADOW_FIELDS:
                row.setdefault(field, "")
            writer.writerow(row)

        self._seen_keys.add(key)
        return True


# ── min_profit_extension_shadow.csv ──────────────────────────────────────────
# Profitability Shadow v2 (2026-08-24). Profitability Sprint v1 리뷰(민우님)
# 지적: 기존 entry_watch_shadow.csv는 "entry_watch " 접두사의 모든 청산
# 유형(급락청산/VWAP이탈청산/최소수익미달청산)을 한 버킷으로 섞어 추적했고,
# 매수 시점(entry) feature만 있어 "5분 청산 판단 순간"의 실제 지표값이
# 없었습니다 — 그래서 조건부 연장 규칙(R1/R2) 검증이 사실상 "5분 시점
# PnL>0 AND 진입 당시 VWAP/MACD 상태"가 되어버려 원래 질문(5분 시점 상태로
# 판단)에 답하지 못했습니다.
#
# 이 로그는 정확히 그 간극을 메웁니다 — `_check_entry_watch()`가
# "entry_watch 최소수익미달청산"을 판단하는 그 순간(watch_minutes 경과 +
# pnl_pct < min_profit_pct)에만, 오직 그 판단 순간의 값을 기록합니다.
# VWAP 이탈청산(더 이른 시점, 다른 질문)이나 급락청산은 이 로그에 남지
# 않습니다 — 그 두 유형은 여전히 entry_watch_shadow.csv(기존, 무변경)가
# 다룹니다.
#
# 이 로거는 SELL을 절대 막지 않습니다 — `_check_entry_watch()`가 이미
# 반환하기로 결정한 Signal을 그대로 반환하기 직전에, 그 순간의 관측치만
# 추가로 남깁니다. 최소수익미달청산 판정 로직 자체는 단 한 줄도 바뀌지
# 않습니다.
#
# 2026-08-24 (Shadow v2 closure, 민우님 코드리뷰 지적): 최초 배치본은
# "청산 이벤트당 정확히 한 번만 호출된다"고 가정해 중복 방지 키가
# 없었으나, 이 전제가 틀렸습니다 — SELL accepted 이후에도 잔고 API가
# 2~3분 늦게 반영되는 동안(OBS.2-A가 이미 실측으로 확인한 패턴,
# 064260/8·21 등) position이 여전히 non-None으로 보여 `_check_entry_
# watch()`가 같은 포지션에 대해 최소수익미달 판단을 매 폴링마다 반복
# 반환할 수 있습니다. PSM은 실제 중복 SELL 주문은 막지만, 이 로깅은
# PSM의 SELL block보다 앞선 `_check_entry_watch()` 안에서 이뤄지므로
# 그 사이 여러 행이 쌓일 수 있습니다. 이제 (symbol, entry_time) 기준
# 중복 방지 키를 둬 같은 position episode당 최초 판단 1건만 기록합니다
# (entry_time은 `state.entry_time_by_symbol`의 실제 진입 시각 — 재진입
# 시 값이 달라지므로 자동으로 새 episode로 구분됩니다). 재시작 시에도
# 기존 CSV에서 키를 복원해 중복 기록을 방지합니다.
#
# 설계 선택 — accepted SELL과의 연결(민우님 코드리뷰 3번 항목): 이 로그는
# `_check_entry_watch()`가 최소수익미달청산 SELL Signal을 만들기로 "판단"
# 하는 순간에 기록되며, 그 SELL이 실제로 broker에 accepted됐는지는 별도로
# 확인하지 않습니다(즉 broker reject가 나도 이 행은 이미 남습니다). 리뷰가
# 제안한 두 방식 — (a) `_check_entry_watch()`에서 snapshot만 만들어 보관
# 하고 `_try_sell_unchecked()`의 accepted 분기에서 실제 order_id와 함께
# 기록, (b) entry_time 기반 최초판단 dedup까지만 구현하고 후속 분석기가
# accepted SELL과 timestamp로 검증 — 중 이번 closure에서는 **(b)를
# 선택**했습니다. 이유: (a)는 SELL 실행 경로(_try_sell_unchecked) 자체에
# snapshot 전달/보관 상태를 추가해야 해서 "관측 전용, 판정 로직 최소
# 침습"이라는 이 라운드의 설계 원칙에 비해 과도하게 침습적이라고 판단했기
# 때문입니다. (symbol, entry_time)이 position episode를 유일하게 식별하고
# trades.csv/entry_watch_shadow.csv에도 동일 종목·근접 시각의 SELL 행이
# 남으므로, 다음 Profitability Sprint가 timestamp 근접 매칭으로 실제
# accepted 여부를 사후 검증할 수 있습니다 — Sprint v1이 이미 이런 근접
# 조인을 signal_log/entry_quality_shadow에 대해 하고 있어 동일한 패턴을
# 재사용할 수 있습니다.

MIN_PROFIT_EXTENSION_SHADOW_FIELDS = [
    "timestamp",                     # 판단(=SELL 신호 발생) 시각
    "symbol",
    "entry_time",                    # 이 position episode의 실제 진입 시각
                                       # (state.entry_time_by_symbol) — (symbol,
                                       # entry_time) 조합이 중복 방지 키
    "holding_minutes",               # 매수 후 경과 시간(분) = elapsed_min
    "pnl_pct",                       # 판단 순간 수익률 (avg 대비)
    "price",                         # 판단 순간 현재가
    "vwap",                          # 판단 순간 VWAP (minute_analysis, stale이면 공백)
    "price_vs_vwap_pct",             # (price-vwap)/vwap*100 (stale이면 공백)
    "macd",                          # 판단 순간 MACD 원시값 (market_price 기준)
    "macd_signal",                   # 판단 순간 MACD Signal 원시값
    "macd_above_signal",             # macd > macd_signal (True/False/공백=지표없음)
    "rsi",                           # 판단 순간 RSI
    "ma5",                           # 판단 순간 분봉 MA5 (stale이면 공백)
    "ma20",                          # 판단 순간 분봉 MA20 (stale이면 공백)
    "peak_pnl_pct",                  # 매수 후 최고가 기준 최대 수익률
    "drawdown_from_peak_pct",        # 최고가 대비 현재 낙폭(%, 0 이하)
    "upside_to_recent_high_pct",     # 판단 순간 상승 여력(%) (stale이면 공백)
    "minute_data_stale",             # True면 vwap/price_vs_vwap_pct/ma5/ma20/
                                       # upside_to_recent_high_pct가 전부 공백
                                       # (그 폴링 시점 분봉 데이터가 fresh하지
                                       # 않아 minute_analysis 자체가 None으로
                                       # 넘어온 경우 — 425p 급락·시간초과청산과
                                       # 동일하게 stale이어도 이 청산 자체는
                                       # 허용되지만, 지표 관측은 불가능)
]


class MinProfitExtensionShadowLogger:
    """entry_watch 최소수익미달청산(5분 시점) 판단 순간의 feature snapshot을
    기록하는 로거입니다.

    entry_watch_shadow.csv(기존)와 달리 청산 유형을 섞지 않고, 오직
    "최소수익미달청산" 한 종류만 기록합니다.

    2026-08-24 (Shadow v2 closure): 최초 배치본의 "청산 이벤트당 정확히
    한 번만 호출된다"는 전제가 틀렸음이 코드리뷰로 확인됐습니다 — SELL
    accepted 뒤 잔고 API 반영이 늦어지는 동안 같은 position에 대해
    같은 판단이 반복될 수 있습니다(위 모듈 주석 참고). (symbol,
    entry_time) 기준으로 중복을 방지합니다 — EntryQualityShadowLogger/
    LowUpsideShadowLogger와 같은 패턴으로, 기존 파일이 있으면 키를
    복원합니다.
    """

    def __init__(self, file_path: str) -> None:
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._seen_keys: set[tuple] = set()

        if self.file_path.exists():
            _migrate_csv_header_if_needed(
                self.file_path, MIN_PROFIT_EXTENSION_SHADOW_FIELDS, "MIN_PROFIT_EXTENSION_SHADOW"
            )
            try:
                with self.file_path.open("r", newline="", encoding="utf-8") as fp:
                    reader = csv.DictReader(fp)
                    for row in reader:
                        try:
                            self._seen_keys.add(self._key(row))
                        except Exception:
                            continue
            except Exception as exc:
                logger.warning(
                    f"[MIN_PROFIT_EXTENSION_SHADOW] 기존 파일에서 중복방지 키 복원 실패"
                    f"(무시하고 빈 상태로 계속 진행): {exc}"
                )
        else:
            with self.file_path.open("w", newline="", encoding="utf-8") as fp:
                writer = csv.DictWriter(fp, fieldnames=MIN_PROFIT_EXTENSION_SHADOW_FIELDS)
                writer.writeheader()

    @staticmethod
    def _key(row: dict[str, Any]) -> tuple:
        return (str(row.get("symbol", "")), str(row.get("entry_time", "")))

    def append_if_new(self, row: dict[str, Any]) -> bool:
        """같은 (symbol, entry_time) episode에 대해 최초 1건만 기록하고
        True를 반환합니다. 이미 기록된 episode면 아무것도 안 하고
        False를 반환합니다."""
        key = self._key(row)
        if key in self._seen_keys:
            return False

        with self.file_path.open("a", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(fp, fieldnames=MIN_PROFIT_EXTENSION_SHADOW_FIELDS, extrasaction="ignore")
            for field in MIN_PROFIT_EXTENSION_SHADOW_FIELDS:
                row.setdefault(field, "")
            writer.writerow(row)

        self._seen_keys.add(key)
        return True


# ── balance_freshness.csv ────────────────────────────────────────────────────
# S01 관측 1단계 (2026-09-11, GPT 5차 검토 반영). `TradingService.
# _get_balance_with_cache()`가 매 호출마다 실제로 어느 분기를 탔는지
# (신규 조회 성공 / 429로 캐시 폴백 / 조회 자체 실패 후 예외 재전파 /
# 아직 갱신 주기 전이라 캐시 재사용)와, 그 시점의 캐시 나이·미해결
# 주문 존재 여부를 있는 그대로 기록합니다.
#
# 이번 단계는 순수 관측입니다 — 이 로거의 존재나 실패 여부가
# _get_balance_with_cache()의 반환값, 조회 주기(balance_refresh_seconds),
# 429 폴백 여부, `_has_unresolved_orders()`의 판정 중 단 하나도 바꾸지
# 않습니다. append 실패는 항상 fail-open이며 app_logger에 WARNING만
# 남깁니다(호출부 docstring 참고).
#
# 이 로그의 목적은 "429가 실제로 얼마나 자주, 어떤 트리거 사유로,
# 그 시점 캐시가 얼마나 오래됐을 때 발생하는지"를 실측하는 것입니다
# — S01의 캐시 유효기간 연장(360/540초)이나 폴백 재도입 여부는 이
# 실측 없이 결정하지 않기로 했습니다(2026-09-11 5차 설계 문서, N값
# 미승인 항목 참고).
#
# 2026-09-14 (안전 복구, GPT 재검토 20260914): 429 시 캐시로 대체하던
# 동작(fallback_stale_cache)을 되돌렸습니다 — 미해결 주문 때문에
# 최신 잔고가 필요한 분기에서 스테일 캐시가 confirm_buy_from_broker()
# 등에 "체결 확인 결과"처럼 흘러드는 경로가 재현됐기 때문입니다.
# 이제 429는 항상 fetch_failed_429_fallback_disabled로 기록되고
# 예외가 그대로 전파됩니다 — fallback_stale_cache 값은 더 이상
# 새로 기록되지 않지만(과거 로그에는 남아있을 수 있음), 분석 코드는
# 하위호환을 위해 이 값도 계속 인식해야 합니다.
BALANCE_FRESHNESS_FIELDS = [
    "timestamp",                # 이 _get_balance_with_cache() 호출 시각
    "outcome",                  # fetch_success | cache_reuse |
                                 # fetch_failed_429_fallback_disabled |
                                 # fetch_failed_no_fallback |
                                 # (과거 로그 전용) fallback_stale_cache
    "trigger_reason",           # no_cache_yet | unresolved_orders |
                                 # routine_refresh_due | cache_still_fresh
    "prior_loaded_at",          # 이 호출 이전에 마지막으로 성공 조회한
                                 # 시각(ISO, 없으면 공백) — "마지막 잔고
                                 # 조회 성공 시각"
    "cache_age_seconds",        # 이 호출 시점, prior_loaded_at 기준 캐시
                                 # 나이(초). prior_loaded_at 없으면 공백
    "balance_refresh_seconds",  # 그 시점 settings.trading.balance_
                                 # refresh_seconds 값(참고용, 실행 중 값이
                                 # 바뀌지 않는 한 매 행 동일)
    "has_unresolved_orders",    # 그 시점 _has_unresolved_orders()의 값
                                 # (기존 함수 그대로 호출한 결과 — 이
                                 # 로거가 그 판정을 바꾸지 않음)
    "error_type",                # 조회를 시도했다가 실패한 경우 예외
                                 # 클래스명(예: "Exception"), 조회 자체를
                                 # 시도하지 않았으면 공백
]


class BalanceFreshnessLogger:
    """S01 관측 1단계 — 잔고 조회 신선도(성공/실패/캐시 나이)를 있는 그대로
    append-only로 기록하는 로거입니다.

    다른 shadow 로거들(LowUpsideShadowLogger 등)과 달리 "같은 판단이
    반복 기록되는 오염"을 걱정할 필요가 없습니다 — 이 로그의 각 행은
    서로 다른 폴링 시점의 서로 다른 관측치이므로, 같은 값이 반복돼도
    그 자체가 유의미한 관측(예: "폴백이 몇 분간 이어졌는가")입니다.
    그래서 append_if_new() 대신 단순 append()만 제공합니다.
    """

    def __init__(self, file_path: str) -> None:
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)

        if self.file_path.exists():
            _migrate_csv_header_if_needed(self.file_path, BALANCE_FRESHNESS_FIELDS, "BALANCE_FRESHNESS")
        else:
            with self.file_path.open("w", newline="", encoding="utf-8") as fp:
                writer = csv.DictWriter(fp, fieldnames=BALANCE_FRESHNESS_FIELDS)
                writer.writeheader()

    def append(self, row: dict[str, Any]) -> None:
        with self.file_path.open("a", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(fp, fieldnames=BALANCE_FRESHNESS_FIELDS, extrasaction="ignore")
            for field in BALANCE_FRESHNESS_FIELDS:
                row.setdefault(field, "")
            writer.writerow(row)


# ── delayed_eval_candidate.csv ───────────────────────────────────────────────
# S02 관측 1단계 (2026-09-11, GPT 5차 검토 반영) → 2026-09-14 (GPT
# 재검토 20260914 반영). entry_watch의 실제 최소수익 판정 시점 근방
# (watch_minutes<=elapsed_min<=watch_minutes+1)을 넘어갈 때, 그 구간
# 안에서 avg>0인 유효한 평가가 단 한 번도 없었던 진입 건을 "지연 평가
# 후보"로만 기록합니다 — 이 로거는 SELL Signal을 만들지도, 반환하지도
# 않습니다(_check_entry_watch()는 이 로깅과 무관하게 여전히 None을
# 반환하고 정규 전략에 판단을 위임합니다). 지연 청산 자체의 활성화는
# 이번 단계의 범위가 아닙니다.
#
# 2026-09-14 갱신: 최초 구현은 판정 구간이 아니라 진입 후 아무
# 시점에서든(0분째 폴링 포함) avg>0 평가가 있으면 "봄"으로 기록해,
# 정작 판정 시점 근방을 놓친 진입 건까지 후보에서 빠지는 결함이
# 있었음(재현 확인) — 판정 구간으로 좁혔습니다.
#
# (symbol, entry_time) 기준 최초 1건만 기록합니다 — 창을 넘긴 뒤에도
# 그 포지션이 청산될 때까지 매 폴링 `_check_entry_watch()`가 계속
# 호출되므로, dedup이 없으면 보유 기간 내내 같은 후보가 반복 기록됩니다.
#
# 2026-09-14 (GPT 재검토 3차 반영) 갱신 — 두 가지를 보강:
# 1. current_price/avg_price가 0 이하이거나 무한/NaN이면(시세 조회
#    글리치 등) price_valid=False로 append()(dedup 없이 매번 기록)만
#    하고, append_if_new()로 (symbol, entry_time) 중복방지 슬롯을
#    소모하지 않습니다 — 그 슬롯을 먼저 차지하면 이후 정상 가격이
#    들어와도 dedup 때문에 다시 기록되지 않아, "지연 시점 수익률"
#    분석에 -100% 같은 무의미한 값만 남는 결함이 재현됐습니다.
# 2. price_observed_at/price_age_seconds를 추가해 이 관측이 실제로
#    최신 시세였는지, 캐시 재사용이었는지 나중에 판단할 수 있게 함
#    (price_age_seconds를 그 시점 settings.trading.price_refresh_
#    seconds와 비교하면 "가격 출처(fresh/재사용)"를 판단할 수 있음 —
#    별도 필드로 근사 추정하는 대신 이 값을 직접 비교하도록 함).
DELAYED_EVAL_CANDIDATE_FIELDS = [
    "detected_at",           # 창을 넘긴 것을 처음 관측한 시각(이 판정이
                              # 나온 폴링 시각)
    "symbol",
    "entry_time",             # state.entry_time_by_symbol의 진입 시각 —
                              # (symbol, entry_time)이 중복 방지 키
                              # (price_valid=True로 append_if_new()된
                              # 행에만 적용 — price_valid=False 행은
                              # append()로 dedup 없이 기록됨)
    "elapsed_min",            # 진입 후 경과 시간(분), 창(watch_minutes+1)을
                              # 얼마나 넘겼는지
    "watch_minutes",          # 그 시점 entry_watch.watch_minutes 설정값
    "avg_price",              # 평균 매수단가
    "current_price",          # 관측 시점 현재가
    "pnl_pct",                # (current_price-avg_price)/avg_price*100 —
                              # price_valid=False면 공백
    "minute_analysis_available",  # 이 호출에 fresh한 minute_analysis가
                              # 전달됐는지(True/False) — "유효한 시세"
                              # 판단의 참고 신호 중 하나(전체는 아님)
    "price_valid",            # current_price>0, avg_price>0이고 둘 다
                              # 유한값인지(True/False). False면 pnl_pct는
                              # 의미 없는 값이므로 분석에서 제외할 것
    "price_observed_at",      # 이 current_price가 캐시에 적재된 시각
                              # (ISO). 조회 자체가 안 됐으면 공백
    "price_age_seconds",      # detected_at 기준 price_observed_at의
                              # 나이(초). price_refresh_seconds보다 many
                              # 크면 재조회 실패로 오래된 캐시를 재사용
                              # 중이었다는 뜻
    "prior_seen_format",      # 이 시점 entry_watch_normal_eval_seen_by_
                              # symbol[symbol] 저장값의 형식 — "none"(값
                              # 없음)/"current"(entry_time 결합 신형식,
                              # entry_time 불일치)/"legacy"(2026-09-14
                              # 이전 구형식, bare timestamp만 저장돼
                              # entry_time 대조 불가)/"invalid"(문자열이
                              # 아니거나 손상됨). "legacy"/"invalid"는
                              # 실제 누락과 구분해서 집계할 것 —
                              # 배포 경계에서만 일시적으로 나타남
]


class DelayedEvalCandidateLogger:
    """S02 관측 1단계 — 정상 창을 통째로 놓친 진입 건을 (symbol, entry_time)당
    최초 1건만 기록하는 로거입니다. LowUpsideShadowLogger와 동일한
    dedup 패턴(재시작 시 기존 파일에서 키 복원)을 씁니다.
    """

    def __init__(self, file_path: str) -> None:
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._seen_keys: set[tuple] = set()

        if self.file_path.exists():
            _migrate_csv_header_if_needed(
                self.file_path, DELAYED_EVAL_CANDIDATE_FIELDS, "DELAYED_EVAL_CANDIDATE"
            )
            try:
                with self.file_path.open("r", newline="", encoding="utf-8") as fp:
                    reader = csv.DictReader(fp)
                    for row in reader:
                        try:
                            self._seen_keys.add(self._key(row))
                        except Exception:
                            continue
            except Exception as exc:
                logger.warning(
                    f"[DELAYED_EVAL_CANDIDATE] 기존 파일에서 중복방지 키 복원 실패"
                    f"(무시하고 빈 상태로 계속 진행): {exc}"
                )
        else:
            with self.file_path.open("w", newline="", encoding="utf-8") as fp:
                writer = csv.DictWriter(fp, fieldnames=DELAYED_EVAL_CANDIDATE_FIELDS)
                writer.writeheader()

    @staticmethod
    def _key(row: dict[str, Any]) -> tuple:
        return (str(row.get("symbol", "")), str(row.get("entry_time", "")))

    def append_if_new(self, row: dict[str, Any]) -> bool:
        """같은 (symbol, entry_time) episode에 대해 최초 1건만 기록하고
        True를 반환합니다. 이미 기록된 episode면 아무것도 안 하고
        False를 반환합니다."""
        key = self._key(row)
        if key in self._seen_keys:
            return False

        with self.file_path.open("a", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(fp, fieldnames=DELAYED_EVAL_CANDIDATE_FIELDS, extrasaction="ignore")
            for field in DELAYED_EVAL_CANDIDATE_FIELDS:
                row.setdefault(field, "")
            writer.writerow(row)

        self._seen_keys.add(key)
        return True

    def append(self, row: dict[str, Any]) -> None:
        """2026-09-14 (GPT 재검토 3차 반영) — dedup 없이 매번 기록합니다.

        무효 가격(current_price<=0 등) 관측을 기록할 때 씁니다.
        `append_if_new()`처럼 `_seen_keys`에 등록하지 않으므로, 이후
        같은 (symbol, entry_time)에 대해 유효한 가격이 들어오면
        `append_if_new()`가 정상적으로 그 행을 기록할 수 있습니다 —
        무효 관측이 (symbol, entry_time)당 1건뿐인 중복방지 슬롯을
        먼저 소모해 정상 관측 기회를 없애는 것을 방지합니다.
        """
        with self.file_path.open("a", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(fp, fieldnames=DELAYED_EVAL_CANDIDATE_FIELDS, extrasaction="ignore")
            for field in DELAYED_EVAL_CANDIDATE_FIELDS:
                row.setdefault(field, "")
            writer.writerow(row)
