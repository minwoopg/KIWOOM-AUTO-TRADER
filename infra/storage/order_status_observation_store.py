from __future__ import annotations

"""우선순위1 1차: 체결조회 증거 독립 저장 + 커버리지 계측.

**이 모듈이 하는 일은 정확히 이것뿐입니다**: `Broker.get_order_status_evidence()`
호출 결과(성공이든 실패든)를 매매 루프를 절대 지연시키지 않는 방식으로
독립된 append-only 로그에 남기고, 그 로그로부터 "고유 주문 기준" 커버리지
지표를 계산합니다.

**이 모듈이 하지 않는 일**: 손익 계산, FIFO 매칭, 확정 손익 표시,
RiskManager/DailyReporter 연결, 조회 빈도 확대. `TrackedOrderJournalStore`도
전혀 재사용하지 않습니다(그 저장소는 안전한 시점에 삭제되므로 영구
증거 보존에 부적합 — 2026-09-18 지적 반영).

설계 배경(민우님 GPT 기반 review, 2026-09-18):
1. `derive_broker_order_status()`의 FILLED 확정은 가격 유효성을
   전혀 보장하지 않으므로, 이 모듈은 "판정"과 "가격 원문"을 분리해
   기록합니다 — `OrderStatusObservation.filled_price_parsed`는 공식
   판정값 그대로이고, `cntr_matches`/`oso_matches`의 각 원소가 담는
   `cntr_pric_raw` 등은 판정과 무관하게 원문을 그대로 보존합니다.
2. 커버리지는 조회 횟수가 아니라 **고유 주문 수** 기준입니다
   (`compute_coverage()` 참고) — 같은 주문을 반복 조회해도 분자가
   1건으로만 잡힙니다.
3. 디스크 쓰기는 메인 폴링 스레드에서 절대 기다리지 않습니다
   (`OrderStatusObservationRecorder`: 제한된 큐 + 별도 기록 스레드).
   "매매 루프에 영향이 없다"가 아니라 "매매 루프가 디스크 완료를
   기다리지 않는다"로 주장 범위를 좁힙니다.
4. 강제 종료 시 유실 구간을 정확히 복원할 수 있다고 약속하지
   않습니다 — `restart_id`+단조증가 `seq_no`+`clean_shutdown` 마커로
   "확인 가능한 범위"와 "불확실 구간"만 구분합니다.
5. `account_scope_id`가 설정되지 않아도 프로그램 기동을 막지
   않습니다 — 이 경우 관측 기능 자체가 비활성화되고, 커버리지는
   "0%"가 아니라 별도의 "계측 비활성" 상태로 표시됩니다.
"""

import hashlib
import json
import logging
import os
import queue
import threading
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from pathlib import Path
from typing import Any, Literal

SCHEMA_VERSION = 1

# ── 환경 3분류 ────────────────────────────────────────────────────
# 2026-09-18 (지적 반영): base_url만으로는 로컬 MockBroker 실행까지
# 구분하지 못합니다. app/main.py의 build_broker()가 이미
# settings.broker.use_mock으로 MockBroker/KiwoomBroker를 나누고,
# KiwoomBroker 내부에서 base_url로 모의투자/실전투자를 나누므로
# 그 두 판단을 그대로 재사용합니다(새 판정 로직을 만들지 않음).
ENV_LOCAL_MOCK = "local_mock"
ENV_KIWOOM_MOCK = "kiwoom_mock"
ENV_KIWOOM_REAL = "kiwoom_real"


def resolve_env(*, use_mock: bool, is_paper_trading: bool) -> str:
    """`BrokerConfig`의 기존 두 필드만으로 환경 3분류를 판정합니다.

    2026-09-18: 처음에는 `base_url` 문자열에 "mock"이 포함되는지로
    판정하려 했으나, `infra/storage/run_baseline.py`가 이미 프로세스
    시작 시점에 `is_mock`/`is_paper_trading`을 명시적 bool로 확정해
    기록하고 있음을 확인해 그 값을 그대로 재사용합니다(문자열
    추정보다 신뢰할 수 있음, `resolve_env_from_baseline_row()`와
    동일한 판정 기준으로 일관성 유지).
    """

    if use_mock:
        return ENV_LOCAL_MOCK
    return ENV_KIWOOM_MOCK if is_paper_trading else ENV_KIWOOM_REAL


# ── 관측 레코드 ───────────────────────────────────────────────────

QueryOutcome = Literal["success", "api_error", "partial"]


