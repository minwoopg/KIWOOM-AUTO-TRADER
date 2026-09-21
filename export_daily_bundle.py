#!/usr/bin/env python3
"""분석용 일일 번들 export (2026-08-06, 1I → 1I.1)

실행:
    python export_daily_bundle.py                # 오늘 날짜
    python export_daily_bundle.py 2026-08-06     # 특정 날짜

결과: exports/bundle_YYYYMMDD.zip

왜 필요한가
-----------
분석 때마다 signal_log.csv(65MB, 23만행)와 app.log(9MB) 전체를
올려야 했습니다. 해당 거래일 몫만 잘라내면 수 MB로 줄어듭니다.

1I.1에서 보완한 것 (GPT 코드리뷰)
--------------------------------
1) **fail-closed 날짜 slicing** — 시간 컬럼을 못 찾으면 전체를
   복사하던 fallback 제거. 일일 번들은 데이터 최소화가 목적이라
   스키마를 모르면 아예 제외하고 MANIFEST에 SCHEMA_ERROR를 남김.
2) **entry_watch_shadow는 trigger_at 기준** — 실제 컬럼명 반영.
3) **allowlist 전용 로그 추출** — 모든 WARNING/ERROR 자동 포함을
   제거. allowlist에 없는 인증·계좌·주문응답 로그가 WARNING이라는
   이유로 번들에 실릴 수 있었음.
4) **키 기반 민감정보 마스킹** — 토큰·계좌 관련 키의 값만 가림.
   날짜·시간·종목코드를 무차별로 지우지 않음.
5) **수집 품질 메타데이터** — 재시작 횟수, session ready 비율,
   shadow-실매수 연결률 등을 collection_quality.txt로 기록.
6) **원자적 ZIP 생성 + 동시 실행 보호** — 고유 임시 디렉터리,
   .tmp 작성 후 무결성 확인, os.replace로 교체, 락 파일.

2026-08-20 observability-only fix
----------------------------------
LOG_TAGS allowlist에 1P0.8-D.1/D.1.1의 order-status reconciliation
로그 태그 5종을 추가(`[ORDER_STATUS_QUERY_FAILED]`,
`[ORDER_STATUS_BALANCE_MISMATCH]`, `[ORDER_STATUS_UNSUPPORTED]`,
`[ORDER_STATUS_CONFIRMED]`, `[LIFECYCLE_ORPHAN]`) — allowlist가
D.1/D.1.1(8/18~19 신설)보다 오래돼서 운영 관측에 필요한 로그가
계속 걸러지고 있었음(8/20 bundle 분석 중 발견). 매매 로직·조회
호출·로그 생성 자체는 무변경, LOG_TAGS 튜플만 확장.

2026-08-24 observability-only closure (E.1-A 운영 검증용)
-----------------------------------------------------------
같은 유형의 gap을 8/24 bundle 분석 중 또 발견: E.1-A(durable
tracked order journal, 8/20 도입)와 1P0.8-A.1(order_id 연결,
8/14 도입)의 CRITICAL 로그 태그 3종이 이 allowlist에 없어서
daily bundle에 전혀 실리지 않고 있었음 — `[TRACKED_ORDER_JOURNAL_ERROR]`
(journal 기록/유지 실패), `[ORDER_ID_MISSING]`(accepted=True인데
order_id가 비어 있음), `[ORDER_PLACEMENT_AMBIGUOUS]`(주문 접수
여부 불명, 사람 확인 필요). 이번에도 "0건"이 "정상"인지 "번들이
안 담았다"인지 구분이 안 되는 문제였음. 세 태그 모두 symbol/
order_id/side만 포함하고 SENSITIVE_KEYS 대상 필드는 담지 않음.
이번 라운드에서 존재하는지 확인했으나 별도로 성공 시점(journal
create/update/orphan/terminal remove)을 남기는 로그 태그는 코드베이스에
아직 없음 — 새로 만들지 않음(범위를 exporter allowlist 확장으로만
좁힘, 민우님 지시). 매매 로직·journal 동작·로그 생성 호출 자체는
전혀 건드리지 않음, LOG_TAGS 튜플만 확장.
"""
from __future__ import annotations

import csv
import dataclasses
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from datetime import date, datetime
from pathlib import Path

from infra.storage.run_baseline import (
    RUN_BASELINE_FIELDS,
    load_run_baselines,
    parse_run_timestamp,
    resolve_scope_for_trade_timestamp,
)
from infra.storage.order_status_observation_store import (
    OrderStatusObservation,
    compute_coverage,
    dedupe_by_write_id,
    find_quarantined_observation_files,
    status_path_for,
)
from infra.broker.kiwoom_order_status import normalize_order_id
from utils.time_utils import KST_TZ

LOGS_DIR = Path("logs")
REPORTS_DIR = Path("reports")
EXPORTS_DIR = Path("exports")

# stale lock 판정 기준 — 정상 export는 수 분 이내에 끝납니다.
STALE_LOCK_SECONDS = 30 * 60

# 날짜별로 잘라낼 CSV — (파일명, 시간 컬럼 후보)
# 2026-08-06 (1I.1): entry_watch_shadow의 실제 시간 컬럼은
# trigger_at (헤더 확인: trigger_at,symbol,trigger_type,...)
CSV_SOURCES: list[tuple[str, tuple[str, ...]]] = [
    ("signal_log.csv", ("timestamp",)),
    ("entry_quality_shadow.csv", ("timestamp",)),
    ("entry_watch_shadow.csv", ("trigger_at", "timestamp", "buy_time")),
    ("trades.csv", ("timestamp", "time", "체결시간")),
    ("position_lifecycle.csv", ("timestamp",)),
    # 2026-08-24 (Profitability Shadow v2 closure, 민우님 코드리뷰 지적):
    # 이 목록은 명시적 allowlist라 자동으로 새 CSV를 담지 않습니다 —
    # Shadow v2가 새로 남기는 두 파일을 여기 추가하지 않으면 실시간
    # 로그는 정상 쌓여도 daily bundle에는 실리지 않아, Profitability
    # Sprint가 매일 bundle 기준으로 분석하는 한 이 기능의 목적(실시간
    # shadow 표본을 빠르게 쌓아 다음 Sprint에서 쓰는 것) 자체를 달성할
    # 수 없습니다.
    ("low_upside_shadow.csv", ("timestamp",)),
    ("min_profit_extension_shadow.csv", ("timestamp",)),
    # 2026-09-11 (S01/S02 관측 1단계, GPT 5차 검토 반영): 위와 동일한
    # 이유로 명시 추가하지 않으면 실시간 로그는 쌓여도 daily bundle에는
    # 실리지 않습니다. 두 파일 모두 순수 관측이며 BUY/SELL 판정에는
    # 관여하지 않습니다.
    ("balance_freshness.csv", ("timestamp",)),
    ("delayed_eval_candidate.csv", ("detected_at",)),
    # 2026-09-15 (180초 감시 공백 대응 3단계, GPT 재검토 5번 지적
    # 반영): 위와 동일한 이유로 명시 추가하지 않으면 잔고 장애 관측
    # 로그(exit_candidate_outage.csv)가 실시간으로는 쌓여도 daily
    # bundle에는 실리지 않아, 장애 발생일 분석에서 핵심 자료가 누락됨.
    # 순수 관측이며 BUY/SELL/체결 확정 판정에는 관여하지 않음.
    ("exit_candidate_outage.csv", ("detected_at",)),
]

# app.log에서 뽑아낼 태그 — **allowlist 전용**.
# 2026-08-06 (1I.1): "모든 WARNING/ERROR 자동 포함"을 제거함.
# allowlist에 없는 인증·계좌·주문응답 로그가 WARNING이라는 이유로
# 번들에 실릴 수 있었기 때문. 1F에서 스윙을 폐기했으므로
# [COND_SWING]도 제거.
#
# 2026-08-20 (observability-only fix, 민우님 GPT 리뷰 반영):
# 1P0.8-D.1(2026-08-18)/D.1.1(2026-08-19)이 새로 남기는 order-status
# reconciliation 로그 태그가 이 allowlist 신설 당시(1I.1, 2026-08-06)
# 존재하지 않았던 태그라 계속 누락되고 있었음 — D.1/D.1.1 운영 관측
# 1~2거래일을 하기로 확정했는데 정작 관련 로그가 daily bundle에서
# 전부 걸러지고 있었던 것(8/20 bundle 분석 중 발견, 0건으로 나온 건
# "이벤트가 없었다"가 아니라 "번들이 안 담았다"는 뜻이었음). 아래
# 5개 태그를 추가 — 전부 domain/service/trading_service.py의
# _reconcile_tracked_order_status()/observe_for_orphan() 경로에서만
# 쓰이고, order_id/symbol/수량(broker_qty 등)만 포함할 뿐 SENSITIVE_KEYS
# 대상 필드(계좌번호/토큰 등)는 담지 않음 — 매매 로직/조회 호출/로그
# 생성 자체는 전혀 건드리지 않은 **수집 대상 확장만**.
#
# 2026-08-24 (observability-only closure, E.1-A 운영 검증용): 아래
# 3개 태그 추가. 전부 domain/service/trading_service.py의 기존
# CRITICAL 로그 경로(_create_tracked_order_journal_entry()/
# _maintain_tracked_order_journal()/_try_buy()/SELL accepted 분기)
# 에서만 쓰이고, symbol/order_id/side만 포함할 뿐 SENSITIVE_KEYS
# 대상 필드(계좌번호/토큰 등)는 담지 않음 — 수집 대상 확장만.
#
# 2026-09-11 (B01 보완, 민우님/GPT 지적): run_baseline.csv 기록이
# 실패해도(app/main.py의 try/except) 그 사실이 daily bundle 어디에도
# 남지 않으면, 나중에 "그날 실행-거래 조인이 왜 안 맞는지"를 알 방법이
# 없음(같은 유형의 "0건이 정상인지 누락인지 구분 안 됨" 문제).
# [RUN_BASELINE] 태그를 추가 — 성공/실패 라인 모두 이 태그로 시작하며
# symbol/order_id 등 민감 필드는 포함하지 않음(git_sha/config_hash만).
# 2026-09-18 (재검토 지적 2번): OrderStatusObservationRecorder가 남기는
# 큐 포화/쓰기 실패/부분쓰기 복구/종료마커 기록 실패 로그 태그가 이
# allowlist에 없어서, 요약 텍스트는 "app.log 슬라이스에서 확인하라"고
# 안내하면서도 실제 번들에는 해당 줄이 전혀 포함되지 않았습니다
# (재현 확인됨). 5개 태그 추가 — 전부 순수 진단/관측 목적이며
# order_id/restart_id/dropped_count 등 저장 품질 수치만 담고
# SENSITIVE_KEYS 대상 필드는 담지 않음 — 매매 로직·저장 로직 자체는
# 무변경.
LOG_TAGS: tuple[str, ...] = (
    "[COND_STATUS]", "[COND_TRUNCATE]", "[COND]",
    "[WS]", "[SESSION_SHADOW]", "[EXPERIMENTAL]",
    "[REPORT]", "[ANALYSIS]", "[RECONCILE]", "[MIN_STALE]",
    "[ORDER_STATUS_QUERY_FAILED]", "[ORDER_STATUS_BALANCE_MISMATCH]",
    "[ORDER_STATUS_UNSUPPORTED]", "[ORDER_STATUS_CONFIRMED]",
    "[LIFECYCLE_ORPHAN]",
    "[TRACKED_ORDER_JOURNAL_ERROR]", "[ORDER_ID_MISSING]",
    "[ORDER_PLACEMENT_AMBIGUOUS]",
    "[RUN_BASELINE]", "[CONFIG_SNAPSHOT_MISSING]",
    "[ORDER_STATUS_OBS_QUEUE_FULL]", "[ORDER_STATUS_OBS_WRITE_FAILED]",
    "[ORDER_STATUS_OBS_RECOVERY]", "[ORDER_STATUS_OBS_SHUTDOWN_MARKER_FAILED]",
    "[ORDER_STATUS_OBS_FSYNC_FAILED]",
)

# ── 민감정보 마스킹 ─────────────────────────────────────────────
# 키 기반 우선 — 날짜·시간·종목코드까지 무차별로 지우지 않기 위해.
SENSITIVE_KEYS = (
    "authorization", "bearer",
    # 2026-08-06 (1I.4, GPT 코드리뷰 P0, 재현 확인): 아래 키들이
    # 목록에 없어서 실제 키움 API 자격증명이 전부 누출됐음.
    #   infra/broker/kiwoom_broker.py:90-91 {"appkey":…, "secretkey":…}
    #   infra/broker/kiwoom_broker.py:107   token = body.get("token")
    #   infra/notify/kakao_notifier.py      rest_api_key, client_id
    # 재현: {"token":"SECRET1"} / {"appkey":"SECRET2"} /
    #       {"secretkey":"SECRET3"} 모두 원문 유지.
    "token", "access_token", "refresh_token",
    "appkey", "app_key", "secretkey", "secret_key",
    "api_key", "apikey", "rest_api_key", "client_secret", "client_id",
    "secret", "password", "passwd",
    "account_number", "account_no", "accountno", "account",
    "계좌번호", "계좌",
)
# 긴 키부터 정렬 — "token"이 "access_token"보다 먼저 매칭되면
# 접두사만 남는 부분 매칭이 생기므로.
_KEY_ALT = "|".join(re.escape(k) for k in sorted(SENSITIVE_KEYS, key=len, reverse=True))

