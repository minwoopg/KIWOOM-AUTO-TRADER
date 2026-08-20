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
"""
from __future__ import annotations

import csv
import io
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from datetime import date, datetime
from pathlib import Path

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
LOG_TAGS: tuple[str, ...] = (
    "[COND_STATUS]", "[COND_TRUNCATE]", "[COND]",
    "[WS]", "[SESSION_SHADOW]", "[EXPERIMENTAL]",
    "[REPORT]", "[ANALYSIS]", "[RECONCILE]", "[MIN_STALE]",
    "[ORDER_STATUS_QUERY_FAILED]", "[ORDER_STATUS_BALANCE_MISMATCH]",
    "[ORDER_STATUS_UNSUPPORTED]", "[ORDER_STATUS_CONFIRMED]",
    "[LIFECYCLE_ORPHAN]",
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


def build(target: date, *, quiet: bool = False) -> Path | None:
    """번들을 원자적으로 생성합니다. 락 획득 실패 시 None 반환."""
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

        # 수집 품질
        quality = build_collection_quality(
            target, log_lines, counts,
            _read_csv(work / f"entry_quality_shadow_{day_compact}.csv"),
            _read_csv(work / f"trades_{day_compact}.csv"),
            _read_csv(work / f"signal_log_{day_compact}.csv"),
        )
        (work / "collection_quality.txt").write_text(quality, encoding="utf-8")

        manifest.append("")
        manifest.append("[ METADATA ]")
        manifest.append("  collection_quality.txt — 수집 완전성·재시작·coverage 요약")
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
                elif p.name == "collection_quality.txt":
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