@dataclass
class OrderStatusObservation:
    """`get_order_status_evidence()` 호출 하나(성공/실패 모두)에 대한
    관측 레코드. 판단·집계를 하지 않고 원문을 그대로 담습니다.

    2026-09-18 (지적 2번 반영): `query_id`는 호출 **이전에** 발급해,
    실패한 조회도 같은 식별자로 종료 결과를 기록할 수 있게 합니다
    (성공한 조회만 기록되던 v2의 결함을 수정).
    """

    query_id: str
    started_at: str          # 호출 직전 시각(ISO) — 성공/실패 모두 존재
    finished_at: str | None  # 호출 완료 시각(ISO) — 실패 시에도 채움
    account_scope_id: str
    env: str
    symbol: str
    requested_order_id: str
    side_context: str            # "BUY_PENDING" | "SELL_PENDING" | "ORPHAN"
    query_kind: str              # 위와 동일한 값(호출부 표현 그대로 보존)
    pending_age_sec_at_query: float

    outcome: QueryOutcome        # "success" | "api_error" | "partial"
    failure_stage: str | None = None   # 실패/부분 성공 시: "oso_fetch" | "cntr_fetch" | "evidence_build" 등
    error_repr: str | None = None      # 예외 요약(민감정보 없음 전제 — 저장 전 export 단계에서 추가 마스킹)

    psm_broker_status: str | None = None   # BrokerOrderStatus 값 — PSM이 실제 소비한 값과 동일(감사용)
    filled_price_parsed: int | None = None  # 공식 판정과 결합된 값(=BrokerOrder.filled_price)

    # 각 원소 예: {"response_order_id", "ord_stt", "cntr_pric_raw", "cntr_qty_raw",
    #             "oso_qty_raw", "ord_qty_raw", "io_tp_nm", "trde_tp", "raw": {...}}
    # "raw"에는 위에 구조화되지 않은 나머지 원문 필드(체결시각/체결식별자 등,
    # 실측 전이라 키 이름을 아직 확정할 수 없음)를 통째로 보존합니다.
    # 제외하는 필드: 없음 — export 단계의 SENSITIVE_KEYS 마스킹만 적용됩니다.
    cntr_matches: list = field(default_factory=list)
    oso_matches: list = field(default_factory=list)
    cntr_match_count: int = 0
    oso_match_count: int = 0

    base_quantity_before_order: int | None = None
    target_quantity_after_order: int | None = None
    journal_linked: bool = False   # TrackedOrderRecord와 연결해 스냅샷을 채울 수 있었는가

    write_id: str = ""       # 이 "사건"의 식별자 — 재시도는 같은 값 재사용, 새 조회만 새로 발급
    restart_id: str = ""
    seq_no: int = 0

    schema_version: int = SCHEMA_VERSION

    def content_hash(self) -> str:
        """`write_id`가 같은 두 레코드의 내용이 실제로 동일한지 비교하기
        위한 해시(write_id 자체와 순전히 기록 시점에만 달라질 수 있는
        필드는 제외 — finished_at 같은 시각 필드는 재시도마다 달라질
        수 있어 해시에서 뺍니다)."""

        payload = asdict(self)
        for k in ("finished_at", "seq_no"):
            payload.pop(k, None)
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()

    def to_json_line(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


def build_entry_evidence(raw: dict) -> dict:
    """cntr/oso 원본 행 하나를 관측 레코드용 구조로 변환합니다.

    2026-09-18 (지적 2번 반영): `ord_qty`(요청 수량 원문)를 누락하지
    않고 포함합니다 — 기존 FILLED 판정 자체가 이 값을 쓰므로, 나중에
    수량 관계(요청/체결/미체결)를 검증하려면 반드시 필요합니다.
    구조화되지 않은 나머지 필드는 "raw"에 원문 그대로 보존합니다
    (필드 목록이 실측 전이라 어떤 키가 더 있을지 확정할 수 없으므로,
    선별 보존이 아니라 전체 보존 + 구조화된 상위 필드로 접근성만
    높이는 방식을 택함).
    """

    return {
        "response_order_id": raw.get("ord_no"),
        "ord_stt": raw.get("ord_stt"),
        "ord_qty_raw": raw.get("ord_qty"),
        "cntr_qty_raw": raw.get("cntr_qty"),
        "oso_qty_raw": raw.get("oso_qty"),
        "cntr_pric_raw": raw.get("cntr_pric"),
        "io_tp_nm": raw.get("io_tp_nm"),
        "trde_tp": raw.get("trde_tp"),
        "raw": dict(raw),
    }


# ── 큐 + 별도 기록 작업자 ─────────────────────────────────────────

class OrderStatusObservationRecorder:
    """제한된 큐에 넣고 별도 스레드가 디스크에 append합니다.

    2026-09-18 (지적 3/4번 반영):
    - `record()`는 큐가 가득 차거나 예외가 나도 절대 블로킹하지
      않습니다 — 그 자리에서 `dropped_count`만 올리고 즉시 반환합니다.
      "작업자 스레드가 매매 루프에 전혀 영향을 주지 않는다"는 표현
      대신 "매매 루프가 디스크 완료를 기다리지 않는다"로 범위를
      좁힙니다(GIL/스케줄링 수준의 미세한 영향까지 없다고 주장하지
      않음).
    - 쓰기 실패는 **같은 파일에 재시도하지 않습니다** — 부분 쓰기가
      파일 중간에 손상된 행으로 남을 위험을 피하기 위해, 실패한
      레코드는 버리고 `lost_count`만 올립니다. 호출부가 정말 같은
      사건을 다시 기록하고 싶다면 같은 `write_id`로 새 `record()`
      호출을 큐에 다시 넣어야 하며, 그 경우 새 줄로 파일 끝에
      추가됩니다(파일 중간이 아니라 항상 끝에만 추가되므로, export
      단계의 "마지막 줄만 불완전할 수 있다"는 가정이 깨지지 않습니다).
    - `shutdown()`은 상한 시간 안에 큐를 드레인하고, 그 안에 못 끝내면
      나머지는 유실로 간주해 프로세스 종료 자체를 막지 않습니다.
      `clean_shutdown`(정상 종료 경로 도달+제한시간 내 드레인 완료),
      `queue_drained`(드레인 시도가 끝났는지 — 디스크 성공을 의미하지
      않음), `dropped_count`(큐 포화/쓰기 실패로 버려진 건수)를
      서로 다른 사실로 분리해 마커에 남깁니다.
    """

    def __init__(
        self,
        path: str,
        *,
        app_logger: logging.Logger | None = None,
        maxsize: int = 200,
        shutdown_drain_timeout_sec: float = 3.0,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._app_logger = app_logger or logging.getLogger(__name__)
        self._queue: "queue.Queue[OrderStatusObservation]" = queue.Queue(maxsize=maxsize)
        self._shutdown_drain_timeout_sec = shutdown_drain_timeout_sec
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._seq_lock = threading.Lock()
        self._seq_no = 0
        self.restart_id = uuid.uuid4().hex
        self.dropped_count = 0
        self._dropped_lock = threading.Lock()

    # ── 시작/복구 ──
    def start(self) -> None:
        self._quarantine_incomplete_tail()
        self._thread = threading.Thread(
            target=self._writer_loop, name="order-status-observation-writer", daemon=True,
        )
        self._thread.start()

    def _quarantine_incomplete_tail(self) -> None:
        """이전 실행이 강제 종료돼 마지막 줄이 잘려있을 수 있는 경우를
        대비합니다. 마지막 줄만 JSON 파싱에 실패하면 그 줄만
        `<path>.corrupt`로 옮기고 CRITICAL 로그를 남깁니다 — 다음
        기록이 손상된 줄 뒤에 이어붙어 두 레코드가 한 줄로 뭉개지는
        것을 막기 위함입니다. 마지막 줄이 아닌 곳에서 손상이
        발견되면(파일 자체 손상 가능성) 프로젝트의 fail-close 원칙대로
        CRITICAL만 남기고 계속 진행합니다(관측 기능이므로 매매를
        막지는 않음 — 그러나 조용히 넘어가지 않음).
        """

        if not self.path.exists():
            return
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError as exc:
            self._app_logger.critical(
                f"[ORDER_STATUS_OBS_RECOVERY] {self.path} 읽기 실패 — {type(exc).__name__}: {exc}"
            )
            return
        if not lines:
            return
        bad_indices = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                json.loads(stripped)
            except json.JSONDecodeError:
                bad_indices.append(i)
        if not bad_indices:
            return
        last_idx = len(lines) - 1
        if bad_indices == [last_idx]:
            # 마지막 줄만 손상 — 정상적인 강제종료 시나리오로 취급
            corrupt_path = self.path.with_suffix(self.path.suffix + ".corrupt")
            with corrupt_path.open("a", encoding="utf-8") as f:
                f.write(
                    f"# quarantined_at={datetime.now().isoformat()} restart_id={self.restart_id}\n"
                )
                f.write(lines[last_idx])
            remaining = "".join(lines[:last_idx])
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(remaining, encoding="utf-8")
            os.replace(tmp, self.path)
            self._app_logger.critical(
                f"[ORDER_STATUS_OBS_RECOVERY] {self.path} 마지막 줄이 불완전해 "
                f"{corrupt_path.name}으로 격리했습니다(강제종료 추정) — 나머지는 정상 유지"
            )
        else:
            self._app_logger.critical(
                f"[ORDER_STATUS_OBS_RECOVERY] {self.path} 마지막 줄이 아닌 위치({bad_indices})에서 "
                f"손상 발견 — 파일 자체가 손상됐을 수 있습니다. 자동 복구하지 않고 그대로 둡니다."
            )

    # ── 기록 ──
    def record(self, observation: OrderStatusObservation) -> None:
        with self._seq_lock:
            self._seq_no += 1
            observation.seq_no = self._seq_no
        observation.restart_id = self.restart_id
        try:
            self._queue.put_nowait(observation)
        except queue.Full:
            with self._dropped_lock:
                self.dropped_count += 1
            self._app_logger.warning(
                f"[ORDER_STATUS_OBS_QUEUE_FULL] write_id={observation.write_id} — "
                f"유실 처리(매매 루프는 대기하지 않음), 누적 유실 {self.dropped_count}건"
            )

    def _writer_loop(self) -> None:
        while not self._stop_event.is_set() or not self._queue.empty():
            try:
                observation = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._write_one(observation)

    def _write_one(self, observation: OrderStatusObservation) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(observation.to_json_line() + "\n")
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass  # 기존 프로젝트 관례(TrackedOrderJournalStore)와 동일 — 치명적이지 않음
        except Exception as exc:
            with self._dropped_lock:
                self.dropped_count += 1
            self._app_logger.critical(
                f"[ORDER_STATUS_OBS_WRITE_FAILED] write_id={observation.write_id} — "
                f"{type(exc).__name__}: {exc} — 같은 파일에 재시도하지 않고 유실 처리"
            )

    # ── 종료 ──
    def shutdown(self) -> dict:
        """상한 시간 안에 드레인을 시도하고 결과 마커를 기록합니다.

        반환값의 `clean_shutdown`은 "제한시간 내에 드레인 시도가 끝났다"는
        뜻이지 "모든 레코드가 디스크에 안전히 저장됐다"는 뜻이 아닙니다
        (지적 4번 반영 — queue_drained와 디스크 확정을 분리).
        """

        self._stop_event.set()
        queue_drained = True
        if self._thread is not None:
            self._thread.join(timeout=self._shutdown_drain_timeout_sec)
            queue_drained = not self._thread.is_alive()

        result = {
            "clean_shutdown": queue_drained,
            "queue_drained": queue_drained,
            "dropped_count": self.dropped_count,
            "restart_id": self.restart_id,
            "last_seq_no": self._seq_no,
            "shutdown_at": datetime.now().isoformat(),
        }
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"__marker__": "shutdown", **result}, ensure_ascii=False) + "\n")
        except Exception as exc:
            self._app_logger.critical(
                f"[ORDER_STATUS_OBS_SHUTDOWN_MARKER_FAILED] {type(exc).__name__}: {exc}"
            )
        return result