# 2026-08-06 (1I.4, GPT 지적): 단일 정규식으로 quoted/unquoted를
# 모두 처리하려다 값 종료 문자에 공백·쉼표·세미콜론이 포함돼
# {"password":"hello world"} / {"secret":"abc,def"} 같은 값이
# 마스킹되지 않았음. **큰따옴표 / 작은따옴표 / 무따옴표 세 패턴으로
# 분리**하는 편이 안정적이라 그렇게 구현함.
#
# quoted 패턴은 닫는 따옴표까지를 값으로 보므로 공백·쉼표·세미콜론이
# 들어가도 전부 가려짐. unquoted 패턴만 구분자에서 값을 끊음.
_DQ_KV_RE = re.compile(
    rf'(?i)(?P<kq>"?)(?P<key>{_KEY_ALT})(?P=kq)(?P<sep>\s*[:=]\s*)"(?P<val>[^"]*)"'
)
_SQ_KV_RE = re.compile(
    rf"(?i)(?P<kq>'?)(?P<key>{_KEY_ALT})(?P=kq)(?P<sep>\s*[:=]\s*)'(?P<val>[^']*)'"
)
_UQ_KV_RE = re.compile(
    rf'(?i)(?P<key>{_KEY_ALT})(?P<sep>\s*[:=]\s*)(?P<val>[^\s,;}}\)\]"\']+)'
)
# "Bearer <token>" 처럼 키 뒤에 공백으로 이어지는 형태
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}")
# 계좌번호 형태 — 8자리-2자리
_ACCT_DASH_RE = re.compile(r"\b\d{8}-\d{2}\b")
# 10자리 이상 연속 숫자. 종목코드(6자리)·날짜(8자리)·분봉 타임스탬프
# (14자리)를 피하기 위해 10~13자리만, 그리고 앞뒤가 숫자가 아닐 때만.
_ACCT_LONG_RE = re.compile(r"(?<!\d)\d{10,13}(?!\d)")


def _mask_dq(m: "re.Match") -> str:
    return f'{m.group("kq")}{m.group("key")}{m.group("kq")}{m.group("sep")}"***"'


def _mask_sq(m: "re.Match") -> str:
    return f"{m.group('kq')}{m.group('key')}{m.group('kq')}{m.group('sep')}'***'"


def _mask_uq(m: "re.Match") -> str:
    return f'{m.group("key")}{m.group("sep")}***'


def mask(line: str) -> str:
    """민감정보를 가립니다. quoted → Bearer → unquoted → 형태 순서."""
    line = _DQ_KV_RE.sub(_mask_dq, line)
    line = _SQ_KV_RE.sub(_mask_sq, line)
    line = _BEARER_RE.sub("Bearer ***", line)
    line = _UQ_KV_RE.sub(_mask_uq, line)
    line = _ACCT_DASH_RE.sub("***", line)
    line = _ACCT_LONG_RE.sub("***", line)
    return line


# 2026-09-18 (재검토 지적 6번): mask()는 자유 텍스트 로그 줄을 위해
# 만들어진 정규식 기반 함수라 "따옴표 없는 10~13자리 숫자"까지
# 가립니다(_ACCT_LONG_RE). JSON으로 직렬화된 관측 레코드 전체
# 문자열에 그대로 적용하면, 원래 숫자였던 필드 값(예: order_id를
# 정수로 담은 필드, 또는 우연히 10~13자리인 다른 숫자 필드)이
# 따옴표 없는 `***`로 바뀌어 그 줄 전체가 더 이상 유효한 JSON이
# 아니게 됩니다(재현: 합성 원문에 숫자 필드 1234567890을 넣으면
# 결과 줄이 파싱 불가).
#
# 그래서 JSON 레코드는 파싱된 객체 상태에서 이 함수로 재귀적으로
# 처리합니다 — 키 이름이 SENSITIVE_KEYS와 "정확히" 일치할 때만
# (부분 문자열 매칭 아님) 그 값을 "***"로 치환합니다. 숫자·불리언·
# None 값은 (민감 키가 아닌 한) 그대로 두므로 JSON 문법이 깨지지
# 않습니다. 이렇게 하면 "account_scope_id" 같은 필드도 "account"와
# 정확히 일치하지 않으므로 실수로 가려지지 않습니다.
_SENSITIVE_KEYS_LOWER = {k.lower() for k in SENSITIVE_KEYS}

# 2026-09-18 재재검토 반영(지적 4번, 재현된 버그): 이전 구현은 민감
# 키가 아닌 "모든" 문자열 값에도 자유문자열용 mask()를 적용했습니다.
# mask()의 _ACCT_LONG_RE는 문맥과 무관하게 10~13자리 숫자 문자열을
# "***"로 가리므로, requested_order_id="1234567890"이나
# cntr_pric_raw="1234567890"처럼 **의미가 명확한 식별자 필드**(주문
# 번호·가격·수량 원문)까지 뭉개져 서로 다른 주문번호가 전부 같은
# "***"가 되고, 후속 연결·집계 근거가 훼손됐습니다(재현 확인).
#
# 이제 mask()는 아래 화이트리스트에 명시된, 실제로 "자유 텍스트"인
# 필드(예외 메시지 등 — 그 안에 무엇이 섞여 들어올지 스키마로 보장할
# 수 없는 필드)에만 적용합니다. 그 외 필드는 키가 SENSITIVE_KEYS와
# 정확히 일치하지 않는 한 원문 그대로 보존합니다 — `raw` 딕셔너리
# 내부의 원본 API 필드들(response_order_id/ord_qty_raw/cntr_pric_raw
# 등)도 이 모듈의 docstring에 명시된 전제(관측 레코드 자체는
# 계좌번호/토큰을 담지 않는 필드만 씀)에 따라 식별자로 보존됩니다 —
# 그 안에 우연히 SENSITIVE_KEYS와 정확히 일치하는 키가 있으면(예:
# 미래에 원문에 "token" 키가 추가되는 등) 그 값은 여전히 재귀적으로
# 가려집니다.
_FREEFORM_TEXT_FIELDS = {"error_repr"}


def _mask_json_value(value, key: str | None = None):
    """파싱된 JSON 값(dict/list/스칼라)을 재귀적으로 마스킹합니다.

    - dict: 키 이름이 SENSITIVE_KEYS와 정확히 일치(대소문자 무시)하면
      값을 통째로 "***"로 치환(하위 구조까지 있어도 더 내려가지
      않음 — 민감 필드 내부 구조를 부분 노출하지 않기 위함). 그 외
      키는 그 키 이름을 들고 값을 재귀 처리.
    - list: 각 원소를 재귀 처리(원소 자체는 특정 키에 속하지 않으므로
      key를 그대로 전달 — 리스트 안의 문자열 원소에 자유문자열
      마스킹을 적용할지는 리스트를 담고 있던 상위 키로 판단).
    - str: 이 값을 가리키는 키가 `_FREEFORM_TEXT_FIELDS`에 있을 때만
      기존 텍스트용 mask()를 적용합니다. 그 외 문자열(주문번호·가격·
      수량 등 식별자 필드)은 원문 그대로 보존합니다.
    - 그 외(숫자/불리언/None): 그대로 반환 — JSON 구조를 깨뜨리는
      원인이었던 부분이라 여기서는 절대 문자열 치환을 하지 않음.
    """
    if isinstance(value, dict):
        result = {}
        for k, v in value.items():
            if str(k).strip().lower() in _SENSITIVE_KEYS_LOWER:
                result[k] = "***" if v is not None else None
            else:
                result[k] = _mask_json_value(v, key=k)
        return result
    if isinstance(value, list):
        return [_mask_json_value(v, key=key) for v in value]
    if isinstance(value, str) and key in _FREEFORM_TEXT_FIELDS:
        return mask(value)
    return value


# ── CSV slicing ─────────────────────────────────────────────────
class SchemaError(Exception):
    """시간 컬럼을 찾지 못해 날짜 slicing이 불가능한 경우."""


