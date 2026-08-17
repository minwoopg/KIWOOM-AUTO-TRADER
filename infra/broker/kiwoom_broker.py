from __future__ import annotations

"""키움 REST API용 실제 모의투자/실전투자 브로커 구현.

이 파일은 이전 스켈레톤 버전에서 한 단계 더 나아가,
지금까지 사용자가 직접 검증한 키움 REST API 호출들을
프로젝트 구조 안으로 옮긴 구현체입니다.

이 브로커가 담당하는 일:
1. 앱키/시크릿키로 접근 토큰 발급
2. 종목 현재가(기본정보) 조회
3. 예수금/주문가능금액 조회
4. 보유 종목 조회
5. 매수 / 매도 주문

주의:
- 이 코드는 '키움 모의투자부터' 쓰는 것을 권장합니다.
- settings.yaml 에서 base_url 을 mock 서버로 두고 먼저 테스트하세요.
- 실전투자로 바꿀 때는 base_url, 앱키/시크릿키, 주문 시간대를 다시 확인하세요.
"""

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests

from config.settings import BrokerConfig
from domain.models import AccountBalance, MarketPrice, OrderRequest, OrderResult, OrderSide, Position, PriceBar, WeeklyBar, MinuteBar
from infra.broker.base import Broker


class KiwoomTransportError(RuntimeError):
    """`session.post()` 자체가 실패(타임아웃, connection 오류 등)해
    브로커 응답을 아예 받지 못한 경우입니다.

    2026-08-14 (1P0.8-P0.1, 319400 실측 P0 사고 대응): 요청이 실제로
    처리됐는지 알 수 없습니다(응답이 없으므로). `place_order()`는 이
    예외를 받으면 주문 접수 여부를 "불명(ambiguous)"으로 취급해야
    합니다 — 아래 `KiwoomHttpError`(서버가 명시적으로 응답한 거부)와
    달리, 실제로는 브로커에 주문이 들어갔을 수도 있으므로 함부로
    롤백하고 재주문하면 중복 주문 위험이 있습니다. 이 클래스는
    `RuntimeError`를 상속하므로, 기존에 `_post()` 실패를 `RuntimeError`/
    `Exception`으로 넓게 잡던 다른 모든 호출부(일봉/분봉 조회 등)의
    동작은 전혀 바뀌지 않습니다 — `place_order()`만 이 타입을 구분해
    별도로 처리합니다.
    """


class KiwoomHttpError(RuntimeError):
    """HTTP 응답은 정상적으로 받았지만 `status_code != 200`인 경우입니다.

    2026-08-14 (1P0.8-P0.1, 319400 실측 P0 사고 대응): 8/14 319400
    종목의 SELL 주문(kt10001)이 HTTP 429("허용된 API 요청 개수를
    초과")로 거부됐는데, 당시 코드는 이를 구분 없이 일반
    `RuntimeError`로 던져서 `place_order()` 호출부(TradingService)가
    이미 `SELL_PENDING`으로 바꿔놓은 상태를 롤백할 방법이 없었습니다.

    2026-08-14 (1P0.8-P0.1 재검토, GPT 코드리뷰 지적 — P0): **이
    예외 자체가 "주문이 미접수됐다"는 증거는 아닙니다.** 최초
    구현은 `status_code != 200`이면 전부 `accepted=False`(definitive
    reject)로 취급했는데, 이는 과분류입니다. 키움 공식 문서는
    kt10000/kt10001이 `/api/dostk/ordr` POST 주문 API라는 것만
    명시할 뿐, "모든 HTTP 오류 코드에 대해 주문이 미접수임을
    보장한다"는 계약까지 제공하지 않습니다 — 예를 들어 408/5xx는
    "키움 내부에서는 이미 주문을 처리했는데 응답 단계에서
    gateway/internal error가 났다"는 시나리오를 배제할 수 없습니다.
    이런 경우까지 definitive reject로 취급해 OPEN/FLAT으로 롤백하고
    재주문을 허용하면, 오늘 막으려던 바로 그 중복 주문 사고로
    이어집니다.

    그래서 `place_order()`는 이 예외를 받아도 곧바로 definitive
    reject로 확정하지 않고, `_is_confirmed_rate_limit_reject()`로
    실제 관찰·확인된 shape(HTTP 429 + `return_code=5` +
    `return_msg`에 "1700" 포함)인지 먼저 확인합니다. 그 shape과
    정확히 일치할 때만 `accepted=False`(안전하게 롤백/재시도
    가능)로 반환하고, 그 외 모든 non-200(408/5xx 포함, 형태가 다른
    429 포함)은 `is_ambiguous=True`로 fail-close합니다 — "모르면
    재주문하지 않는다"는 원칙을 HTTP 레벨에서도 지키기 위함입니다.
    """

    def __init__(self, message: str, status_code: int, body: Any) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def _is_confirmed_rate_limit_reject(status_code: int, body: Any) -> bool:
    """오늘(8/14) 319400 실측으로 안전성이 확인된 단 하나의 케이스만
    definitive reject로 whitelist합니다: HTTP 429 + `return_code=5` +
    `return_msg`에 "1700"(허용된 API 요청 개수 초과) 포함.

    2026-08-14 (1P0.8-P0.1 재검토, GPT 코드리뷰 지적 — P0): 이 함수가
    `False`를 반환하는 모든 경우(408/5xx, 형태가 다른 429, 다른
    return_code 등)는 `place_order()`가 `is_ambiguous=True`로
    처리합니다 — 즉 "definitive reject로 넓힐지"를 결정하는 게
    아니라 "이것만 확실하니 이것만 예외로 인정한다"는 whitelist
    입니다. 401/403 등 다른 코드도 실제 응답이나 공식 계약을
    확보하면 여기 추가하면 되지만, 지금은 추측으로 넓히지 않습니다.
    """
    if status_code != 429:
        return False
    if not isinstance(body, dict):
        return False
    if body.get("return_code") != 5:
        return False
    return "1700" in str(body.get("return_msg", ""))


