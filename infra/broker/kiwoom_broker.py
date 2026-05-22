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

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests

from config.settings import BrokerConfig
from domain.models import AccountBalance, MarketPrice, OrderRequest, OrderResult, OrderSide, Position, PriceBar, WeeklyBar, MinuteBar
from infra.broker.base import Broker


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
        """
        from datetime import date

        base_dt = date.today().strftime("%Y%m%d")

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

        raw_bars = api_response.body.get("stk_min_pole_chart_qry", [])

        # 빈 응답이면 응답 body 키를 로그에 남겨 원인 파악
        if not raw_bars:
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
        return bars

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

        response = self._post(
            endpoint="/api/dostk/ordr",
            api_id=api_id,
            payload=payload,
            cont_yn="N",
            next_key="",
            raise_on_business_error=False,
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

        response = self.session.post(
            f"{self.config.base_url}{endpoint}",
            headers=self._headers(api_id=api_id, cont_yn=cont_yn, next_key=next_key),
            json=payload,
            timeout=10,
        )
        api_response = self._to_api_response(response)

        if api_response.status_code != 200:
            raise RuntimeError(
                f"kiwoom request failed: api_id={api_id}, http={api_response.status_code}, body={api_response.body}"
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