def slice_csv(src: Path, dst: Path, target: date, ts_cols: tuple[str, ...]) -> tuple[int, int]:
    """해당 날짜 행만 골라 새 CSV로 씁니다. (전체행, 추출행) 반환.

    2026-08-06 (1I.1): 시간 컬럼을 못 찾으면 예전엔 전체 행을
    복사했는데, 일일 번들은 데이터 최소화가 목적이므로
    **fail-closed**가 맞음 — SchemaError를 올려 해당 파일을
    번들에서 제외하고 MANIFEST에 기록.
    """
    day = target.strftime("%Y-%m-%d")
    day_compact = target.strftime("%Y%m%d")
    total = kept = 0
    with src.open(newline="", encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        fields = list(reader.fieldnames or [])
        if not fields:
            raise SchemaError("empty header")
        col = next((c for c in ts_cols if c in fields), None)
        if col is None:
            raise SchemaError("timestamp column not found")
        with dst.open("w", newline="", encoding="utf-8") as out:
            writer = csv.DictWriter(out, fieldnames=fields)
            writer.writeheader()
            for row in reader:
                total += 1
                v = str(row.get(col) or "")
                if v.startswith(day) or v.startswith(day_compact):
                    writer.writerow(row)
                    kept += 1
    return total, kept


def rotated_log_paths(logs_dir: Path) -> list[Path]:
    """app.log 와 app.log.1 ~ app.log.10 만 대상으로 합니다.

    2026-08-06 (1I.2, GPT 코드리뷰 P0-2): 프로그램은
    RotatingFileHandler(20MB × 백업 10개)를 쓰므로 거래량이 많은
    날은 같은 날짜 로그가 app.log / app.log.1 / app.log.2 ...로
    나뉩니다. 1I.1의 exporter는 app.log 하나만 읽어서 오전 로그나
    이전 재시작 로그를 놓쳤고, **이번에 session 결론을 잘못 냈던
    직접 원인이 바로 이 로테이션 누락**이었습니다.

    `app copy.log` 같은 임의 파일은 포함하지 않습니다 — 정확히
    `app.log`와 `app.log.<1~10>`만.
    """
    found = [logs_dir / "app.log"] if (logs_dir / "app.log").exists() else []
    for i in range(1, 11):
        p = logs_dir / f"app.log.{i}"
        if p.exists():
            found.append(p)
    return found


def slice_log(sources: list[Path], dst: Path, target: date) -> tuple[int, int, list[str], list[str]]:
    """여러 로그 파일에서 allowlist 태그 줄만 모아 시간순으로 씁니다.

    반환: (전체줄, 추출줄, 추출된 줄 목록, 사용한 소스 파일명)
    """
    day = target.strftime("%Y-%m-%d")
    total = kept = 0
    collected: list[str] = []
    used: list[str] = []
    for src in sources:
        hit = 0
        with src.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                total += 1
                if not line.startswith(day):
                    continue
                if not any(t in line for t in LOG_TAGS):
                    continue
                collected.append(mask(line.rstrip("\r\n")))
                hit += 1
        if hit:
            used.append(f"{src.name} ({hit:,}줄)")
        kept += hit
    # 로테이션 파일은 app.log.N이 오래된 순이라 파일 순서가 시간순이
    # 아님 — 타임스탬프 접두사로 정렬해 하나의 파일로 합칩니다.
    collected.sort(key=lambda l: l[:23])
    dst.write_text("\n".join(collected) + ("\n" if collected else ""), encoding="utf-8")
    return total, kept, collected, used


# ── 체결조회 증거 관측 로그 (order_status_observations.jsonl) ─────
# 2026-09-18 (우선순위1 1차, 지적 5번의 "번들 연결까지 1차에 포함"
# 반영): `OrderStatusObservationRecorder`가 남기는 append-only JSONL을
# 날짜로 잘라 raw로 포함하고, `compute_coverage()`로 커버리지 요약을
# 만듭니다. 이 섹션은 다음을 지킵니다:
#   - 마지막 줄이 불완전해도(강제종료 중 write) 그 한 줄만 제외하고
#     번들 생성을 실패시키지 않습니다(라이브 로그 파일 자체는 건드리지
#     않음 — `_quarantine_incomplete_tail()`과 달리 이건 읽기 전용
#     스냅샷 처리).
#   - 마지막 줄이 아닌 위치의 손상은 "파일 자체 손상 가능성"으로
#     별도 표시하고 계속 진행합니다(조용히 넘어가지 않음).
#   - `dedupe_by_write_id()`로 같은 write_id+같은 내용의 재시도 기록은
#     1건으로 집계하고, 같은 write_id+다른 내용은 충돌로 별도 표시합니다.
#   - 원문(raw) 필드는 이 exporter의 기존 `mask()`(SENSITIVE_KEYS 기반)를
#     한 번 더 적용합니다 — 관측 레코드 자체는 계좌번호/토큰을 담지
#     않는 필드만 쓰지만(모듈 docstring 참고), export 단계에서도
#     동일한 방어선을 적용해 두 지점 중 하나가 뚫려도 나머지가
#     막도록 합니다. 마스킹이 실제로 무언가를 가렸다면 그 사실을
#     manifest에 남겨 "조용히 사라진 정보"가 없게 합니다.
#   - 이 번들은 하루 단위이므로, 오늘 조회했지만 **다른 날짜에 접수된
#     주문**은 이 번들만으로는 order_date를 확정할 수 없어 "미확인"
#     으로 분류됩니다 — 이는 결함이 아니라 하루 단위 번들의 알려진
#     한계입니다(1차 지적 3번의 "미래 B의 확정 키를 정하자는 뜻이
#     아니라 현재 A의 통계부터 서로 다른 주문을 섞지 말자는 조건"과
#     동일한 원칙 — 확인할 수 없으면 추정하지 않고 미확인으로 둠).
def _default_order_status_observation_log_path() -> Path:
    """`config.settings.StorageConfig.order_status_observation_log_file`의
    기본값을 그대로 읽어와 이 경로를 exporter에 별도로 하드코딩하지
    않습니다(2026-09-18 재검토 지적 6번: "설정한 관측 파일 경로도
    exporter가 현재 고정 경로 대신 일관되게 사용하도록 맞추세요").

    StorageConfig는 state_file/trade_log_file 등 여러 필수(기본값
    없는) 인자를 요구하는 dataclass라서 인스턴스를 만들 수 없습니다
    (이 exporter는 독립 실행 스크립트라 실행 중인 Settings 인스턴스에
    접근할 방법이 없음) — 그래서 `dataclasses.fields()`로 해당 필드의
    **기본값**만 읽습니다. 필드가 사라지거나 기본값이 없어지는
    비정상 상황에서는 기존과 동일한 리터럴로 안전하게 폴백합니다.
    """
    try:
        from config.settings import StorageConfig as _StorageConfig
        for f in dataclasses.fields(_StorageConfig):
            if f.name == "order_status_observation_log_file" and f.default is not dataclasses.MISSING:
                return Path(f.default)
    except Exception:
        pass
    return LOGS_DIR / "order_status_observations.jsonl"


ORDER_STATUS_OBSERVATION_LOG = _default_order_status_observation_log_path()


def resolve_order_status_observation_log_path(
    settings_path: str | Path = "config/settings.yaml",
) -> tuple[Path, bool]:
    """실제 설정 파일(기본: config/settings.yaml)의
    `storage.order_status_observation_log_file` 값을 읽어옵니다
    (2026-09-18 재재검토 반영, 지적 4번의 두 번째 재현: "설정 필드의
    기본값을 읽는 것은 실제 설정값을 읽는 것과 다릅니다" — 사용자가
    settings.yaml에서 `custom/obs.jsonl`로 지정해도 exporter는
    `_default_order_status_observation_log_path()`(필드의 **기본값**만
    읽음)를 계속 써서 항상 `logs/order_status_observations.jsonl`을
    반환했습니다).

    이제 앱 기동 경로와 동일한 `config.settings.load_settings()`로
    실제 설정을 로드해 그 값을 씁니다. 이 exporter는 독립 실행
    스크립트라 실행 중인 앱의 Settings 인스턴스에 접근할 방법이
    없으므로, 매 호출마다 설정 파일을 다시 읽습니다(자동 실행
    빈도를 고려하면 비용이 무시할 만한 수준). 지정된 설정 파일이
    없거나 파싱에 실패하면(예: 테스트 환경, 설정 파일 미배치)
    조용히 죽지 않고 기존과 동일한 하드코딩 기본값으로 폴백합니다.

    반환값: (경로, 실제 설정에서 읽었는지 여부). 두 번째 값이
    False면 폴백이 발생했다는 뜻이며, 호출부(`build()`)가 이를
    MANIFEST에 남겨 "조용히 사용자 설정이 무시되는" 일이 없게
    합니다.
    """
    try:
        from config.settings import load_settings as _load_settings
        settings = _load_settings(settings_path)
        raw = str(settings.storage.order_status_observation_log_file or "").strip()
        if raw:
            return Path(raw), True
    except Exception:
        pass
    return _default_order_status_observation_log_path(), False

_OBS_KNOWN_FIELDS = {f.name for f in dataclasses.fields(OrderStatusObservation)}


def slice_jsonl_observations(
    src: Path, dst: Path, target: date,
    *, quarantine_paths: list[Path] | None = None,
) -> tuple[int, int, int, int, int, int, int, int]:
    """관측 JSONL을 날짜로 잘라 새 파일로 씁니다(마스킹 재적용 포함).

    반환: (전체 줄, 해당 날짜로 채택된 줄, 말미 불완전으로 제외된 줄,
    말미가 아닌 위치의 손상 줄, 마스킹이 실제로 값을 바꾼 줄 수,
    격리 파일 수, 격리 파일에서 이 날짜로 복구된 줄 수, 격리 파일
    내에서 손상돼 제외된 줄 수)

    2026-09-18 재검토 지적 6번 반영: 마스킹은 문자열 전체가 아니라
    **파싱된 JSON 객체**에 대해 재귀적으로 적용합니다(`_mask_json_value`)
    — 그래서 결과 줄은 항상 다시 `json.loads()`로 파싱 가능한 유효한
    JSON입니다(재현된 버그: 숫자 필드가 따옴표 없는 `***`로 치환돼
    파싱 불가가 되던 문제).

    2026-09-21 3차 재검토 반영(지적 1번, 재현된 버그): `src`가 격리
    (`_quarantine_and_rotate_file()`)로 옆에 치워진 적이 있으면, 격리
    직전까지 그 파일에 쌓여 있던 정상 레코드는 삭제되지 않고
    `quarantine_paths`가 가리키는 파일들 안에 그대로 남아있습니다 —
    예전엔 exporter가 `src`(격리 이후 새로 시작된 파일)만 읽어서 이
    레코드들이 이후 번들·집계 어디에도 다시 나타나지 않았습니다. 이제
    그 파일들도 같은 날짜 필터(+마스킹)를 적용해 `dst`에 함께
    포함합니다. `src`가 아예 존재하지 않아도(격리 후 아직 새 파일이
    생기지 않은 경우) 예외 없이 진행합니다.
    """
    day = target.strftime("%Y-%m-%d")
    lines = src.read_text(encoding="utf-8", errors="replace").splitlines() if src.exists() else []
    total = len(lines)
    kept = 0
    trailing_incomplete = 0
    mid_file_corrupt = 0
    masked_changed = 0
    out_lines: list[str] = []
    last_idx = total - 1
    for i, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            if i == last_idx:
                trailing_incomplete += 1
            else:
                mid_file_corrupt += 1
            continue
        if data.get("__marker__") == "shutdown":
            # 2026-09-18 재재검토 반영(지적 5번, 재현된 버그): 예전엔
            # 종료 마커를 날짜와 무관하게 전부 포함했습니다 — 그러면
            # 몇 달 전의 정상 종료 마커가 오늘 번들에도 계속 나타나
            # "과거에 정상 종료했다"는 사실이 "지금도 정상"이라는
            # 착시를 줄 위험이 있습니다(지적: "과거 정상 종료 마커가
            # 있다는 사실로 현재 실행의 저장 상태를 판단할 수는
            # 없습니다"). 이제 일반 관측과 동일하게 마커도
            # `shutdown_at`이 이 번들의 날짜인 것만 포함합니다 —
            # "지금 실행 중"인지는 이 마커가 아니라 별도의 실행 상태
            # 스냅샷(`_read_running_status()`)으로 판단합니다.
            marker_at = str(data.get("shutdown_at") or "")
            if not marker_at.startswith(day):
                continue
            masked_data = _mask_json_value(data)
            out_lines.append(json.dumps(masked_data, ensure_ascii=False, sort_keys=True))
            continue
        started_at = str(data.get("started_at") or "")
        if not started_at.startswith(day):
            continue
        kept += 1
        original = json.dumps(data, ensure_ascii=False, sort_keys=True)
        masked_data = _mask_json_value(data)
        masked_line = json.dumps(masked_data, ensure_ascii=False, sort_keys=True)
        if masked_line != original:
            masked_changed += 1
        out_lines.append(masked_line)

    quarantine_file_count = 0
    quarantine_recovered = 0
    quarantine_skipped = 0
    for qpath in (quarantine_paths or []):
        quarantine_file_count += 1
        try:
            q_lines = qpath.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for q_raw_line in q_lines:
            q_stripped = q_raw_line.strip()
            if not q_stripped:
                continue
            try:
                q_data = json.loads(q_stripped)
            except json.JSONDecodeError:
                # 격리 파일 자체가 "복구 불가능한 손상"이 확인된 뒤에
                # 옆으로 치워진 것이므로, 위치(말미/중간) 구분 없이
                # 손상 줄로만 집계합니다 — 이미 별도로 격리된 파일이라
                # 정상 파일의 "말미만 예외" 가정을 적용할 근거가 없음.
                quarantine_skipped += 1
                continue
            if q_data.get("__marker__") == "shutdown":
                marker_at = str(q_data.get("shutdown_at") or "")
                if not marker_at.startswith(day):
                    continue
                out_lines.append(
                    json.dumps(_mask_json_value(q_data), ensure_ascii=False, sort_keys=True)
                )
                quarantine_recovered += 1
                continue
            q_started_at = str(q_data.get("started_at") or "")
            if not q_started_at.startswith(day):
                continue
            out_lines.append(
                json.dumps(_mask_json_value(q_data), ensure_ascii=False, sort_keys=True)
            )
            quarantine_recovered += 1

    dst.write_text("\n".join(out_lines) + ("\n" if out_lines else ""), encoding="utf-8")
    return (
        total, kept, trailing_incomplete, mid_file_corrupt, masked_changed,
        quarantine_file_count, quarantine_recovered, quarantine_skipped,
    )


def _parse_observations_for_coverage(dst: Path) -> tuple[list[OrderStatusObservation], list[str]]:
    """슬라이스된 관측 JSONL에서 커버리지 계산용 레코드만 복원합니다.

    종료 마커(`__marker__`)는 `OrderStatusObservation`이 아니므로 제외합니다.
    알 수 없는 필드(미래 스키마 변경분)가 섞여 있어도 이 exporter가
    깨지지 않도록 알려진 필드만 골라 재구성합니다.
    """
    observations: list[OrderStatusObservation] = []
    parse_errors: list[str] = []
    if not dst.exists():
        return observations, parse_errors
    for raw_line in dst.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            parse_errors.append(str(exc))
            continue
        if data.get("__marker__") == "shutdown":
            continue
        filtered = {k: v for k, v in data.items() if k in _OBS_KNOWN_FIELDS}
        try:
            observations.append(OrderStatusObservation(**filtered))
        except TypeError as exc:
            parse_errors.append(f"필드 불일치: {exc}")
    return observations, parse_errors


def _parse_shutdown_markers(dst: Path) -> list[dict]:
    """슬라이스된 관측 JSONL에서 종료 마커(`__marker__=shutdown`) 줄만
    골라 반환합니다(2026-09-18 재검토 반영, 지적 2번) — 기록기가
    실제로 정상 종료됐는지, 그때 유실 건수가 몇 건이었는지를 번들
    안에서 바로 확인할 수 있게 합니다."""
    markers: list[dict] = []
    if not dst.exists():
        return markers
    for raw_line in dst.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if data.get("__marker__") == "shutdown":
            markers.append(data)
    return markers


def _load_full_observations_for_order_date(
    src: Path, target: date,
    *, quarantine_paths: list[Path] | None = None,
) -> tuple[list[OrderStatusObservation], list[str]]:
    """전체(하루로 자르지 않은) 관측 로그 원본에서 `order_accepted_at`이
    target 날짜인 관측만 골라 반환합니다(2026-09-18 재재검토 반영,
    지적 3번).

    이 번들은 하루 단위라 지금까지는 "조회일(started_at)로 자른
    슬라이스"만 커버리지 계산에 썼습니다 — 그러면 전날 접수된 주문을
    다음날 조회한 경우(키움 조회 지연/재시도로 실제 발생), 그 관측은
    조회일 슬라이스에는 있지만 주문일(전날) 슬라이스에는 아예 없어서
    **양쪽 날짜 어디의 관측률에도 반영되지 않았습니다**(재현: 9/17
    접수 주문을 9/18에 조회 → 9/17 번들 0%, 9/18 번들엔 고아 관측
    1건). 이제 "주문일별 관측률"은 조회가 언제 일어났는지와 무관하게
    전체 원본 로그를 다시 훑어 `order_accepted_at`이 target 날짜인
    관측을 전부 모읍니다 — 그래서 다음날 조회한 관측도 원래 주문이
    접수된 날짜의 커버리지에 정확히 귀속됩니다.

    이 결과는 번들에 raw로 그대로 저장되지 않고(이미 하루치 raw
    슬라이스가 별도로 존재함) 이 함수를 호출하는 커버리지 계산에만
    쓰이므로, 여기서는 마스킹을 다시 적용하지 않습니다(파일 자체를
    노출하는 경로가 아니라 메모리 내 집계 전용).

    2026-09-21 3차 재검토 반영(지적 1번): `src`뿐 아니라
    `quarantine_paths`(격리된 옛 파일들)도 같은 기준으로 훑습니다 —
    격리 직전까지 쌓여 있던 정상 레코드도 주문일 커버리지에서
    사라지면 안 되기 때문입니다.
    """
    observations: list[OrderStatusObservation] = []
    parse_errors: list[str] = []
    day = target.strftime("%Y-%m-%d")

    def _scan(path: Path) -> None:
        if not path.exists():
            return
        try:
            raw_text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            parse_errors.append(f"{path} 읽기 실패: {exc}")
            return
        for raw_line in raw_text.splitlines():
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError:
                continue  # 손상/불완전 줄 — raw 슬라이스 쪽에서 이미 별도로 표시되므로 여기서는 조용히 건너뜀
            if data.get("__marker__") == "shutdown":
                continue
            accepted_at = str(data.get("order_accepted_at") or "")
            if not accepted_at.startswith(day):
                continue
            filtered = {k: v for k, v in data.items() if k in _OBS_KNOWN_FIELDS}
            try:
                observations.append(OrderStatusObservation(**filtered))
            except TypeError as exc:
                parse_errors.append(f"필드 불일치: {exc}")

    _scan(src)
    for qpath in (quarantine_paths or []):
        _scan(qpath)
    return observations, parse_errors


def _read_running_status(status_path: Path) -> tuple[dict | None, bool]:
    """`OrderStatusObservationRecorder`가 남기는 "현재 실행 상태"
    스냅샷 파일을 읽습니다(2026-09-18 재재검토 반영, 지적 5번).

    반환: (파싱된 딕셔너리 또는 None, 파일이 존재했었는가).

    2026-09-21 3차 재검토 반영(지적 2번, 재현된 버그): 예전엔 "파일이
    없음"과 "파일은 있지만 JSON이 손상됐거나 dict가 아님"을 둘 다
    `None` 하나로 뭉뚱그려 반환했습니다 — 그 결과 `_classify_run_state()`가
    두 경우를 구분하지 못해 상태 파일이 손상됐을 때도 "계측_비활성"
    (관측 기능을 켠 적이 없음)으로 오표시됐습니다. 이제 두 번째 값으로
    "파일이 실제로 존재했는가"를 함께 반환해 호출부가 구분할 수 있게
    합니다. 이 exporter는 읽기 전용이라 손상돼 있어도 복구를 시도하지
    않습니다(원본 기록기 쪽 책임)."""
    if not status_path.exists():
        return None, False
    try:
        data = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, True
    if not isinstance(data, dict):
        return None, True
    return data, True


RUN_STATE_DISABLED = "계측_비활성"
RUN_STATE_CLEAN_SHUTDOWN = "정상_종료"
RUN_STATE_UNCLEAR_SHUTDOWN = "종료_확인_불가"
RUN_STATE_RUNNING = "실행_중"
RUN_STATE_STATUS_UNREADABLE = "상태확인_불가"


def _classify_run_state(
    run_status: dict | None, *, stale_after_sec: float = 120.0,
    status_file_existed: bool = False,
) -> str:
    """실행 상태 스냅샷으로부터 5가지 상태 중 하나를 판정합니다
    (2026-09-18 재재검토 반영, 지적 5번의 "실행 중/정상 종료/종료
    확인 불가/계측 비활성" 구분에 2026-09-21 3차 재검토(지적 2번)로
    "상태확인_불가"를 추가).

    - 스냅샷 자체가 없음(`status_file_existed=False`) → "계측_비활성"
      (관측 기능이 시작된 적 없음 — account_scope_id 미설정 등).
    - 스냅샷 파일은 있었지만 읽지 못함(JSON 손상 등, `run_status is
      None`인데 `status_file_existed=True`) → "상태확인_불가"
      (재현된 버그: 예전엔 이 경우도 "계측_비활성"으로 잘못 표시돼
      "관측 기능을 켠 적이 없다"는 오해를 줬습니다 — 실제로는 기능이
      켜져 있었는데 상태 파일 하나만 손상된 것일 수 있습니다).
    - `clean_shutdown`이 True/False로 확정돼 있음 → 그 값을 그대로
      "정상_종료"/"종료_확인_불가"로 반영(종료 배선을 실제로 탄 뒤의
      결과이므로 가장 신뢰도가 높음).
    - `clean_shutdown`이 아직 None(종료 배선을 타지 않음)인데
      `updated_at`을 파싱할 수 없거나(형식 오류) **미래 시각**이면
      → "상태확인_불가"(재현된 버그: 예전엔 미래 시각도
      `datetime.now() - updated_at`이 음수가 돼 항상 stale_after_sec
      이하로 판정돼 "실행_중"으로 잘못 통과했습니다 — 미래 시각은
      시계 오차든 파일 손상이든 정상 신호로 볼 수 없습니다).
    - `updated_at`이 최근(기본 120초 이내, 미래가 아님)이면 "실행_중"
      — 기록기 작업자 스레드가 폴링마다(기본 5초 간격) 이 파일을
      갱신하므로, 최근 갱신은 "그 스레드가 최근까지 살아있었다"는
      뜻입니다.
    - 그 외(오래된 스냅샷) → "종료_확인_불가"(예: 강제 종료로 종료
      배선 자체를 타지 못한 경우 — 마지막 스냅샷만 남고 더 이상
      갱신되지 않음).
    """
    if run_status is None:
        return RUN_STATE_STATUS_UNREADABLE if status_file_existed else RUN_STATE_DISABLED
    cs = run_status.get("clean_shutdown")
    if cs is True:
        return RUN_STATE_CLEAN_SHUTDOWN
    if cs is False:
        return RUN_STATE_UNCLEAR_SHUTDOWN
    updated_at = run_status.get("updated_at")
    updated_dt = None
    if updated_at:
        try:
            updated_dt = datetime.fromisoformat(str(updated_at))
        except ValueError:
            updated_dt = None
    if updated_dt is None:
        return RUN_STATE_STATUS_UNREADABLE
    age_sec = (datetime.now() - updated_dt).total_seconds()
    if age_sec < 0:
        # 미래 시각 — 시계 오차든 파일 손상이든 정상 신호로 볼 수 없음.
        return RUN_STATE_STATUS_UNREADABLE
    if age_sec <= stale_after_sec:
        return RUN_STATE_RUNNING
    return RUN_STATE_UNCLEAR_SHUTDOWN


def build_order_status_summary(
    target: date,
    query_day_observations: list[OrderStatusObservation],
    order_day_observations: list[OrderStatusObservation],
    trades_rows: list[dict], all_baselines: list[dict],
    shutdown_markers: list[dict] | None = None,
    run_status: dict | None = None,
    status_file_existed: bool = False,
    order_day_extra_count: int = 0,
    aggregation_computed_at: str | None = None,
) -> str:
    """체결조회 증거 저장·커버리지 요약(2026-09-18 지적 2/3/4번 반영,
    2026-09-18 재검토에서 3번/2번 재보완).

    - 고유 주문 기준(조회 횟수 아님)으로 집계합니다.
    - order_date는 journal 스냅샷에서 확인된 `order_accepted_at`
      (실제 주문 접수 시각)을 우선 근거로 씁니다. 2026-09-18 재검토
      지적: "오늘 trades.csv에 같은 order_id가 있으면 오늘 주문"이라는
      이전 추정은, 키움 주문번호가 날짜마다 재사용될 수 있어(전날
      주문 "123"과 오늘 주문 "123"이 실제로는 다른 주문일 수 있음)
      서로 다른 주문을 같은 주문으로 잘못 연결할 위험이 있었습니다 —
      제거했습니다. `order_accepted_at`이 없으면 추정하지 않고
      "미확인"으로 남깁니다.
    - account_scope_id가 여럿 섞여 있으면(계정 여러 개를 같은 로그
      파일에 쓴 경우) 스코프별로 각각 계산합니다 — `compute_coverage()`
      가 각 호출에서 해당 계좌의 관측만 스스로 걸러내므로(2026-09-18
      재검토 반영), 여기서 관측 리스트를 스코프별로 미리 나눌 필요는
      없습니다.

    2026-09-18 재재검토 반영(지적 2/3/5번): 이번 라운드에서 통계를
    두 갈래로 명확히 분리했습니다 —
    1) "조회일 활동 통계"(`query_day_observations`, `started_at` 기준
       하루 슬라이스): 오늘 실제로 조회가 몇 번 실행됐는지.
    2) 계좌별 "주문일별 관측률"(`order_day_observations`,
       `order_accepted_at` 기준으로 전체 로그를 다시 훑은 결과):
       오늘 접수된 주문이 (조회가 언제 일어났든) 실제로 얼마나
       관측됐는지 — 이제 부분 조회 성공(`outcome="partial"`)은
       `query_partial_count`로만 집계되고 관측률 분자에는 포함되지
       않습니다(지적 2번).
    또한 "현재 실행 상태"(`run_status`)를 종료 마커와 별도로 표시해,
    번들이 실행 중인 프로그램에서 생성돼 이번 실행의 종료 마커가
    아직 없는 상태(정상)와 실제 저장 실패를 구분합니다(지적 5번).
    """
    L: list[str] = []
    day = target.strftime("%Y-%m-%d")

    L.append("=" * 58)
    L.append("  체결조회 증거 저장·커버리지 (order_status_coverage)")
    L.append("=" * 58)
    L.append("이 요약은 손익을 계산하지 않습니다 — 무엇을 조회했고 무엇을")
    L.append("저장했으며 무엇이 빠졌는지만 구분합니다.")
    L.append("")
    L.append("※ 아래는 서로 다른 두 통계입니다(2026-09-18 재재검토 반영) — '조회일")
    L.append("  활동 통계'는 오늘 조회가 실행된 건수(조회 시각 기준)이고, 계좌별")
    L.append("  커버리지는 '주문 접수일(order_accepted_at)' 기준입니다. 전일 접수")
    L.append("  주문을 오늘 조회했다면, 그 관측은 오늘이 아니라 원래 주문이 접수된")
    L.append("  날짜의 커버리지에 집계됩니다(집계 기준 시각 = order_accepted_at).")

    query_deduped, query_conflicting_ids = dedupe_by_write_id(query_day_observations)
    L.append("")
    L.append("[ 조회일 활동 통계 — 오늘(조회 시각 기준) ]")
    L.append(f"observation_raw_line_count            = {len(query_day_observations)}")
    L.append(f"observation_unique_write_id_count      = {len(query_deduped)}"
              " (같은 write_id+같은 내용 재시도는 1건으로 집계)")
    if query_conflicting_ids:
        L.append(f"⚠ observation_write_id_conflict_count = {len(query_conflicting_ids)}"
                  " — 같은 write_id인데 내용이 다름(버그 신호, 임의로 하나를 고르지 않음)")
        for wid in query_conflicting_ids[:10]:
            L.append(f"    conflicting write_id: {wid}")
        if len(query_conflicting_ids) > 10:
            L.append(f"    ... 외 {len(query_conflicting_ids) - 10}건")
    else:
        L.append("observation_write_id_conflict_count    = 0")

    L.append("")
    run_state = _classify_run_state(run_status, status_file_existed=status_file_existed)
    L.append(f"observation_run_state(현재 실행 상태)   = {run_state}")
    if run_status:
        L.append(
            f"    restart_id={run_status.get('restart_id', '')}"
            f" updated_at={run_status.get('updated_at', '')}"
        )
        L.append(
            f"    dropped_count={run_status.get('dropped_count')}"
            f" fsync_unconfirmed_count={run_status.get('fsync_unconfirmed_count')}"
            f" queue_full_dropped_count={run_status.get('queue_full_dropped_count')}"
            f" file_healthy={run_status.get('file_healthy')}"
        )
    elif status_file_existed:
        # 2026-09-21 3차 재검토 반영(지적 2번, 재현된 버그): 상태 파일이
        # 존재는 했지만(JSON 손상/dict 아님/updated_at 파싱 불가·미래
        # 시각) 읽을 수 없는 경우 — "계측_비활성"(기능을 켠 적이
        # 없음)과 절대 혼동하면 안 됩니다. 관측 기능은 켜져 있었을 수
        # 있고, 단지 이 진단용 스냅샷 파일 하나만 문제가 있는 것일 수
        # 있습니다.
        L.append("    ⚠ 실행 상태 스냅샷 파일이 있지만 내용을 신뢰할 수 없습니다"
                  "(JSON 손상, dict 아님, 또는 updated_at이 파싱 불가/미래 시각) —")
        L.append("      이 사실만으로 관측 기능이 비활성이라고 판단하면 안 됩니다."
                  " 파일을 직접 열어 확인하세요.")
    else:
        L.append("    ⚠ 실행 상태 스냅샷 파일이 없습니다 — 관측 기능이 비활성"
                  "(account_scope_id 미설정)이었거나 기록기가 아직 한 번도 시작된"
                  " 적이 없습니다.")
    L.append("    ※ '실행_중'은 최근 상태 갱신 시각 기준 추정(하트비트 방식)입니다 —")
    L.append("      프로세스 생존을 다른 수단으로 확정하지는 않습니다.")

    L.append("")
    markers = shutdown_markers or []
    L.append(f"recorder_shutdown_marker_count(이 날짜)  = {len(markers)}"
              " (이 날짜에 실제로 종료된 실행의 마커만 — 다른 날짜의 과거 마커는")
    L.append("  포함하지 않습니다, 재재검토 지적 5번)")
    for m in markers:
        L.append(
            f"    restart_id={m.get('restart_id', '')} clean_shutdown={m.get('clean_shutdown')}"
            f" dropped_count={m.get('dropped_count')} shutdown_at={m.get('shutdown_at', '')}"
        )
    if not markers:
        if run_state == RUN_STATE_RUNNING:
            L.append("    이 날짜에 종료 마커가 없습니다 — 위 실행 상태가 '실행_중'이므로")
            L.append("      정상입니다(아직 종료하지 않았으니 종료 마커가 없는 게 맞습니다).")
        elif run_state == RUN_STATE_STATUS_UNREADABLE:
            L.append("    ⚠ 종료 마커도 없고 위 실행 상태도 확인할 수 없습니다(스냅샷 손상/")
            L.append("      시각 이상) — 정상 종료인지 비정상 종료인지 이 정보만으로는")
            L.append("      판단할 수 없습니다. 상태 스냅샷 파일을 직접 확인하세요.")
        else:
            L.append("    ⚠ 종료 마커가 없습니다 — 이 날짜에 기록기가 정상 종료 배선을 타지")
            L.append("      않고 프로세스가 끝났을 수 있습니다(예: 강제 종료). 큐 포화/쓰기")
            L.append("      실패는 app.log의 [ORDER_STATUS_OBS_QUEUE_FULL]/")
            L.append("      [ORDER_STATUS_OBS_WRITE_FAILED] 태그로도 확인하세요.")

    # order_id 비교는 compute_coverage()/find_all_matching()과 동일하게
    # normalize_order_id()로 정규화합니다(0-padding 차이로 같은 주문이
    # 다른 것으로 갈리지 않도록).
    def _accepted(r: dict) -> bool:
        for k in ("accepted", "order_accepted", "success", "is_success"):
            if k in r:
                return str(r.get(k) or "").strip().lower() in ("true", "1", "y", "yes", "성공", "ok")
        return False

    today_order_ids = {
        normalize_order_id(r.get("order_id") or "")
        for r in trades_rows if _accepted(r) and normalize_order_id(r.get("order_id") or "")
    }

    accepted_orders: set[tuple[str, str, str, str]] = set()
    scope_unresolved = 0
    for r in trades_rows:
        if not _accepted(r):
            continue
        oid = normalize_order_id(r.get("order_id") or "")
        if not oid:
            continue
        ts = str(r.get("timestamp") or r.get("time") or "").strip()
        scope = resolve_scope_for_trade_timestamp(all_baselines, ts) if ts else None
        if scope is None:
            scope_unresolved += 1
            continue
        account_scope_id, env = scope
        if not account_scope_id:
            scope_unresolved += 1
            continue
        accepted_orders.add((account_scope_id, env, day, oid))

    L.append("")
    L.append(f"today_accepted_order_count(trades.csv)  = {len(today_order_ids)}")
    L.append(f"today_accepted_order_scope_resolved      = {len(accepted_orders)}")
    if scope_unresolved:
        L.append(f"⚠ today_accepted_order_scope_unresolved = {scope_unresolved}"
                  " — run_baseline.csv로 계정/환경을 연결하지 못한 주문(연결 실행 없음/"
                  "account_scope_id 미설정) — 아래 커버리지 분모에서 제외됨")

    def _order_date_resolver(obs: OrderStatusObservation) -> str | None:
        # 2026-09-18 재검토 반영(지적 3번): "오늘 trades.csv에 같은
        # order_id가 있으면 오늘 주문"이라는 추정을 제거했습니다 —
        # 키움 주문번호가 날짜마다 재사용될 수 있어(전날 "123"과 오늘
        # "123"이 다른 주문일 수 있음) 서로 다른 주문을 섞을 위험이
        # 있었습니다. journal에서 확인된 실제 접수 시각만 근거로 씁니다.
        if not obs.order_accepted_at:
            return None
        candidate = str(obs.order_accepted_at)[:10]
        return candidate if len(candidate) == 10 and candidate[4] == "-" and candidate[7] == "-" else None

    order_day_deduped, order_day_conflicting_ids = dedupe_by_write_id(order_day_observations)
    day_compact = target.strftime("%Y%m%d")
    L.append("")
    L.append("[ 계좌별 커버리지 — 주문 접수일(order_accepted_at) 기준 ]")
    L.append(f"order_day_observation_count           = {len(order_day_observations)}"
              " (조회가 언제 실행됐든, 이 날짜에 접수된 주문에 대한 관측 전체)")
    # 2026-09-21 3차 재검토 반영(지적 3번, 재현된 버그): 위 카운트에는
    # 익일(다른 날짜)에 조회된 관측도 섞여 있는데, 그 관측은 "조회일
    # raw 슬라이스"(started_at 기준)에는 나타나지 않아 번들만으로는
    # 근거를 재확인할 수 없었습니다. 이제 그 몫만 별도 raw 파일(아래
    # order_day_extra_count>0일 때 build()가 생성)로 함께 포함하고,
    # 여기서는 그 사실과 건수, 그리고 이 집계가 전체 로그를 다시
    # 훑은 시각을 함께 남깁니다(로그가 그 이후 더 늘어나면 다음 번들의
    # 수치가 달라질 수 있음을 명시).
    L.append(
        f"  ⤷ 이 중 조회일 raw 슬라이스에 없는(다른 날짜에 조회된) 관측 = {order_day_extra_count}건"
        + (
            f" — raw/order_status_observations_orderday_extra_{day_compact}.jsonl 참고"
            if order_day_extra_count else ""
        )
    )
    L.append(
        f"  ⤷ 집계 기준 시각(전체 로그 재스캔 시각) = {aggregation_computed_at or '알 수 없음'}"
        " (이 시각 이후 로그가 추가되면 다음 번들의 수치가 달라질 수 있음)"
    )
    if order_day_conflicting_ids:
        L.append(f"⚠ order_day_write_id_conflict_count   = {len(order_day_conflicting_ids)}"
                  " — 같은 write_id인데 내용이 다름(버그 신호)")

    scopes = sorted({acc for acc, _env, _d, _oid in accepted_orders} | {
        obs.account_scope_id for obs in order_day_deduped if obs.account_scope_id
    })
    if not scopes:
        L.append("")
        L.append("계정/환경 연결이 가능한 오늘 접수 주문도, 관측 기록도 없어 커버리지를"
                  " 계산하지 않습니다(run_baseline.csv 참고 — 미설정이면 위"
                  " scope_unresolved에 반영됨).")
        return "\n".join(L)

    for scope_id in scopes:
        cov = compute_coverage(
            account_scope_id=scope_id, accepted_orders=accepted_orders,
            observations=order_day_deduped, order_date_resolver=_order_date_resolver,
        )
        L.append("")
        L.append(f"[ account_scope_id = {scope_id} ]")
        if cov["status"] == "계측_비활성":
            L.append(f"  status = 계측_비활성 ({cov.get('reason', '')})")
            continue
        L.append(f"  total_accepted_orders          = {cov['total_accepted_orders']}")
        L.append(f"  observed_unique_orders         = {cov['observed_unique_orders']}")
        L.append(f"  filled_unique_orders           = {cov['filled_unique_orders']}")
        L.append(f"  priced_unique_orders           = {cov['priced_unique_orders']}")

        def _pct(v):
            return "N/A" if v is None else f"{v * 100:.0f}%"

        L.append(f"  order_observation_rate(주문 관측률)   = {_pct(cov['order_observation_rate'])}")
        L.append(f"  status_confirmation_rate(상태 확인률) = {_pct(cov['status_confirmation_rate'])}")
        L.append(f"  price_capture_rate(가격 확보율)       = {_pct(cov['price_capture_rate'])}")
        L.append(f"  query_attempt_count             = {cov['query_attempt_count']}")
        L.append(f"  query_success_count             = {cov['query_success_count']}")
        L.append(f"  query_partial_count              = {cov['query_partial_count']}"
                  " (oso/cntr 중 일부만 성공 — 완전한 조회 성공으로 집계하지 않음,"
                  " 재재검토 지적 2번)")
        L.append(f"  query_failed_count              = {cov['query_failed_count']}")
        L.append(f"  unresolved_order_date_count      = {cov['unresolved_order_date_count']}"
                  " (journal에서 접수 시각을 확인하지 못한 관측 — 저널이 이미 삭제된 경우 등)")
        L.append(f"  orphan_observation_count         = {cov['orphan_observation_count']}"
                  " (order_date는 확인됐지만 accepted_orders에 없는 조회 — trades.csv에")
        L.append("    해당 주문이 없거나 계정/환경 연결이 안 된 경우")

    L.append("")
    L.append("※ 4번째 지표(조회·저장 품질)는 write_id 재시도/충돌 집계, 위 종료 마커,")
    L.append("  app.log의 [ORDER_STATUS_OBS_QUEUE_FULL]/[ORDER_STATUS_OBS_WRITE_FAILED]/")
    L.append("  [ORDER_STATUS_OBS_FSYNC_FAILED] 태그를 함께 참고하세요 — 큐에서 버려진")
    L.append("  레코드는 애초에 이 raw 파일에 없으므로 이 exporter가 사후에 재구성할 수")
    L.append("  없습니다.")
    L.append("=" * 58)
    return "\n".join(L)


# ── 수집 품질 메타데이터 ────────────────────────────────────────
def _lock_owner_alive(lock_path: Path) -> bool:
    """락 소유 프로세스가 아직 살아 있는지 확인합니다.

    2026-08-06 (1I.4, GPT 지적 P2): 30분이 지나면 소유 프로세스가
    살아 있어도 새 프로세스가 락을 제거하고 진입할 수 있었음.
    PID가 살아 있으면 stale로 보지 않는다(판정 불가 시에는 보수적
    으로 "살아 있다"고 보아 회수하지 않음).
    """
    try:
        text = lock_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return False
    m = re.search(r"pid=(\d+)", text)
    if not m:
        return False          # 형식을 모르면 회수 허용(구버전 락)
    try:
        os.kill(int(m.group(1)), 0)
        return True           # 살아 있음 → 회수 금지
    except ProcessLookupError:
        return False
    except PermissionError:
        return True           # 남의 소유 프로세스가 실재 → 회수 금지
    except OSError:
        return True


def _identity(r: dict, idx: int) -> tuple:
    """수락된 주문의 고유 식별자.

    2026-08-06 (1I.3, GPT 지적 P1-3): order_id 집합 크기로 세면
    ID가 없는 주문이 통째로 사라졌음(accepted 10건 중 8건만 ID →
    8건으로 집계). ID가 없으면 행 고유값으로 대체하고, 누락 건수는
    별도 경고 필드로 남긴다.
    """
    oid = str(r.get("order_id") or "").strip()
    if oid:
        return ("order_id", oid)
    return ("fallback", str(r.get("timestamp", "")), str(r.get("symbol", "")),
            str(r.get("side", "")), str(r.get("quantity", "")),
            str(r.get("price", "")), idx)


def _first_last_ts(rows: list[dict], col: str) -> tuple[str, str]:
    vals = [str(r.get(col) or "") for r in rows if str(r.get(col) or "")]
    return (vals[0], vals[-1]) if vals else ("", "")


def build_collection_quality(target: date, log_lines: list[str],
                             counts: dict[str, int],
                             shadow_rows: list[dict],
                             trades_rows: list[dict],
                             signal_rows: list[dict] | None = None) -> str:
    """분석자가 그날 데이터를 어디까지 믿어도 되는지 판정합니다.

    2026-08-06 (1I.1): 8/6처럼 장중 재시작이 여러 번 있었던 날은
    session VWAP 관측이 통째로 무의미해지는데, 리포트만 봐서는
    그걸 알 수 없었음. 판정은 **보수적으로** — 조건을 전부
    만족할 때만 COMPLETE.
    """
    signal_rows = signal_rows or []
    L: list[str] = []
    day = target.strftime("%Y-%m-%d")

    starts = sum(1 for l in log_lines if "watcher.start() 진입" in l)
    ws_connect = sum(1 for l in log_lines if "[WS]" in l and "연결 성공" in l)
    # 2026-08-06 (1I.2, GPT 지적 P2): "재연결" 부분일치로 세면
    # 정상 기동 로그 "[WS] start() 진입 — 재연결 루프 시작"까지
    # 재연결로 집계됨. 실제 재시도만 세도록 조건을 좁힘.
    ws_reconnect = sum(1 for l in log_lines
                       if "[WS]" in l and "연결 끊김:" in l and "초 후 재연결" in l)
    truncate_lines = [l for l in log_lines if "[COND_TRUNCATE]" in l]
    max_trunc = 0
    for l in truncate_lines:
        m = re.search(r"조건검색 종목 (\d+)개가 잘렸", l)
        if m:
            max_trunc = max(max_trunc, int(m.group(1)))

    ready_true = sum(1 for l in log_lines if "[SESSION_SHADOW]" in l and "ready=True" in l)
    ready_false = sum(1 for l in log_lines if "[SESSION_SHADOW]" in l and "ready=False" in l)
    ready_total = ready_true + ready_false

    ts_vals = [l[:19] for l in log_lines if l.startswith(day)]
    first_ts = ts_vals[0] if ts_vals else ""
    last_ts = ts_vals[-1] if ts_vals else ""

    sh_first, sh_last = _first_last_ts(shadow_rows, "timestamp")
    sig_first, sig_last = _first_last_ts(signal_rows, "timestamp")
    attempts = sum(1 for r in shadow_rows if str(r.get("order_attempted", "")).lower() == "true")
    sh_accepted_rows = [r for r in shadow_rows
                        if str(r.get("order_accepted", "")).lower() == "true"]
    sh_accepted = len(sh_accepted_rows)
    sh_missing_id = sum(1 for r in sh_accepted_rows if not str(r.get("order_id") or "").strip())
    sh_id_complete = all(str(r.get("order_id") or "").strip() for r in sh_accepted_rows)
    sh_accepted_uniq = (len({_identity(r, i) for i, r in enumerate(sh_accepted_rows)})
                        if sh_id_complete else None)
    # 2026-08-06 (1I.2, GPT 지적 P1-2): side=BUY만 세면 브로커가
    # 거부한 주문까지 "실제 매수"로 집계돼 coverage가 왜곡됨.
    # accepted=True인 주문만 세고, order_id로 유니크 처리.
    def _is_buy(r: dict) -> bool:
        return any(str(r.get(k, "")).upper() in ("BUY", "매수")
                   for k in ("side", "type", "구분", "order_type"))

    # 2026-08-06 (1I.3, GPT 지적 P1-2): accepted 컬럼이 없으면 예전엔
    # 모든 BUY를 수락으로 간주했는데, export의 원칙이 fail-closed이므로
    # **모르는 주문을 체결로 추정하면 안 됨**. None을 돌려 N/A 처리하고
    # SCHEMA_WARNING을 남긴다.
    # 2026-08-06 (1I.4, GPT 지적 P1): 빈 문자열은 "명시적 거부"가
    # 아니라 "미상"이므로 False가 아니라 None으로 처리해야 함.
    # 알 수 없는 값도 마찬가지.
    _TRUE = ("true", "1", "y", "yes", "성공", "ok")
    _FALSE = ("false", "0", "n", "no", "실패", "거부")

    def _accepted_state(r: dict) -> bool | None:
        for k in ("accepted", "order_accepted", "success", "is_success"):
            if k in r:
                raw = str(r.get(k) or "").strip().lower()
                if raw in _TRUE:
                    return True
                if raw in _FALSE:
                    return False
                return None
        return None

    # 2026-08-06 (1I.3, GPT 지적 P1-3): order_id가 일부에만 있으면
    # ID 집합 크기로 세다가 ID 없는 주문이 통째로 사라졌음
    # (accepted 10건 중 8건만 ID → 8건으로 집계). ID가 없으면
    # 행 고유값으로 대체하고, 누락 건수를 별도 경고로 남긴다.
    buy_attempts = [r for r in trades_rows if _is_buy(r)]
    accepted_states = [_accepted_state(r) for r in buy_attempts]
    # fail-closed — 한 건이라도 판정 불가면 스키마를 신뢰하지 않는다.
    accepted_state_missing = sum(1 for st in accepted_states if st is None)
    accepted_schema_ok = not buy_attempts or accepted_state_missing == 0
    buy_accepted = [r for r, st in zip(buy_attempts, accepted_states) if st is True]
    buy_missing_id = sum(1 for r in buy_accepted if not str(r.get("order_id") or "").strip())
    # 2026-08-06 (1I.4, GPT 지적 P1): fallback에 idx가 들어가 완전히
    # 동일한 중복 행도 서로 다른 주문으로 세어졌음 —
    # "unique_accepted_buy_order_count"라는 이름과 맞지 않음.
    # 분석 메타데이터에서는 보수적 방식을 택해, order_id가 하나라도
    # 없으면 unique 수와 coverage를 N/A로 처리하고 행 수만 표시한다.
    buy_id_complete = all(str(r.get("order_id") or "").strip() for r in buy_accepted)
    buys = len({_identity(r, i) for i, r in enumerate(buy_accepted)}) if buy_id_complete else None
    reliable = sum(1 for r in shadow_rows
                   if str(r.get("condition_source_reliable", "")).lower() == "true")

    # 보수적 판정 — 조건을 전부 만족할 때만 COMPLETE
    # 2026-08-06 (1I.2, GPT 지적 P1-1): 1I.1은 entry_quality_shadow의
    # 첫 기록으로 판정했는데, 이 파일은 **legacy BUY 후보가 있을 때만**
    # 기록되므로 09:00부터 정상 수집됐어도 첫 후보가 10:30이면
    # PARTIAL로 오판함. 수집 범위 판정은 매 폴링마다 기록되는
    # signal_log.csv를 기준으로 해야 정확함. shadow 첫·마지막 시각은
    # 별도 coverage 정보로만 남김.
    open_ok = bool(sig_first) and sig_first[11:16] <= "09:02"
    close_ok = bool(sig_last) and sig_last[11:16] >= "15:15"
    # 2026-08-06 (1I.4, GPT 지적 P1, 재현 확인): 기동 로그가 0건이어도
    # signal_log 범위만 맞으면 COMPLETE가 됐음. 보고한 판정 기준은
    # "프로세스 재시작 없음"이 아니라 "정확히 1회 기동"이어야 하고,
    # 로그가 아예 없는 경우는 판정 근거 자체가 없으므로 따로 구분한다.
    if starts == 0:
        status = "UNKNOWN_START_PARTIAL"
    elif starts > 1:
        status = "RESTARTED_PARTIAL"
    elif open_ok and close_ok:
        status = "COMPLETE"
    else:
        status = "PARTIAL"

    # 2026-08-06 (1I.3, GPT 지적 P1-1): 예전엔 [SESSION_SHADOW] 로그
    # 이벤트 수(ready_true)로 판정했는데, session VWAP 게이트 성과
    # 분석의 실제 모집단은 **entry_quality_shadow의 legacy BUY 후보
    # 중 ready=True**임. HOLD 평가에서는 ready=True가 있어도 BUY
    # 후보에서 0건이면 분석 표본이 없으므로 AVAILABLE이라 하면 안 됨.
    # 로그 이벤트 기준 상태는 진단용으로 따로 남김.
    session_state_status = "COLLECTING" if ready_true > 0 else "NO_COMPLETE_SESSION"
    sh_ready = sum(1 for r in shadow_rows
                   if str(r.get("session_metrics_ready", "")).lower() == "true")
    session_interp = "AVAILABLE" if sh_ready > 0 else "INVALID_FOR_THIS_DAY"
    rolling_interp = ("AVAILABLE" if status == "COMPLETE"
                      else "AVAILABLE_WITH_COVERAGE_LIMIT")

    def add(k: str, v) -> None:
        L.append(f"{k:38s}= {v}")

    L.append("=" * 58)
    L.append("  수집 품질 메타데이터 (collection_quality)")
    L.append("=" * 58)
    add("trade_date", day)
    add("first_log_timestamp", first_ts or "N/A")
    add("last_log_timestamp", last_ts or "N/A")
    add("process_start_count", starts)
    add("websocket_connect_count", ws_connect)
    add("websocket_reconnect_count", ws_reconnect)
    add("collection_status", status)
    add("full_day_collection", status == "COMPLETE")
    L.append("")
    add("signal_log_rows", counts.get("signal_log.csv", 0))
    add("entry_quality_shadow_rows", counts.get("entry_quality_shadow.csv", 0))
    add("signal_collection_first_ts", sig_first or "N/A")
    add("signal_collection_last_ts", sig_last or "N/A")
    add("shadow_first_candidate_ts", sh_first or "N/A")
    add("shadow_last_candidate_ts", sh_last or "N/A")
    L.append("")
    add("buy_order_attempt_count", len(buy_attempts) if trades_rows else "N/A(trades.csv 없음)")
    if not trades_rows:
        add("accepted_buy_order_count", "N/A")
        add("unique_accepted_buy_order_count", "N/A")
    elif not accepted_schema_ok:
        add("accepted_buy_order_count", "N/A(SCHEMA_WARNING)")
        add("unique_accepted_buy_order_count", "N/A(SCHEMA_WARNING)")
        add("trades_accepted_schema", "SCHEMA_WARNING: accepted 컬럼 없음 — 수락 여부 판정 불가")
    else:
        add("accepted_buy_order_count", len(buy_accepted))
        add("unique_accepted_buy_order_count",
            buys if buys is not None else "N/A(order_id 누락 있음)")
    add("accepted_buy_missing_order_id_count", buy_missing_id if trades_rows else "N/A")
    add("buy_accepted_state_missing_count", accepted_state_missing if trades_rows else "N/A")
    add("shadow_order_attempt_count", attempts)
    add("shadow_order_accepted_count", sh_accepted)
    add("shadow_unique_accepted_order_count",
        sh_accepted_uniq if sh_accepted_uniq is not None else "N/A(order_id 누락 있음)")
    add("shadow_accepted_missing_order_id_count", sh_missing_id)
    # 같은 개념끼리 비교 — shadow accepted ÷ trades accepted BUY
    if trades_rows and accepted_schema_ok and buys and sh_accepted_uniq is not None:
        add("shadow_to_actual_buy_coverage",
            f"{sh_accepted_uniq}/{buys} ({sh_accepted_uniq / buys * 100:.0f}%)")
    else:
        add("shadow_to_actual_buy_coverage", "N/A")
    L.append("")
    # 2026-08-06 (1I.2, GPT 지적 P2): ready=True/False는 로그 이벤트
    # 행 비율이며 같은 종목이 매분 반복되므로 독립 표본 비율이 아님.
    # 명칭을 명확히 하고, shadow 후보 기준 비율을 따로 제공.
    add("session_ready_log_event_count", ready_true)
    add("session_not_ready_log_event_count", ready_false)
    add("session_ready_log_event_ratio",
        f"{ready_true}/{ready_total} ({ready_true / ready_total * 100:.1f}%)"
        if ready_total else "N/A")
    add("shadow_candidate_session_ready_count", sh_ready)
    add("shadow_candidate_session_ready_ratio",
        f"{sh_ready}/{len(shadow_rows)} ({sh_ready / len(shadow_rows) * 100:.1f}%)"
        if shadow_rows else "N/A")
    add("condition_source_reliable_true_count", reliable)
    add("condition_source_reliable_ratio",
        f"{reliable}/{len(shadow_rows)} ({reliable / len(shadow_rows) * 100:.1f}%)"
        if shadow_rows else "N/A")
    L.append("")
    add("cond_truncate_event_count", len(truncate_lines))
    add("max_truncated_condition_count", max_trunc)
    L.append("")
    add("session_state_collection_status", session_state_status)
    add("session_gate_interpretation", session_interp)
    add("rolling_gate_interpretation", rolling_interp)
    L.append("")
    L.append("판정 기준(보수적): signal_log 첫 기록이 09:00~09:02이고, 마지막 기록이 15:15 이후이며,")
    L.append("장중 프로세스 재시작이 없을 때만 COMPLETE. shadow 첫 기록은 legacy BUY")
    L.append("후보가 있어야 생기므로 수집 완전성 판정에 쓰지 않습니다.")
    L.append("그 외에는 PARTIAL 또는 RESTARTED_PARTIAL. session 게이트는")
    L.append("ready=True 행이 하나도 없으면 성과 해석이 불가능하므로")
    L.append("INVALID_FOR_THIS_DAY로 표시합니다.")
    L.append("=" * 58)
    return "\n".join(L)


# ── 번들 생성 ───────────────────────────────────────────────────
def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as f:
        return list(csv.DictReader(f))


def build(
    target: date,
    *,
    quiet: bool = False,
    settings_path: str | Path = "config/settings.yaml",
    obs_log_path: str | Path | None = None,
) -> Path | None:
    """번들을 원자적으로 생성합니다. 락 획득 실패 시 None 반환.

    `settings_path`/`obs_log_path`(2026-09-18 재재검토 반영, 지적
    4번): 관측 로그 경로를 실제 설정에서 읽거나(기본 동작 —
    `resolve_order_status_observation_log_path(settings_path)`),
    `obs_log_path`로 직접 지정할 수 있습니다. 후자는 테스트나
    수동 실행에서 특정 경로를 강제할 때 씁니다.
    """
    day_compact = target.strftime("%Y%m%d")
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    final_path = EXPORTS_DIR / f"bundle_{day_compact}.zip"
    lock_path = EXPORTS_DIR / f"bundle_{day_compact}.lock"

    # ── 동시 실행 보호 ──
    # 15:20 자동 실행과 수동 실행이 겹치면 서로의 작업물을 지울 수
    # 있으므로, O_EXCL로 락을 잡고 실패하면 기존 실행을 건드리지
    # 않고 조용히 물러남(불완전 ZIP을 만들지 않음).
    # 2026-08-06 (1I.2, GPT 지적 P2): 프로세스가 강제 종료되면 락이
    # 남아 이후 export가 영구히 거부됨. 정상 export는 수 분 이상
    # 걸리지 않으므로 30분을 stale 기준으로 보고 회수한다.
    # 2026-08-06 (1I.3, GPT 지적 P2): 락에 고유 토큰을 기록하고
    # 해제 시 자신이 만든 것일 때만 삭제 — 30분 초과 프로세스 A의
    # 락을 B가 회수한 뒤, A가 finally에서 B의 락까지 지워 C가 동시
    # 진입하는 경계 상황을 막기 위함.
    lock_token = f"pid={os.getpid()} created={datetime.now().isoformat()} nonce={time.time_ns()}"
    if lock_path.exists():
        age = time.time() - lock_path.stat().st_mtime
        if age > STALE_LOCK_SECONDS and not _lock_owner_alive(lock_path):
            if not quiet:
                print(f"⚠ stale lock 감지({age / 60:.0f}분 경과) — 제거 후 재시도합니다.")
            # 회수는 quiet 여부와 무관하게 수행
            lock_path.unlink(missing_ok=True)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, lock_token.encode())
        os.close(fd)
    except FileExistsError:
        if not quiet:
            print(f"⚠ 다른 export가 진행 중입니다({lock_path}) — 이번 실행은 건너뜁니다.")
        return None

    work = Path(tempfile.mkdtemp(prefix=f"bundle_{day_compact}_", dir=str(EXPORTS_DIR)))
    # 2026-08-06 (1I.4, GPT 지적 P2): 고정 tmp 이름은 두 실행이
    # 겹칠 때 서로의 임시 파일을 덮어쓸 수 있으므로 nonce를 붙임.
    tmp_zip = EXPORTS_DIR / f"bundle_{day_compact}.{os.getpid()}-{time.time_ns()}.zip.tmp"
    try:
        manifest: list[str] = []
        counts: dict[str, int] = {}
        report_files: list[str] = []
        raw_files: list[str] = []

        manifest.append("=" * 58)
        manifest.append(f"  분석용 일일 번들  {target.strftime('%Y-%m-%d')}")
        manifest.append(f"  생성 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        manifest.append("=" * 58)
        manifest.append("")
        manifest.append("[ RAW — 검증용 원본 (해당 날짜 행만 추출) ]")

        for name, ts_cols in CSV_SOURCES:
            src = LOGS_DIR / name
            dst = work / f"{name[:-4]}_{day_compact}.csv"
            if not src.exists():
                manifest.append(f"  {name:30s} | MISSING | 원본 없음 | excluded")
                continue
            try:
                total, kept = slice_csv(src, dst, target, ts_cols)
            except SchemaError as exc:
                # fail-closed — 전체 복사하지 않고 제외
                if dst.exists():
                    dst.unlink()
                manifest.append(f"  {name:30s} | SCHEMA_ERROR | {exc} | excluded")
                continue
            counts[name] = kept
            raw_files.append(dst.name)
            size_kb = dst.stat().st_size / 1024
            manifest.append(f"  {name:30s} | OK | {kept:,}행 / 전체 {total:,}행 | {size_kb:,.0f} KB")

        # 2026-09-11 (B01 보완, 민우님/GPT 지적): run_baseline.csv가
        # daily bundle에 전혀 실리지 않아 "이 날짜 거래가 어떤 실행/
        # 설정으로 나왔는지"를 번들만으로 알 수 없었음. 당일 시작한
        # 실행 + 전날(혹은 그 이전)에 시작해 이 날짜까지 이어지고
        # 있을 수 있는 가장 최근 실행 1건("이월 추정")을 포함한다.
        # 이 조인은 어디까지나 최선 추정이며(run_baseline.py의
        # resolve_run_id_for_timestamp() docstring 참고), 이 섹션은
        # 그 한계를 번들 안에서 분석자가 바로 알 수 있게 명시한다.
        manifest.append("")
        manifest.append("[ RAW — 실행 기준선 (run_baseline.csv, B01) ]")
        baseline_src = LOGS_DIR / "run_baseline.csv"
        # 2026-09-18: 체결조회 증거 커버리지 요약(아래)도 이 조인
        # 결과가 필요하므로, run_baseline.csv가 없는 경우를 포함해
        # 항상 정의해 둡니다(빈 리스트면 커버리지 쪽에서 scope_unresolved로
        # 자연히 반영됨 — 별도 분기 불필요).
        all_baselines: list[dict] = []
        if not baseline_src.exists():
            manifest.append(f"  {'run_baseline.csv':30s} | MISSING | 원본 없음 | excluded")
            manifest.append("  ⚠ 이 날짜의 거래/신호를 실행(run_id)에 연결할 근거가 없습니다.")
        else:
            all_baselines = load_run_baselines(str(baseline_src))
            day_start = datetime.combine(target, datetime.min.time(), tzinfo=KST_TZ)
            day_end = datetime.combine(target, datetime.max.time(), tzinfo=KST_TZ)

            parsed: list[tuple[dict, object]] = []
            unparseable = 0
            for b in all_baselines:
                dt = parse_run_timestamp(b.get("started_at"))
                if dt is None:
                    unparseable += 1
                else:
                    parsed.append((b, dt))

            same_day = [(b, dt) for b, dt in parsed if day_start <= dt <= day_end]
            before_day = [(b, dt) for b, dt in parsed if dt < day_start]
            # 재부팅 스케줄이 없다는 전제 하의 best-effort — 자정을
            # 넘겨 재시작 없이 계속 도는 실행이 있다면 그 실행이 이
            # 날짜 새벽 거래를 만들었을 수 있음. 확정 아님.
            carried_over = max(before_day, key=lambda pair: pair[1]) if before_day else None

            included = [b for b, _dt in same_day]
            if carried_over is not None:
                included.append(carried_over[0])

            if not included:
                manifest.append(f"  {'run_baseline.csv':30s} | OK | 이 날짜에 해당하는 실행 없음 | excluded")
                # 2026-09-11 (B01 v2 재검토, GPT 재지적): 연결 가능한
                # 실행이 하나도 없을 때(run_baseline.csv가 비어있거나
                # 모든 started_at이 파싱 불가) 이 날짜에 실제 거래/
                # 신호가 있으면 "실행 없음"이 아니라 "연결 불가"임을
                # 명시해야 함 — 이전 구현은 이 경우 UNRESOLVED 표시
                # 자체를 건너뛰어(아래 earliest_included_dt 계산이
                # included가 비면 None이 되므로) 조용히 "정상, 그냥
                # 실행이 없었다"로 읽힐 위험이 있었음.
                for label, fname in (
                    ("signal_log", f"signal_log_{day_compact}.csv"),
                    ("trades", f"trades_{day_compact}.csv"),
                ):
                    rows_probe = _read_csv(work / fname)
                    if rows_probe:
                        manifest.append(
                            f"    ⚠ {label}에 이 날짜 행이 {len(rows_probe)}건 있지만"
                            " 연결 가능한 실행 기록이 전혀 없습니다 — 이 날짜 전체가"
                            " run_id로 연결할 근거가 없습니다(UNRESOLVED)."
                        )
            else:
                dst = work / f"run_baseline_{day_compact}.csv"
                with dst.open("w", newline="", encoding="utf-8") as fp:
                    writer = csv.DictWriter(fp, fieldnames=RUN_BASELINE_FIELDS)
                    writer.writeheader()
                    for b in included:
                        writer.writerow({k: b.get(k, "") for k in RUN_BASELINE_FIELDS})
                raw_files.append(dst.name)
                manifest.append(
                    f"  {'run_baseline.csv':30s} | OK | 당일 시작 {len(same_day)}건"
                    f" + 전날 이월 추정 {1 if carried_over else 0}건"
                    f" | {dst.stat().st_size / 1024:,.1f} KB"
                )
                if carried_over is not None:
                    manifest.append(
                        f"    ⚠ 이월 추정 실행(run_id={carried_over[0].get('run_id', '')})은"
                        " 확정된 연결이 아닙니다(best-effort, 재시작 로그 없음을 가정)."
                    )

                # config snapshot (redacted) — 존재하면 함께 포함.
                # 없어도(예: 이 실행 시점엔 아직 해당 기능이 없었음)
                # 실패로 취급하지 않고 조용히 생략한다.
                config_dir = LOGS_DIR / "run_baseline_configs"
                snap_included = 0
                for b in included:
                    run_id = b.get("run_id", "")
                    src = config_dir / f"{run_id}.json"
                    if src.exists():
                        shutil.copy2(src, work / f"run_baseline_config_{run_id}.json")
                        raw_files.append(f"run_baseline_config_{run_id}.json")
                        snap_included += 1
                manifest.append(
                    f"    config snapshot(redacted) 포함: {snap_included}/{len(included)}건"
                )

            if unparseable:
                manifest.append(f"    ⚠ started_at 파싱 실패 {unparseable}건 — 조인에서 제외")

            # "구분할 수 있음" 요구사항 — 이 날짜의 거래/신호 첫 기록이
            # 포함된 실행의 시작 시각보다 앞서면(=조인 근거 자체가
            # 없으면) 자동 보정하지 않고 명시적으로 표시만 한다.
            earliest_included_dt = min(
                (dt for _b, dt in same_day + ([carried_over] if carried_over else [])),
                default=None,
            )
            if earliest_included_dt is not None:
                for label, fname in (
                    ("signal_log", f"signal_log_{day_compact}.csv"),
                    ("trades", f"trades_{day_compact}.csv"),
                ):
                    rows_probe = _read_csv(work / fname)
                    first_ts, _last_ts = _first_last_ts(rows_probe, "timestamp")
                    if not first_ts:
                        continue
                    first_dt = parse_run_timestamp(first_ts)
                    if first_dt is not None and first_dt < earliest_included_dt:
                        manifest.append(
                            f"    ⚠ {label}의 첫 기록({first_ts})이 포함된 실행의 시작 시각보다"
                            " 앞섭니다 — 이 구간은 run_id로 연결할 근거가 없습니다(UNRESOLVED)."
                        )
        manifest.append("  ※ run_id 연결은 시간범위 최선 추정입니다(암호학적 확정 아님) —")
        manifest.append("    기록 실패 여부는 app.log의 [RUN_BASELINE] 태그 라인을 참고하세요.")

        # 2026-09-18 (우선순위1 1차, 체결조회 증거 저장·커버리지):
        # order_status_observations.jsonl을 날짜로 잘라 raw로 포함.
        # trades.csv는 이미 위에서 이 날짜로 슬라이스돼 work에 있으므로
        # 그걸 그대로 재사용(중복 조회 없음).
        manifest.append("")
        manifest.append("[ RAW — 체결조회 증거 관측 (order_status_observations.jsonl) ]")
        if obs_log_path is not None:
            resolved_obs_log = Path(obs_log_path)
            manifest.append(f"  observation_log_source = 명시적 지정(obs_log_path) → {resolved_obs_log}")
        else:
            resolved_obs_log, _loaded_from_settings = resolve_order_status_observation_log_path(settings_path)
            if _loaded_from_settings:
                manifest.append(
                    f"  observation_log_source = config.settings.load_settings({settings_path!r})"
                    f" → {resolved_obs_log}"
                )
            else:
                manifest.append(
                    f"  observation_log_source = 폴백(설정 로드 실패 또는 파일 없음, 지적 4번 대응)"
                    f" → {resolved_obs_log}"
                )
        obs_src = resolved_obs_log
        obs_dst = work / f"order_status_observations_{day_compact}.jsonl"
        obs_records: list[OrderStatusObservation] = []
        obs_shutdown_markers: list[dict] = []
        # 2026-09-21 3차 재검토 반영(지적 1번): 격리(_quarantine_and_rotate_file())로
        # 옆으로 치워진 옛 파일이 있으면, 격리 직전까지 쌓여있던 정상
        # 레코드도 이 번들에 포함해야 합니다 — obs_src(격리 후 새로
        # 시작된 파일)만 읽으면 그 레코드들은 영원히 사라집니다.
        quarantined_obs_paths = find_quarantined_observation_files(obs_src)
        if not obs_src.exists() and not quarantined_obs_paths:
            manifest.append(f"  {'order_status_observations.jsonl':30s} | MISSING | 원본 없음 | excluded")
            manifest.append("  ⚠ 관측 기능이 비활성(account_scope_id 미설정)이었거나 아직 기록이 없습니다.")
        else:
            (
                obs_total, obs_kept, obs_trailing_bad, obs_mid_bad, obs_masked,
                obs_q_files, obs_q_recovered, obs_q_skipped,
            ) = slice_jsonl_observations(
                obs_src, obs_dst, target, quarantine_paths=quarantined_obs_paths,
            )
            raw_files.append(obs_dst.name)
            if not obs_src.exists():
                manifest.append(
                    f"  {'order_status_observations.jsonl':30s} | MISSING(원본) | 격리된 파일에서만 복구"
                )
            else:
                manifest.append(
                    f"  {'order_status_observations.jsonl':30s} | OK | {obs_kept:,}줄 / 전체 {obs_total:,}줄"
                    f" | {obs_dst.stat().st_size / 1024:,.1f} KB"
                )
            if obs_trailing_bad:
                manifest.append(
                    f"    ⚠ 말미 불완전 줄 {obs_trailing_bad}건 제외(강제종료 추정 — 정상적인 상황)"
                )
            if obs_mid_bad:
                manifest.append(
                    f"    ⚠ 말미가 아닌 위치의 손상 줄 {obs_mid_bad}건 발견 — 파일 자체 손상"
                    " 가능성(조용히 넘어가지 않고 표시만 하고 계속 진행)"
                )
            if obs_masked:
                manifest.append(f"    민감정보 마스킹 재적용: {obs_masked}줄에서 값 변경됨")
            if obs_q_files:
                manifest.append(
                    f"    ⚠ 격리된 손상 파일 {obs_q_files}건 발견 — 격리 직전까지의 정상 레코드"
                    f" {obs_q_recovered}건을 이 raw에 포함했습니다(2026-09-21 3차 재검토 반영,"
                    " 지적 1번 — 예전엔 격리된 파일의 정상 레코드가 집계·번들에서 그냥"
                    " 사라졌습니다)"
                )
                if obs_q_skipped:
                    manifest.append(f"      격리 파일 내 손상 줄 {obs_q_skipped}건은 계속 제외됨")
            obs_records, obs_parse_errors = _parse_observations_for_coverage(obs_dst)
            if obs_parse_errors:
                manifest.append(
                    f"    ⚠ 커버리지 계산용 레코드 복원 실패 {len(obs_parse_errors)}건"
                    "(알 수 없는 스키마 — 집계에서 제외)"
                )
            obs_shutdown_markers = _parse_shutdown_markers(obs_dst)
            if obs_shutdown_markers:
                manifest.append(f"    이 날짜 종료 마커 {len(obs_shutdown_markers)}건 발견(상세는 커버리지 요약 참고)")

        # 2026-09-18 재재검토 반영(지적 3번): "주문일별 관측률"은 조회
        # 시각이 아니라 주문 접수일 기준이어야 하므로, 하루로 자르지
        # 않은 전체 원본을 다시 훑어 order_accepted_at이 이 날짜인
        # 관측을 모읍니다(조회가 다음날 이뤄졌어도 원래 주문일에 귀속).
        # 2026-09-21 3차 재검토 반영(지적 1번): 격리된 파일도 함께 훑음.
        aggregation_computed_at = datetime.now().isoformat()
        order_day_records, order_day_parse_errors = _load_full_observations_for_order_date(
            obs_src, target, quarantine_paths=quarantined_obs_paths,
        )
        if order_day_parse_errors:
            manifest.append(
                f"    ⚠ 주문일 기준 관측 복원 실패 {len(order_day_parse_errors)}건"
                "(알 수 없는 스키마 — 집계에서 제외)"
            )

        # 2026-09-21 3차 재검토 반영(지적 3번, 재현된 버그): 주문일
        # 커버리지 집계(order_day_records)에는 다른 날짜(익일 등)에
        # 조회된 관측도 섞여 있는데, 그 관측은 조회일 raw 슬라이스
        # (obs_records, started_at 기준)에는 나타나지 않아 번들만으로는
        # 그 수치의 근거를 재확인할 수 없었습니다. write_id로 차집합을
        # 구해 그 몫만 별도 raw 파일로 남깁니다.
        query_day_write_ids = {obs.write_id for obs in obs_records if obs.write_id}
        order_day_extra_records = [
            obs for obs in order_day_records
            if not obs.write_id or obs.write_id not in query_day_write_ids
        ]
        if order_day_extra_records:
            extra_dst = work / f"order_status_observations_orderday_extra_{day_compact}.jsonl"
            extra_lines = [
                json.dumps(_mask_json_value(dataclasses.asdict(o)), ensure_ascii=False, sort_keys=True)
                for o in order_day_extra_records
            ]
            extra_dst.write_text("\n".join(extra_lines) + "\n", encoding="utf-8")
            raw_files.append(extra_dst.name)
            manifest.append("")
            manifest.append("[ RAW — 주문일 커버리지에 쓰인 익일(교차일) 관측 근거 ]")
            manifest.append(
                f"  {extra_dst.name:44s} | OK | {len(order_day_extra_records):,}건"
                " (2026-09-21 3차 재검토 반영, 지적 3번 — 이 날짜 접수 주문을 다른"
                " 날짜에 조회한 관측. 계좌별 커버리지 집계에는 포함되지만 위 조회일"
                " raw 슬라이스에는 나타나지 않는 근거)"
            )
        else:
            manifest.append("")
            manifest.append("[ RAW — 주문일 커버리지에 쓰인 익일(교차일) 관측 근거 ]")
            manifest.append("  없음 (이 날짜 접수 주문에 대해 다른 날짜에 조회된 관측 없음)")

        # 2026-09-18 재재검토 반영(지적 5번): 종료 마커와 별도로,
        # 기록기가 살아있는 동안 주기적으로 갱신하는 "현재 실행 상태"
        # 스냅샷을 함께 읽어 번들에 반영합니다.
        # 2026-09-21 3차 재검토 반영(지적 2번, 재현된 버그): "파일 없음"과
        # "파일은 있지만 손상돼 못 읽음"을 구분해야 하므로 두 번째
        # 반환값(status_file_existed)도 함께 받습니다.
        obs_status_path = status_path_for(obs_src)
        run_status, status_file_existed = _read_running_status(obs_status_path)
        if run_status is not None:
            status_display = "있음"
        elif status_file_existed:
            status_display = "손상(있지만 읽을 수 없음)"
        else:
            status_display = "없음"
        manifest.append(
            f"  observation_status_snapshot = {obs_status_path} ({status_display})"
        )

        manifest.append("")
        manifest.append("[ RAW — 로그 (allowlist 태그 줄만, 마스킹 적용) ]")
        app_sources = rotated_log_paths(LOGS_DIR)
        log_lines: list[str] = []
        if app_sources:
            dst = work / f"app_analysis_{day_compact}.log"
            total, kept, log_lines, used = slice_log(app_sources, dst, target)
            raw_files.append(dst.name)
            manifest.append(f"  {'app.log(+로테이션)':30s} | OK | {kept:,}줄 / 전체 {total:,}줄")
            manifest.append("  source logs:")
            for u in (used or ["(해당 날짜 줄이 있는 파일 없음)"]):
                manifest.append(f"    - {u}")
        else:
            manifest.append(f"  {'app.log':30s} | MISSING | 원본 없음 | excluded")
        manifest.append(f"  allowlist: {', '.join(LOG_TAGS)}")
        manifest.append("  ※ 모든 WARNING/ERROR 자동 포함은 하지 않습니다(1I.1).")
        manifest.append(f"  마스킹 키: {', '.join(SENSITIVE_KEYS)}")

        manifest.append("")
        manifest.append("[ REPORT — 분석 결과 문서 ]")
        for prefix in ["daily_report", "signal_analysis", "trade_analysis",
                       "indicator_analysis", "shadow_analysis", "bb_block_impact",
                       "minute_bar_quality", "replay", "crash_rebound"]:
            for base in (REPORTS_DIR, LOGS_DIR):
                src = base / f"{prefix}_{day_compact}.txt"
                if src.exists():
                    shutil.copy2(src, work / src.name)
                    report_files.append(src.name)
                    manifest.append(f"  {src.name:44s} {src.stat().st_size / 1024:,.0f} KB")
                    break
        if not report_files:
            manifest.append("  ⚠ 해당 날짜 리포트 없음 (15:20 이전 종료됐을 수 있음)")

        # 2026-09-16 (GPT 8차 재검토 지적 반영): daily_report가 아직
        # 잔고 대조가 끝나지 않은 채(미해결 주문 남은 상태) 생성된
        # 잠정본이면, 리포트 본문(DailyReporter._build_report()가
        # 남기는 배너)을 그대로 확인해 번들 메타데이터에도 같은
        # 사실을 남긴다 — 리포트 파일만 보고 잠정 여부를 놓칠 수
        # 있는 사람이 MANIFEST.txt만 봐도 알 수 있게 하기 위함.
        is_provisional_report = False
        daily_report_name = f"daily_report_{day_compact}.txt"
        if daily_report_name in report_files:
            try:
                is_provisional_report = "잠정(대조 미완료)" in (work / daily_report_name).read_text(encoding="utf-8")
            except OSError:
                pass

        # 수집 품질
        quality = build_collection_quality(
            target, log_lines, counts,
            _read_csv(work / f"entry_quality_shadow_{day_compact}.csv"),
            _read_csv(work / f"trades_{day_compact}.csv"),
            _read_csv(work / f"signal_log_{day_compact}.csv"),
        )
        (work / "collection_quality.txt").write_text(quality, encoding="utf-8")

        # 체결조회 증거 커버리지 — trades.csv(오늘 슬라이스)와
        # run_baseline.csv 조인 결과(all_baselines)를 그대로 재사용.
        order_status_summary = build_order_status_summary(
            target, obs_records, order_day_records,
            _read_csv(work / f"trades_{day_compact}.csv"),
            all_baselines,
            shutdown_markers=obs_shutdown_markers,
            run_status=run_status,
            status_file_existed=status_file_existed,
            order_day_extra_count=len(order_day_extra_records),
            aggregation_computed_at=aggregation_computed_at,
        )
        (work / "order_status_coverage.txt").write_text(order_status_summary, encoding="utf-8")

        manifest.append("")
        manifest.append("[ METADATA ]")
        manifest.append("  collection_quality.txt — 수집 완전성·재시작·coverage 요약")
        manifest.append("  order_status_coverage.txt — 체결조회 증거 저장·커버리지 요약(손익 계산 아님)")
        if is_provisional_report:
            manifest.append(
                "  ⚠ daily_report — 잠정(대조 미완료) 상태로 생성됨. "
                "잔고 대조 완료 후 재생성된 최종본으로 교체될 수 있습니다."
            )
        manifest.append("")
        manifest.append("[ 포함하지 않은 것 ]")
        manifest.append("  .env / state.json / runtime_state.json / token 응답 원문")
        manifest.append("  주문·잔고 API response body, 인증 헤더")
        manifest.append("  분봉 원본(data/) — 성과 계산 단계에서 별도 요청")
        manifest.append("=" * 58)
        (work / "MANIFEST.txt").write_text("\n".join(manifest), encoding="utf-8")

        # ── 원자적 ZIP 생성 ──
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for p in sorted(work.iterdir()):
                if not p.is_file():
                    continue
                if p.name in report_files:
                    arc = f"reports/{p.name}"
                elif p.name in raw_files:
                    arc = f"raw/{p.name}"
                elif p.name in ("collection_quality.txt", "order_status_coverage.txt"):
                    arc = f"metadata/{p.name}"
                else:
                    arc = p.name
                z.write(p, arc)
        # 무결성 확인 후에만 최종 경로로 교체 — 중간에 죽어도
        # 기존 ZIP이 깨진 파일로 대체되지 않음.
        with zipfile.ZipFile(tmp_zip) as z:
            if z.testzip() is not None:
                raise RuntimeError("생성된 ZIP 무결성 검사 실패")
        # 2026-08-06 (1I.5): 디스크 플러시.
        # Windows에서 읽기 전용("rb") 핸들에 fsync를 걸면
        # OSError: [Errno 9] Bad file descriptor가 발생함
        # (FlushFileBuffers가 쓰기 권한을 요구하기 때문) —
        # 실서버(PowerShell)에서 실제로 재현됨. 쓰기 가능한
        # 모드로 열어야 하며, 플러시는 "있으면 좋은" 보강이지
        # 번들 생성의 성공 조건이 아니므로 실패해도 진행한다
        # (무결성은 바로 위 testzip()으로 이미 확인함).
        try:
            with open(tmp_zip, "r+b") as f:
                f.flush()
                os.fsync(f.fileno())
        except OSError as exc:
            if not quiet:
                print(f"⚠ fsync 생략({exc}) — ZIP 무결성 검사는 통과했으므로 계속 진행합니다.")
        os.replace(tmp_zip, final_path)

        if not quiet:
            print("\n".join(manifest))
            print()
            print(quality)
            print()
            print(order_status_summary)
            print()
            print(f"저장: {final_path}  ({final_path.stat().st_size / 1024 / 1024:.1f} MB)")
        return final_path
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if tmp_zip.exists():
            tmp_zip.unlink()
        try:
            if lock_path.read_text(encoding="utf-8").strip() == lock_token.strip():
                lock_path.unlink()
        except FileNotFoundError:
            pass


def _force_utf8_stdout() -> None:
    """Windows 콘솔 한글 깨짐 방지 — 직접 실행할 때만 적용."""
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def main() -> int:
    _force_utf8_stdout()
    args = sys.argv[1:]
    try:
        target = datetime.strptime(args[0], "%Y-%m-%d").date() if args else date.today()
    except ValueError:
        print("날짜 형식이 잘못됐습니다. 예: python export_daily_bundle.py 2026-08-06")
        return 1
    return 0 if build(target) is not None else 2


if __name__ == "__main__":
    sys.exit(main())