# ── 실행 단위 메타데이터 (계좌/환경 ↔ trades.csv 연결) ────────────
#
# 2026-09-18 (지적 3번 반영): `trades.csv`에는 `account_scope_id`/`env`
# 컬럼이 없고, 이번 단계에서 그 스키마를 바꾸지 않습니다(RiskManager의
# 필수 헤더 검증을 건드리지 않기 위함). 이 연결은 새로 만들지 않고
# 기존 `infra/storage/run_baseline.py`(B01, 2026-09-11)를 그대로
# 재사용합니다 — 그 모듈이 이미 프로세스 시작마다 run_id/is_mock/
# is_paper_trading/started_at을 완전히 분리된 파일(run_baseline.csv)에
# 기록하고, trades.csv/signal_log.csv의 timestamp를 그 시간범위로
# 조인하는 `resolve_run_id_for_timestamp()`를 갖고 있었기 때문입니다.
# 이번 라운드에서 그 파일에 `account_scope_id` 필드와
# `resolve_scope_for_trade_timestamp()`(run_id 조인 결과에서
# (account_scope_id, env)를 뽑아주는 얇은 wrapper)만 추가했습니다 —
# 새 조인 로직을 병행해서 만들지 않았습니다. `compute_coverage()`의
# `accepted_orders` 인자는 그 함수의 결과로 미리 만들어 전달하고,
# 연결되는 run이 없는 과거 행은 그 단계에서 "미확인"으로 제외합니다.