# 2026-08-14 (1P0.8-P0.3, 민우님 확정 범위): _is_confirmed_rate_limit_reject()
# whitelist에 해당하는 429/1700만 대상으로 하는 짧은 bounded retry입니다.
# timeout/connection reset/408/5xx/형태가 다른 429에는 절대 적용하지
# 않습니다(place_order()에서 이 케이스들은 첫 시도든 재시도 도중이든
# 즉시 is_ambiguous=True로 fail-close되고, 재시도 루프에 들어오지
# 않습니다). 무한 재시도를 막기 위해 최대 횟수를 상수로 고정합니다.
PLACE_ORDER_RATE_LIMIT_MAX_RETRIES = 2
PLACE_ORDER_RATE_LIMIT_RETRY_BACKOFF_SECONDS = 1.5


@dataclass(frozen=True)
class KiwoomApiResponse:
    """키움 API 공통 응답을 다룰 때 사용하는 내부 보조 객체입니다.

    왜 이런 객체를 두나?
    - requests.Response 를 코드 전체에서 직접 다루면 읽기가 어려워집니다.
    - 필요한 값만 한 번 정리해서 넘기면 디버깅이 쉬워집니다.
    """

    status_code: int
    headers: dict[str, str]
    body: dict[str, Any]


class KiwoomBroker(Broker):
    """키움 REST API와 통신하는 실제 브로커 구현체입니다.

    이 클래스는 TradingService 가 사용하는 브로커 인터페이스를 구현합니다.
    즉 TradingService 입장에서는
    - '어느 증권사인지'
    - '실전인지 모의인지'
    를 몰라도 되고,
    그저 '가격 주세요', '잔고 주세요', '주문 넣어주세요' 라고만 요청하면 됩니다.
    """

    def __init__(self, config: BrokerConfig) -> None:
        """브로커 설정과 HTTP 세션을 준비합니다.

        Parameters
        ----------
        config:
            settings.yaml 에서 읽어온 키움 관련 설정입니다.
        """

        self.config = config
        self.session = requests.Session()
        self.access_token: str | None = None
        self.token_expires_at: str | None = None
        # 2026-07-27 (1B단계): 분봉 진단 로그를 (symbol, base_date,
        # tick_scope, count) 조합별 최초 1회만 남기기 위한 키 집합.
        # 로그 기록이 실제로 성공한 뒤에만 키를 추가 — 로그 자체가
        # 실패해도 다음 호출에서 재시도할 수 있게 함.
        self._minute_diagnostic_keys: set[tuple[str, str, str, int, str]] = set()
        # 2026-08-14 (1P0.8-P0.3): place_order()의 429/1700 bounded
        # retry가 재시도 사이에 대기하는 데 쓰는 sleep 함수입니다.
        # 테스트에서 실제로 몇 초씩 기다리지 않도록 broker._retry_sleep
        # 을 no-op으로 바꿔치기할 수 있게 인스턴스 속성으로 뒀습니다.
        self._retry_sleep = time.sleep

    def authenticate(self) -> None:
        """키움 OAuth 토큰을 발급받아 저장합니다.

        사용자가 직접 테스트했던 것과 같은 흐름입니다.
        - URL: /oauth2/token
        - body: grant_type, appkey, secretkey

        성공하면 self.access_token 에 Bearer 토큰 원문이 저장됩니다.
        실패하면 RuntimeError 를 발생시켜 프로그램이 더 진행되지 않게 합니다.
        """

        payload = {
            "grant_type": "client_credentials",
            "appkey": self.config.app_key,
            "secretkey": self.config.secret_key,
        }

        response = self.session.post(
            f"{self.config.base_url}/oauth2/token",
            json=payload,
            timeout=10,
        )
        api_response = self._to_api_response(response)

        if api_response.status_code != 200:
            raise RuntimeError(f"token request failed: http={api_response.status_code}, body={api_response.body}")

        if api_response.body.get("return_code") != 0:
            raise RuntimeError(f"token request failed: {api_response.body}")

        token = api_response.body.get("token")
        if not token:
            raise RuntimeError(f"token missing in response: {api_response.body}")

        self.access_token = token
        self.token_expires_at = api_response.body.get("expires_dt")

    def get_minute_bars(self, symbol: str, tick_scope: int = 3, count: int = 40) -> list[MinuteBar]:
        """키움 분봉 API(ka10080)로 분봉 데이터를 가져옵니다.

        응답 배열 키: stk_min_pole_chart_qry
        체결시간(cntr_tm): 'YYYYMMDDHHmmSS' 형식
        가격에 -부호가 붙을 수 있으므로 _parse_abs_int로 처리합니다.

        2026-08-04 (실서버 코드가 1B.5 이전 버전으로 남아있던 것을
        발견해 복구): 이전 버전은 raw_bars를 받은 직후 응답이
        정상이든 비어있든 상관없이 항상 먼저 _maybe_log_minute_bar_
        diagnostics(raw_bars=raw_bars, returned_bars=[])를 호출해
        (symbol, base_date, tick_scope, count) 키를 선점했음. 이후
        실제 파싱·reverse()를 완료한 뒤 returned_bars=bars로 다시
        같은 함수를 불러도, 이미 등록된 키 때문에 최초 1회 방어
        로직이 이 두 번째(진짜 값을 담은) 호출을 조용히 스킵함 —
        실제로 이 문제가 재발한 실제 app.log로 재현 확인(정상
        60개를 반환했는데 로그엔 outcome= 필드조차 없고 returned=0
        으로 찍힘, outcome 필드 자체가 CHANGELOG 1B.5에서 추가된
        것이라 애초에 이 버전에는 없었음).

        수정: "빈 응답"과 "정상 응답"을 서로 다른 코드 경로에서만
        각각 정확히 한 번씩 진단하도록 재구성 — response_outcome
        ("EMPTY"/"SUCCESS")이 진단 키에도 포함되어 절대 서로를
        밀어내지 않음(1B.5). 정상 응답이어도 구조 검증(1B.7~1B.9)
        을 통과한 것만 반환하도록 확장 — 과거 봉 우회, 진입 품질
        미달 우회 등은 domain/service/trading_service.py의
        _evaluate_bar_freshness()가 이미 담당하므로, 이 함수는
        1B단계 원안 그대로 "raw_bars[:count] 파싱 + reverse()"라는
        핵심 로직 자체는 건드리지 않고 진단만 정확하게 복구.
        """
        from datetime import date

        base_dt = date.today().strftime("%Y%m%d")

        # 진단용 요청 시작 시각 (KST) — 기존 로직에는 영향 없음.
        _request_started_at = self._safe_diagnostic_now()

        api_response = self._post(
            endpoint="/api/dostk/chart",
            api_id="ka10080",
            payload={
                "stk_cd": symbol,
                "tic_scope": str(tick_scope),
                "upd_stkpc_tp": "1",
                "base_dt": base_dt,
            },
        )

        # 진단용 응답 수신 시각 — 기존 로직에는 영향 없음
        _response_received_at = self._safe_diagnostic_now()

        raw_bars = api_response.body.get("stk_min_pole_chart_qry", [])

        # ── 빈 응답 경로: EMPTY 진단만, 여기서만 실행 ──────────────
        if not raw_bars:
            try:
                self._try_log_minute_bar_diagnostics(
                    symbol=symbol,
                    base_date=base_dt,
                    tick_scope=str(tick_scope),
                    requested_count=count,
                    raw_bars=raw_bars,
                    returned_bars=[],
                    headers=api_response.headers,
                    request_started_at=_request_started_at,
                    response_received_at=_response_received_at,
                    response_outcome="EMPTY",
                )
            except Exception as diag_exc:
                import logging
                logging.getLogger("app").warning(
                    f"[MIN_BOOTSTRAP] {symbol} | 진단 로그 생성 실패(무시, 분봉 조회는 정상 진행): "
                    f"{diag_exc}"
                )

            import logging
            logging.getLogger("app").error(
                f"[MIN] 분봉 빈 응답 — body 키: {list(api_response.body.keys())} | "
                f"return_msg: {api_response.body.get('return_msg', '없음')}"
            )
            return []

        bars: list[MinuteBar] = []
        for item in raw_bars[:count]:
            bars.append(
                MinuteBar(
                    cntr_tm=str(item.get("cntr_tm", "")),
                    open_price=self._parse_abs_int(item.get("open_pric")),
                    high_price=self._parse_abs_int(item.get("high_pric")),
                    low_price=self._parse_abs_int(item.get("low_pric")),
                    close_price=self._parse_abs_int(item.get("cur_prc")),
                    volume=self._parse_abs_int(item.get("trde_qty")),
                    acc_volume=self._parse_abs_int(item.get("acc_trde_qty")),
                )
            )

        # 키움은 최신 → 과거 순이므로 뒤집어서 과거 → 최신 순으로 반환
        bars.reverse()

        # ── 정상 응답 경로: SUCCESS 진단, 여기서만 정확히 한 번 ────
        # 위까지의 모든 로직(raw_bars[:count], MinuteBar 파싱,
        # bars.reverse())은 이 블록 이전과 완전히 동일함. 아래는
        # 오직 로그만 남기고, bars 값을 전혀 건드리지 않음.
        try:
            self._try_log_minute_bar_diagnostics(
                symbol=symbol,
                base_date=base_dt,
                tick_scope=str(tick_scope),
                requested_count=count,
                raw_bars=raw_bars,
                returned_bars=bars,
                headers=api_response.headers,
                request_started_at=_request_started_at,
                response_received_at=_response_received_at,
                response_outcome="SUCCESS",
            )
        except Exception as diag_exc:
            import logging
            logging.getLogger("app").warning(
                f"[MIN_BOOTSTRAP] {symbol} | 진단 로그 생성 실패(무시, 분봉 조회는 정상 진행): "
                f"{diag_exc}"
            )

        return bars

    @staticmethod
    def _safe_diagnostic_now():
        """진단용 현재 시각(KST)을 안전하게 반환합니다. 절대 예외를 던지지 않습니다.

        2026-07-27 (2차 GPT 코드리뷰 지적): get_minute_bars()에서
        요청 시작/응답 수신 두 지점이 각각 동일한 try/except
        패턴을 중복하고 있었음 — 하나의 헬퍼로 정리. 실패 시 None을
        반환하며, 이 반환값이 None이어도 이후 로직(분봉 파싱/반환)
        은 전혀 영향받지 않음(진단 필드가 None으로 남을 뿐).
        """
        try:
            from datetime import datetime as _dt
            from infra.broker.minute_bar_diagnostics import KST as _KST
            return _dt.now(_KST)
        except Exception:
            return None

    def _try_log_minute_bar_diagnostics(
        self,
        *,
        symbol: str,
        base_date: str,
        tick_scope: str,
        requested_count: int,
        raw_bars: list,
        returned_bars: list[MinuteBar],
        headers: dict[str, str],
        request_started_at,
        response_received_at,
        response_outcome: str,
    ) -> None:
        """(symbol, base_date, tick_scope, count, outcome) 조합별 최초 1회만 진단 로그를 남깁니다.

        2026-08-04 (1B.5 CHANGELOG 재현·복구): 이름을 _maybe_log_...
        에서 _try_log_...로 변경, response_outcome("EMPTY" 또는
        "SUCCESS")을 진단 키에 포함시켜 두 결과가 서로 다른 진단으로
        남도록 함 — 호출부(get_minute_bars)가 이제 이 함수를 정확히
        한 경로에서만(EMPTY면 빈 응답 분기에서, SUCCESS면 파싱 완료
        후) 호출하므로, 같은 (symbol, base_date, tick_scope, count)
        조합이라도 EMPTY 진단과 SUCCESS 진단이 서로를 밀어내지 않음.

        로그 기록이 실제로 성공한 뒤에만 키를 추가 — 로그 자체가
        실패해도(디스크 문제 등) 다음 호출에서 재시도할 수 있음.
        """
        from infra.broker.minute_bar_diagnostics import (
            build_minute_bar_diagnostics, format_diagnostics_log_line,
            format_order_detail_log_line,
        )

        key = (symbol, base_date, tick_scope, requested_count, response_outcome)
        if key in self._minute_diagnostic_keys:
            return

        diagnostics = build_minute_bar_diagnostics(
            symbol=symbol,
            base_date=base_date,
            tick_scope=tick_scope,
            requested_count=requested_count,
            raw_bars=raw_bars,
            returned_bars_timestamps=[b.cntr_tm for b in returned_bars],
            headers=headers,
            request_started_at=request_started_at,
            response_received_at=response_received_at,
            response_outcome=response_outcome,
        )

        import logging
        app_logger = logging.getLogger("app")
        app_logger.info(format_diagnostics_log_line(diagnostics))
        # 2026-07-27: raw_sort_direction이 UNKNOWN(비단조)일 때만
        # 원인 조사용 2차 로그를 추가로 남김 — 정상(ASC/DESC)이면
        # None이 반환되어 로그를 남기지 않음.
        order_detail = format_order_detail_log_line(diagnostics)
        if order_detail is not None:
            app_logger.warning(order_detail)
        self._minute_diagnostic_keys.add(key)

    def get_weekly_prices(self, symbol: str, weeks: int) -> list[WeeklyBar]:
        """키움 주봉 API(ka10082)로 종목의 최근 주봉 데이터를 가져옵니다.

        응답 배열 키: stk_stk_pole_chart_qry (일봉과 다름에 주의)
        키움 응답은 최신 → 과거 순이므로 reverse() 후 반환합니다.
        """
        from datetime import date

        base_dt = date.today().strftime("%Y%m%d")

        api_response = self._post(
            endpoint="/api/dostk/chart",
            api_id="ka10082",
            payload={
                "stk_cd": symbol,
                "base_dt": base_dt,
                "upd_stkpc_tp": "1",
            },
        )

        raw_bars = api_response.body.get("stk_stk_pole_chart_qry", [])
        bars: list[WeeklyBar] = []

        for item in raw_bars:
            bars.append(
                WeeklyBar(
                    date=str(item.get("dt", "")),
                    open_price=self._parse_abs_int(item.get("open_pric")),
                    high_price=self._parse_abs_int(item.get("high_pric")),
                    low_price=self._parse_abs_int(item.get("low_pric")),
                    close_price=self._parse_abs_int(item.get("cur_prc")),
                    volume=self._parse_abs_int(item.get("trde_qty")),
                )
            )

        bars.reverse()
        return bars[-weeks:] if len(bars) > weeks else bars

    def get_token(self) -> str:
        """WebSocket 로그인에 사용할 접근 토큰을 반환합니다."""
        return self.access_token or ""

    def get_market_price(self, symbol: str) -> MarketPrice:
        """한 종목의 현재 시세를 조회합니다.

        현재 구현은 사용자가 직접 성공시킨
        '주식기본정보요청(ka10001)' 기반입니다.

        Parameters
        ----------
        symbol:
            예: '005930'

        Returns
        -------
        MarketPrice
            내부 표준 시세 객체
        """

        api_response = self._post(
            endpoint="/api/dostk/stkinfo",
            api_id="ka10001",
            payload={"stk_cd": symbol},
        )

        # 키움 응답은 가격 문자열에 +, - 부호가 붙는 경우가 많습니다.
        # 예: '-218750'
        # 이때 현재가 자체를 음수 가격으로 쓰면 안 되므로 절대값으로 변환합니다.
        current_price = self._parse_abs_int(api_response.body.get("cur_prc"))
        base_price = self._parse_abs_int(api_response.body.get("base_pric"))
        previous_close = base_price

        return MarketPrice(
            symbol=symbol,
            current_price=current_price,
            reference_price=base_price,
            previous_close=previous_close,
            timestamp=datetime.now(),
        )

    def get_account_balance(self) -> AccountBalance:
        """계좌 현금과 보유 종목을 함께 조회합니다.

        키움 REST 에서는 한 번의 호출로 모든 정보가 깔끔하게 정리되지 않을 수 있어,
        여기서는 다음 두 API를 조합합니다.

        1. kt00001 예수금상세현황요청
           - 주문 가능 금액, 예수금 관련 값
        2. kt00018 계좌평가잔고내역요청
           - 실제 보유 종목 목록, 평균단가, 수량

        Returns
        -------
        AccountBalance
            내부 표준 계좌 객체
        """

        deposit_response = self._post(
            endpoint="/api/dostk/acnt",
            api_id="kt00001",
            payload={"qry_tp": "3"},
            cont_yn="N",
            next_key="",
        )

        holdings_response = self._post(
            endpoint="/api/dostk/acnt",
            api_id="kt00018",
            payload={"qry_tp": "1", "dmst_stex_tp": "KRX"},
            cont_yn="N",
            next_key="",
        )

        cash = self._parse_abs_int(deposit_response.body.get("ord_alow_amt"))
        total_asset = self._parse_abs_int(holdings_response.body.get("prsm_dpst_aset_amt"))

        positions: list[Position] = []
        for item in holdings_response.body.get("acnt_evlt_remn_indv_tot", []):
            raw_symbol = str(item.get("stk_cd", "")).strip()
            symbol = raw_symbol[1:] if raw_symbol.startswith("A") else raw_symbol
            quantity = self._parse_abs_int(item.get("rmnd_qty"))
            average_price = self._parse_abs_int(item.get("pur_pric"))

            if symbol and quantity > 0:
                positions.append(
                    Position(
                        symbol=symbol,
                        quantity=quantity,
                        average_price=average_price,
                    )
                )

        return AccountBalance(cash=cash, total_asset=total_asset, positions=positions)

    def get_daily_prices(self, symbol: str, days: int) -> list[PriceBar]:
        """키움 일봉 API(ka10081)로 종목의 최근 일봉 데이터를 가져옵니다.

        키움 ka10081: 주식일봉차트조회
        - stk_cd       : 종목코드
        - base_dt      : 기준일자 (YYYYMMDD) — 오늘 날짜로 설정
        - upd_stkpc_tp : 수정주가구분 (1 = 수정주가 적용)

        응답의 'stk_dt_pole_chart_qry' 리스트를 파싱합니다.
        키움 응답은 최신 날짜가 앞에 오므로 reverse() 후 반환합니다.
        """
        from datetime import date

        base_dt = date.today().strftime("%Y%m%d")

        api_response = self._post(
            endpoint="/api/dostk/chart",
            api_id="ka10081",
            payload={
                "stk_cd": symbol,
                "base_dt": base_dt,
                "upd_stkpc_tp": "1",
            },
        )

        raw_bars = api_response.body.get("stk_dt_pole_chart_qry", [])
        bars: list[PriceBar] = []

        for item in raw_bars:
            bars.append(
                PriceBar(
                    date=str(item.get("dt", "")),
                    open_price=self._parse_abs_int(item.get("open_pric")),
                    high_price=self._parse_abs_int(item.get("high_pric")),
                    low_price=self._parse_abs_int(item.get("low_pric")),
                    close_price=self._parse_abs_int(item.get("cur_prc")),
                    volume=self._parse_abs_int(item.get("trde_qty")),
                )
            )

        # 키움은 최신 → 과거 순이므로 뒤집어서 과거 → 최신 순으로 반환
        bars.reverse()

        # 요청한 days 수만큼만 잘라서 반환
        return bars[-days:] if len(bars) > days else bars

    def place_order(self, order: OrderRequest) -> OrderResult:
        """매수 또는 매도 주문을 키움 REST API로 전송합니다.

        현재는 사용자가 직접 테스트해서 성공한
        - kt10000: 매수
        - kt10001: 매도
        를 사용합니다.

        Parameters
        ----------
        order:
            내부 표준 주문 요청 객체

        Returns
        -------
        OrderResult
            브로커가 주문을 어떻게 처리했는지 요약한 결과
        """

        api_id = "kt10000" if order.side == OrderSide.BUY else "kt10001"

        # ver1 은 단순하게 '시장가' 중심으로 갑니다.
        # 키움 요청값에서 trde_tp='3' 은 시장가 예시로 검증했습니다.
        payload = {
            "dmst_stex_tp": "KRX",
            "stk_cd": order.symbol,
            "ord_qty": str(order.quantity),
            "ord_uv": "" if order.order_type == "market" else str(order.price or ""),
            "trde_tp": "3" if order.order_type == "market" else "0",
            "cond_uv": "",
        }

        # 2026-08-14 (1P0.8-P0.1/P0.2, 319400 실측 P0 사고 대응): 이전엔
        # _post()가 던진 예외가 여기서 그대로 place_order() 밖으로
        # 전파돼, 호출부(TradingService)가 이미 SELL_PENDING/BUY_PENDING
        # 으로 바꿔놓은 상태를 롤백할 방법이 없었습니다(8/14 319400
        # 종목 SELL이 kt10001 HTTP 429로 실패 → 109분간 청산 불가 →
        # -1.37%였을 손절이 -6.4%까지 확대). place_order()는 이제
        # 주문 전송 과정에서 분류 가능한 HTTP/transport failure를
        # 예외로 전파하지 않고 OrderResult로 반환합니다(access token
        # 누락처럼 프로그래밍 오류에 가까운 사전조건 실패는 여전히
        # 예외로 남습니다 — 이런 것까지 조용히 삼켜 OrderResult로
        # 위장하면 오히려 원인 파악이 어려워집니다).
        # 2026-08-14 (1P0.8-P0.3, 민우님 확정 범위): 429/1700
        # whitelist에 해당하는 경우에만, 짧은 bounded retry(최대
        # PLACE_ORDER_RATE_LIMIT_MAX_RETRIES회)를 허용합니다. 그 외
        # 모든 실패(timeout/connection reset/408/5xx/형태가 다른
        # 429)는 재시도 없이 즉시 반환합니다 — retry는 "서버가
        # 명시적으로 처리하지 않았다고 응답한" 케이스에만 안전하고,
        # 응답이 불명확한 케이스에 재시도를 걸면 원 주문이 실제로는
        # 살아있을 때 중복 주문을 만들 위험이 있기 때문입니다.
        rate_limit_retries_used = 0
        while True:
            try:
                response = self._post(
                    endpoint="/api/dostk/ordr",
                    api_id=api_id,
                    payload=payload,
                    cont_yn="N",
                    next_key="",
                    raise_on_business_error=False,
                )
                break
            except KiwoomHttpError as exc:
                # 2026-08-14 (1P0.8-P0.1 재검토, GPT 코드리뷰 지적 — P0):
                # "HTTP 응답을 받았다"는 사실만으로는 definitive reject를
                # 보장하지 않습니다(KiwoomHttpError 클래스 docstring
                # 참고). 오늘 실측으로 확인된 429+1700 shape만 whitelist
                # 하고, 그 외(408/5xx 및 형태가 다른 429 포함)는
                # is_ambiguous=True로 fail-close합니다 — 재시도도
                # 하지 않고 즉시 반환합니다(이 shape은 재시도해도
                # "미접수가 확정됐다"는 근거가 없어 안전성이 없음).
                if not _is_confirmed_rate_limit_reject(exc.status_code, exc.body):
                    return OrderResult(
                        order_id="",
                        symbol=order.symbol,
                        side=order.side,
                        requested_quantity=order.quantity,
                        accepted=False,
                        message=f"HTTP {exc.status_code}: {exc.body}",
                        timestamp=datetime.now(),
                        is_ambiguous=True,
                    )

                # whitelist(429+1700) 케이스 — 재시도 여력이 남아
                # 있으면 짧게 대기 후 재시도합니다. 소진됐으면 지금까지
                # 해오던 대로 definitive reject로 확정합니다.
                if rate_limit_retries_used >= PLACE_ORDER_RATE_LIMIT_MAX_RETRIES:
                    return OrderResult(
                        order_id="",
                        symbol=order.symbol,
                        side=order.side,
                        requested_quantity=order.quantity,
                        accepted=False,
                        message=(
                            f"HTTP {exc.status_code}: {exc.body} "
                            f"(429/1700 재시도 {rate_limit_retries_used}회 모두 실패)"
                        ),
                        timestamp=datetime.now(),
                    )

                rate_limit_retries_used += 1
                import logging

                logging.getLogger("app").warning(
                    f"[ORDER_RATE_LIMIT_RETRY] {order.symbol} | side={order.side.value} | "
                    f"api_id={api_id} | {rate_limit_retries_used}/{PLACE_ORDER_RATE_LIMIT_MAX_RETRIES}"
                    f"번째 429(1700) 재시도 — {PLACE_ORDER_RATE_LIMIT_RETRY_BACKOFF_SECONDS}초 대기 후 재전송"
                )
                self._retry_sleep(PLACE_ORDER_RATE_LIMIT_RETRY_BACKOFF_SECONDS)
                continue
            except KiwoomTransportError as exc:
                # 응답 자체를 못 받음 — 실제 접수 여부 불명. 재시도
                # 대상이 아닙니다(whitelist는 "명시적으로 응답받은
                # 429/1700"만 다룸). is_ambiguous=True로 표시해
                # 호출부가 절대 자동 롤백/재주문하지 않도록 합니다.
                return OrderResult(
                    order_id="",
                    symbol=order.symbol,
                    side=order.side,
                    requested_quantity=order.quantity,
                    accepted=False,
                    message=str(exc),
                    timestamp=datetime.now(),
                    is_ambiguous=True,
                )

        accepted = response.body.get("return_code") == 0
        message = str(response.body.get("return_msg", ""))
        order_id = str(response.body.get("ord_no", ""))

        return OrderResult(
            order_id=order_id,
            symbol=order.symbol,
            side=order.side,
            requested_quantity=order.quantity,
            accepted=accepted,
            message=message,
            timestamp=datetime.now(),
        )

    def _post(
        self,
        endpoint: str,
        api_id: str,
        payload: dict[str, Any],
        cont_yn: str = "N",
        next_key: str = "",
        raise_on_business_error: bool = True,
    ) -> KiwoomApiResponse:
        """키움 REST POST 호출을 공통 처리하는 내부 헬퍼입니다.

        이 함수의 역할:
        1. 토큰 존재 여부 확인
        2. 키움 공통 헤더 구성
        3. POST 요청 전송
        4. HTTP 응답 파싱
        5. 필요 시 업무 오류(return_code != 0) 예외 처리
        """

        if not self.access_token:
            raise RuntimeError("access token is missing. call authenticate() first.")

        try:
            response = self.session.post(
                f"{self.config.base_url}{endpoint}",
                headers=self._headers(api_id=api_id, cont_yn=cont_yn, next_key=next_key),
                json=payload,
                timeout=10,
            )
        except requests.exceptions.RequestException as exc:
            # 2026-08-14 (1P0.8-P0.1): 응답 자체를 못 받은 전송 실패 —
            # KiwoomHttpError(아래)와 구분되는 KiwoomTransportError로
            # 던집니다. 둘 다 RuntimeError를 상속하므로 기존 호출부의
            # 동작(넓게 RuntimeError/Exception으로 잡는 코드)은 그대로
            # 유지됩니다.
            raise KiwoomTransportError(
                f"kiwoom transport failed: api_id={api_id}: {type(exc).__name__}: {exc}"
            ) from exc
        api_response = self._to_api_response(response)

        if api_response.status_code != 200:
            # 2026-08-14 (1P0.8-P0.1, 319400 실측 P0 사고 대응,
            # P0 재검토로 문구 정정): non-200 응답을 KiwoomHttpError로
            # 분류해 상위 호출부로 전달합니다. 이 응답이
            # DEFINITIVE_REJECT인지 AMBIGUOUS인지의 최종 판정은
            # 여기서 하지 않습니다 — place_order()가
            # _is_confirmed_rate_limit_reject() whitelist 정책으로
            # 판단합니다(자세한 근거는 KiwoomHttpError 클래스
            # docstring 참고).
            raise KiwoomHttpError(
                f"kiwoom request failed: api_id={api_id}, http={api_response.status_code}, body={api_response.body}",
                status_code=api_response.status_code,
                body=api_response.body,
            )

        if raise_on_business_error and api_response.body.get("return_code") != 0:
            raise RuntimeError(f"kiwoom business error: api_id={api_id}, body={api_response.body}")

        return api_response

    def _headers(self, api_id: str, cont_yn: str = "N", next_key: str = "") -> dict[str, str]:
        """키움 REST API 공통 헤더를 만듭니다.

        authorization 헤더는 반드시 'Bearer {token}' 형식이어야 하고,
        api-id 헤더로 어떤 TR 을 호출하는지 지정합니다.
        """

        if not self.access_token:
            raise RuntimeError("access token is missing")

        return {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {self.access_token}",
            "cont-yn": cont_yn,
            "next-key": next_key,
            "api-id": api_id,
        }

    def _to_api_response(self, response: requests.Response) -> KiwoomApiResponse:
        """requests.Response 를 내부 응답 객체로 바꿉니다."""

        try:
            body = response.json()
        except ValueError:
            body = {"raw_text": response.text}

        return KiwoomApiResponse(
            status_code=response.status_code,
            headers={
                "next-key": response.headers.get("next-key", ""),
                "cont-yn": response.headers.get("cont-yn", ""),
                "api-id": response.headers.get("api-id", ""),
            },
            body=body,
        )

    @staticmethod
    def _parse_abs_int(value: Any) -> int:
        """키움 숫자 문자열을 안전하게 정수로 바꿉니다.

        키움 응답은 종종 아래처럼 옵니다.
        - '000000000437250'
        - '-218750'
        - '+225000'
        - ''

        현재가/기준가/수량처럼 '크기'가 중요한 값은 절대값으로 써야 하므로
        여기서는 abs(int(...)) 형태로 처리합니다.
        """

        if value is None:
            return 0

        text = str(value).strip()
        if not text:
            return 0

        try:
            return abs(int(float(text)))
        except ValueError:
            return 0