# ── 커버리지 계측 ─────────────────────────────────────────────────

COVERAGE_DISABLED = "계측_비활성"


def compute_coverage(
    *,
    account_scope_id: str,
    accepted_orders: set[tuple[str, str, str, str]],   # (account_scope_id, env, order_date, order_id)
    observations: list[OrderStatusObservation],
    order_date_resolver,  # Callable[[OrderStatusObservation], str | None] — None이면 미확인
) -> dict:
    """고유 주문 기준 커버리지 4개 지표(2026-09-18 지적 2번의 표를 그대로 채택).

    - `account_scope_id`가 비어있으면 계측 자체를 비활성으로 표시합니다
      (지적 5번 반영 — 0%가 아니라 "계측 비활성").
    - 서로 다른 (env, order_date)의 같은 order_id는 절대 합치지 않습니다
      (지적 3번 반영) — 키 자체가 4-튜플입니다.
    - order_date를 확정할 수 없는 관측은 "미확인" 버킷으로 분리하고
      분모/분자 어디에도 넣지 않습니다.
    - `accepted_orders`에 없는 order_id의 관측은 "고아 관측"으로 분리합니다.
    """

    if not account_scope_id.strip():
        return {"status": COVERAGE_DISABLED, "reason": "account_scope_id 미설정"}

    unresolved_date_count = 0
    orphan_count = 0
    observed_keys: set[tuple[str, str, str, str]] = set()
    filled_keys: set[tuple[str, str, str, str]] = set()
    priced_keys: set[tuple[str, str, str, str]] = set()

    query_attempt = 0
    query_success = 0
    query_failed = 0
    write_success = 0  # 실제 디스크 반영 여부는 이 함수 호출 시점엔 알 수 없음 — 별도 카운터로 다룸(라이터의 dropped_count 참고)

    for obs in observations:
        query_attempt += 1
        if obs.outcome == "api_error":
            query_failed += 1
            continue
        query_success += 1

        order_date = order_date_resolver(obs)
        if order_date is None:
            unresolved_date_count += 1
            continue

        key = (obs.account_scope_id, obs.env, order_date, obs.requested_order_id)
        if key not in accepted_orders:
            orphan_count += 1
            continue

        observed_keys.add(key)
        if obs.psm_broker_status == "FILLED":
            filled_keys.add(key)
            if obs.filled_price_parsed is not None and obs.filled_price_parsed > 0:
                priced_keys.add(key)

    scoped_accepted = {k for k in accepted_orders if k[0] == account_scope_id}
    total_accepted = len(scoped_accepted)
    order_observation_rate = (len(observed_keys) / total_accepted) if total_accepted else None
    status_confirmation_rate = (len(filled_keys) / len(observed_keys)) if observed_keys else None
    price_capture_rate = (len(priced_keys) / len(filled_keys)) if filled_keys else None

    return {
        "status": "계측됨",
        "total_accepted_orders": total_accepted,
        "observed_unique_orders": len(observed_keys),
        "filled_unique_orders": len(filled_keys),
        "priced_unique_orders": len(priced_keys),
        "order_observation_rate": order_observation_rate,
        "status_confirmation_rate": status_confirmation_rate,
        "price_capture_rate": price_capture_rate,
        "query_attempt_count": query_attempt,
        "query_success_count": query_success,
        "query_failed_count": query_failed,
        "unresolved_order_date_count": unresolved_date_count,
        "orphan_observation_count": orphan_count,
    }


def dedupe_by_write_id(
    observations: list[OrderStatusObservation],
) -> tuple[list[OrderStatusObservation], list[str]]:
    """같은 `write_id`가 여러 번 나타나는 경우를 처리합니다.

    2026-09-18 (지적 4번 반영): 내용까지 동일하면 정상적인 재시도로
    간주해 한 번만 집계에 남기고, 내용이 다르면 그 자체가 버그
    신호이므로 조용히 하나를 고르지 않고 `write_id` 목록을 별도로
    반환해 호출부가 오류로 처리하게 합니다.
    """

    by_id: dict[str, list[OrderStatusObservation]] = {}
    for obs in observations:
        by_id.setdefault(obs.write_id, []).append(obs)

    deduped: list[OrderStatusObservation] = []
    conflicting_ids: list[str] = []
    for write_id, group in by_id.items():
        hashes = {g.content_hash() for g in group}
        if len(hashes) == 1:
            deduped.append(group[0])
        else:
            conflicting_ids.append(write_id)
    return deduped, conflicting_ids
