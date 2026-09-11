"""
카카오톡 나에게 보내기 알림 모듈.

준비:
1. https://developers.kakao.com 에서 앱 생성
2. settings.yaml kakao 섹션에 토큰 입력
3. 액세스 토큰 만료(6시간) 시 refresh_token으로 자동 갱신

2026-09-11: 이 프로그램을 여러 사람이 각자 컴퓨터에서 각자 계좌로
돌리는 경우, 각 컴퓨터의 .env에 그 사람 본인의 카카오 토큰을 넣어야
그 사람 카카오톡으로만 알림이 갑니다(민우님이 각 컴퓨터에 직접
입력해주는 방식 포함). 코드 자체에는 원래도 제3자에게 알림을 보낼
경로가 없지만(카카오 "나에게 보내기" API 자체가 토큰 발급자 본인
에게만 전송), 여러 토큰을 손으로 옮겨 적다 보면 실수로 엉뚱한 토큰
(예: 본인 것)을 넣을 위험은 남아있습니다. verify_account()는 지금
설정된 토큰이 실제로 어느 카카오 계정 것인지 시작 시점에 바로
확인하기 위한 자체 점검용입니다 — 매매 로직과는 무관합니다.

2026-09-11 (GPT reclosure, 2차): 토큰 lifecycle 관련 3가지를 추가로
닫았습니다.
1. client_secret: 2026년 기준 카카오 앱은 "Client Secret 사용함"이
   기본이고, 이 경우 refresh 요청에 client_secret을 같이 보내야
   합니다. 민우님 기존 앱은 이 옵션이 꺼져 있어 지금까지 문제가
   없었지만, 새로 만드는 앱은 켜져 있을 수 있어 선택 파라미터로
   지원을 추가했습니다(비워두면 기존과 동일하게 동작 — 하위호환).
2. 동시성: TradingService(매수/매도 알림)와 시작 알림/계정 자체 점검이
   이제 같은 KakaoNotifier 인스턴스를 공유하므로(app/main.py), 두
   스레드가 거의 동시에 401을 만나 동시에 갱신을 시도할 수 있습니다.
   락으로 직렬화하고, 락을 기다리는 동안 이미 다른 스레드가 갱신을
   끝냈으면 중복 갱신 요청을 보내지 않습니다.
3. 토큰 회전 영속화: 카카오는 refresh 시 기존 refresh_token을 폐기할
   수 있습니다. 갱신된 토큰을 프로세스 메모리에만 두면, 재시작 시
   .env의 예전(이미 폐기된) 값을 다시 읽어 갱신이 실패합니다. .env는
   코드가 직접 고치지 않고(사용자가 손으로 관리하는 파일이라 자동
   수정은 포맷 손상 위험), 대신 .gitignore로 이미 제외된 data/ 아래
   이 프로그램 전용 상태 파일에 원자적으로 저장하고, 다음 시작 시
   .env보다 이 파일을 우선해서 읽습니다. 토큰 값 자체는 로그에 절대
   남기지 않습니다.

2026-09-11 (GPT reclosure, 3차): 2차 reclosure에서 실제로 재현된 두
가지 문제를 마저 닫았습니다.
1. 동시성 재현 버그: send()/verify_account()가 401을 "받은 뒤" 그
   시점의 self.access_token을 stale_token으로 다시 읽고 있었습니다.
   그런데 그 사이(요청을 보내고 응답을 받는 동안) 다른 스레드가 먼저
   갱신을 끝내버리면, 401을 받은 스레드가 읽는 self.access_token은
   이미 "새" 값이 되어버려 _refresh_access_token()의 이중 확인이
   무력화되고(같은 값이라 통과) 불필요한 중복 갱신 요청이 나갑니다
   (검토자가 실제로 2번의 refresh POST를 재현 확인). 요청을 보내기
   "직전"에 실제로 사용한 토큰을 스냅샷해서 _send_request()/
   _user_info_request()에 명시적으로 넘기고, 401 처리도 그 스냅샷
   값으로만 판단하도록 고쳤습니다(응답 이후 self.access_token을 다시
   읽지 않음).
2. 계정 전환 시 상태 파일 오버라이드 버그: 이 프로그램의 data/ 폴더를
   (매매 상태 연속성 때문에) 새 컴퓨터로 그대로 복사해가고, 그 사람이
   자기 .env에 자기 카카오 토큰을 넣어도, 예전 build_notifier()는
   상태 파일이 있으면 무조건 우선했기 때문에 새 사람의 .env 값이
   무시되고 원래(민우님) 계정의 저장된 토큰을 계속 쓰는 문제가
   재현됐습니다 — 여러 사람이 각자 계좌/계정으로 쓰게 하려는 이
   기능의 목적 자체를 무력화하는 문제였습니다. 이제 상태 파일에
   "seed_refresh_token_fingerprint"(최초 .env refresh_token의 SHA-256
   해시 — 원문 저장 아님)를 같이 저장하고, 시작할 때 지금 .env의
   refresh_token 지문이 저장된 지문과 일치할 때만("같은 계정이 정상
   회전됨") 저장된 토큰을 신뢰합니다. 지문이 다르면(다른 사람의 .env로
   교체됨) 옛 상태를 버리고 지금 .env 값으로 새로 시작합니다. 지문
   자체도 로그에 남기지 않습니다.
3. 갱신 실패 로그에 카카오 응답 원문(resp.text)을 그대로 남기던 걸
   제거하고, HTTP 상태코드 + (있으면) 카카오가 응답에 넣어주는
   에러코드(예: KOE322)만 안전하게 남기도록 했습니다 — 카카오 에러
   응답 본문에 민감한 값이 섞여 나오지 않는다는 가정에 기대지 않기
   위함입니다.

2026-09-11 (GPT reclosure, 4차): 3차에서 "응답 원문을 로그에 남기지
않는다"를 _refresh_access_token()의 실패 로그에만 적용하고,
verify_account()/send()의 비-200 실패 로그(resp.text[:100])는 그대로
남겨둔 것을 검토자가 재현·지적했습니다. "토큰 값은 어떤 실패 경로에서도
로그에 남지 않는다"는 계약을 완전히 지키기 위해, 이 두 실패 로그도
_refresh_access_token()과 동일하게 HTTP 상태코드 + 안전한 에러코드만
남기도록 통일했습니다.

2026-09-11 (GPT reclosure, 5차): 4차에서 만든 _safe_kakao_error_code()가
JSON의 error_code/error 필드 값을 검증 없이 그대로 반환하고 있어서,
카카오 서버 응답이 그 필드에 임의 문자열(예: 토큰 값)을 담아 보내면
resp.text는 안 찍혀도 이 필드를 통해 그대로 로그에 노출될 수 있다는
점을 검토자가 재현·지적했습니다("에러코드만 남긴다"는 게 "무엇이든
error/error_code 필드에 있으면 남긴다"가 되어버린 것). 이제 알려진
형식/값만 허용하는 whitelist로 강화했습니다: error_code는 "KOE"+숫자
형식일 때만, code는 정수일 때만, error는 OAuth 표준 에러 문자열
목록에 있을 때만 통과시키고, 그 외에는 전부 버립니다.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

KAKAO_SEND_URL      = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
KAKAO_REFRESH_URL   = "https://kauth.kakao.com/oauth/token"
KAKAO_USER_INFO_URL = "https://kapi.kakao.com/v2/user/me"


def _load_persisted_tokens(token_state_file: str) -> dict | None:
    """토큰 상태 파일에서 이전에 회전된 access/refresh token을 읽습니다.

    파일이 없거나(첫 실행), 손상됐거나, 값이 비어있으면 조용히 None을
    반환합니다 — 이 경우 build_notifier()가 기존처럼 .env 값을 씁니다.
    토큰 값은 절대 로그에 남기지 않습니다.

    2026-09-11 (3차 reclosure): "seed_refresh_token_fingerprint"와
    "schema_version"도 함께 반환합니다 — 호출자(build_notifier)가 지금
    .env의 refresh_token 지문과 비교해서, 다른 계정의 상태 파일을
    실수로 신뢰하지 않도록 판단하는 데 씁니다. 이 함수 자체는 비교
    로직을 갖지 않고 순수하게 파일 내용만 읽어 반환합니다.
    """
    try:
        path = Path(token_state_file)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        access_token  = data.get("access_token")  or ""
        refresh_token = data.get("refresh_token") or ""
        if not access_token and not refresh_token:
            return None
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "seed_refresh_token_fingerprint": data.get("seed_refresh_token_fingerprint") or "",
            "schema_version": data.get("schema_version"),
        }
    except Exception as e:
        logger.warning(f"[KAKAO] 토큰 상태 파일 읽기 실패({e}) — .env 값을 사용합니다")
        return None


def _fingerprint_token(token: str) -> str:
    """refresh_token의 SHA-256 지문을 계산합니다(원문은 절대 저장/로그하지 않음).

    이 지문은 "지금 .env에 어느 계정의 refresh_token이 들어있는지"를
    원문 노출 없이 구분하기 위한 용도로만 씁니다 — 사람이 이 값만
    보고 원래 토큰을 복원할 수 없습니다(단방향 해시). 토큰이 비어있으면
    빈 문자열을 반환합니다(비교 시 항상 불일치로 취급되어, refresh_token이
    없는 설정은 저장된 상태를 신뢰하지 않고 항상 .env 값을 그대로 씁니다).
    """
    token = token or ""
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


_KAKAO_ERROR_CODE_PATTERN = re.compile(r"^KOE\d+$")

# 2026-09-11 (5차 reclosure): 카카오 OAuth 표준 에러 문자열만 로그에 남기는
# 것을 허용하는 whitelist. 이 목록에 없는 값(예: 서버가 실수로 토큰 같은
# 임의 문자열을 echo한 경우)은 절대 로그로 통과시키지 않습니다.
_SAFE_OAUTH_ERROR_VALUES = frozenset({
    "invalid_request",
    "invalid_client",
    "invalid_grant",
    "unauthorized_client",
    "unsupported_grant_type",
    "invalid_scope",
})


def _safe_kakao_error_code(resp: requests.Response) -> str:
    """카카오 에러 응답에서 안전하게 에러코드만 뽑아냅니다.

    카카오 에러 응답 본문(resp.text)을 그대로 로그에 남기지 않기 위한
    용도입니다. 4차 reclosure까지는 JSON의 error_code/error 필드 값을
    검증 없이 그대로 반환했는데, 그 필드 값 자체가 임의 문자열(예: 서버가
    실수로 토큰 값을 echo)일 수 있다는 지적을 5차 reclosure에서 재현·
    반영했습니다 — 이제 "본문에 민감한 값이 섞여 나오지 않는다"는 가정에
    기대지 않고, 알려진 형식/값만 통과시킵니다.
    - error_code: "KOE"+숫자 형식(카카오 공식 에러코드, 예: "KOE322")일
      때만 그대로 반환.
    - code: 정수형일 때만 문자열로 변환해 반환.
    - error: OAuth 표준 에러 문자열 whitelist(_SAFE_OAUTH_ERROR_VALUES)에
      있을 때만 반환.
    위 세 조건에 모두 해당하지 않거나 JSON 파싱에 실패하면 빈 문자열을
    반환합니다(이 경우 호출부는 HTTP 상태코드만 로그에 남깁니다).
    """
    try:
        data = resp.json()
        error_code = data.get("error_code")
        if isinstance(error_code, str) and _KAKAO_ERROR_CODE_PATTERN.match(error_code):
            return error_code
        code = data.get("code")
        if isinstance(code, int):
            return str(code)
        error = data.get("error")
        if isinstance(error, str) and error in _SAFE_OAUTH_ERROR_VALUES:
            return error
        return ""
    except Exception:
        return ""


def mask_kakao_id(kakao_id: str) -> str:
    """카카오 사용자 id를 로그/알림 메시지에 남길 때 뒤 4자리만 보이게 마스킹합니다.

    id 자체가 실제 계정을 특정할 수 있는 정보라, 여러 컴퓨터의 설정이
    맞는지 사람이 눈으로 구분할 수 있을 정도로만 남기고 나머지는 가립니다.
    """
    kakao_id = str(kakao_id or "")
    if len(kakao_id) <= 4:
        return "*" * len(kakao_id)
    return "*" * (len(kakao_id) - 4) + kakao_id[-4:]


class KakaoNotifier:
    """카카오톡 나에게 보내기."""

    def __init__(
        self,
        access_token:  str,
        refresh_token: str = "",
        rest_api_key:  str = "",
        client_secret: str = "",
        token_state_file: str = "",
        seed_refresh_token_fingerprint: str = "",
    ):
        self.access_token     = access_token
        self.refresh_token    = refresh_token
        self.rest_api_key     = rest_api_key
        self.client_secret    = client_secret
        self.token_state_file = token_state_file
        # 2026-09-11 (3차 reclosure): 이 인스턴스를 만들 때 .env에 있던
        # refresh_token의 지문(SHA-256). _persist_tokens()가 상태 파일에
        # 이 값을 함께 저장해서, 다음 시작 시 build_notifier()가 "지금
        # .env가 그때와 같은 계정인지"를 원문 노출 없이 확인할 수 있게
        # 합니다. 토큰이 회전돼도 이 seed 자체는 바뀌지 않습니다(.env는
        # 코드가 고치지 않으므로 .env의 refresh_token은 그대로이기 때문).
        self.seed_refresh_token_fingerprint = seed_refresh_token_fingerprint
        self._enabled         = bool(access_token)
        # 2026-09-11: TradingService와 시작 알림/계정 확인이 같은
        # 인스턴스를 공유하게 되면서, 두 스레드가 거의 동시에 401을
        # 만나 동시에 갱신을 시도할 수 있음 — 직렬화용 락.
        self._refresh_lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        """토큰이 설정되어 알림이 켜져 있는지 (외부에서 읽기 전용으로 확인용)."""
        return self._enabled

    def verify_account(self) -> dict | None:
        """지금 설정된 토큰이 어느 카카오 계정 것인지 확인합니다.

        여러 사람 컴퓨터에 각자 다른 토큰을 넣어줄 때, 그 자리에서 바로
        "맞는 계정에 연결됐는지"를 확인하기 위한 자체 점검용입니다.
        매매 로직과 무관하고, 실패해도 알림 전송 자체를 막지 않습니다
        (send()와 동일하게 fail-open — 이 확인이 안 된다고 프로그램
        시작을 중단시키지 않습니다).

        Returns:
            성공 시 {"id": "12345678", "nickname": "홍길동" 또는 None}.
            토큰이 없거나(비활성) 조회에 실패하면 None.
        """
        if not self._enabled:
            return None
        try:
            # 2026-09-11 (3차 reclosure): 요청을 보내기 "직전"의 토큰을
            # 스냅샷합니다. 401을 받은 "뒤"에 self.access_token을 다시
            # 읽으면, 그 사이 다른 스레드(예: TradingService의 send())가
            # 먼저 갱신을 끝낸 경우 이미 "새" 값을 읽게 되어 아래
            # _refresh_access_token()의 중복 갱신 방지 이중 확인이
            # 무력화됩니다(실제로 재현된 버그) — 반드시 요청 전 스냅샷을
            # 그대로 들고 있다가 판단에 씁니다.
            token_used = self.access_token
            resp = self._user_info_request(token_used)
            if resp.status_code == 401 and self.refresh_token:
                logger.info("[KAKAO] 계정 확인 중 토큰 만료 — 자동 갱신 시도")
                if self._refresh_access_token(token_used):
                    resp = self._user_info_request(self.access_token)
            if resp.status_code != 200:
                # 2026-09-11 (4차 reclosure): _refresh_access_token()과
                # 동일하게, 카카오 응답 원문(resp.text)을 로그에 남기지
                # 않습니다 — 응답 본문에 민감한 값이 섞여 나오지 않는다는
                # 가정에 기대지 않기 위함(3차 reclosure에서 refresh 실패
                # 로그만 닫고 이 두 실패 로그를 놓쳤던 것을 재현 확인 후
                # 마저 닫음).
                error_code = _safe_kakao_error_code(resp)
                suffix = f" | {error_code}" if error_code else ""
                logger.warning(f"[KAKAO] 계정 확인 실패 | HTTP {resp.status_code}{suffix}")
                return None
            data = resp.json()
            kakao_id = str(data.get("id", "")) if data.get("id") is not None else ""
            nickname = (
                (data.get("kakao_account") or {})
                .get("profile", {})
                .get("nickname")
            )
            if not kakao_id:
                return None
            return {"id": kakao_id, "nickname": nickname}
        except Exception as e:
            logger.warning(f"[KAKAO] 계정 확인 중 예외: {e}")
            return None

    def _user_info_request(self, token: str) -> requests.Response:
        return requests.get(
            KAKAO_USER_INFO_URL,
            headers={"Authorization": f"Bearer {token}"},
            params={"property_keys": '["kakao_account.profile"]'},
            timeout=5,
        )

    def send(self, text: str) -> bool:
        """텍스트 메시지를 카카오톡으로 전송합니다."""
        if not self._enabled:
            return False
        try:
            # 2026-09-11 (3차 reclosure): verify_account()와 동일한 이유로
            # 요청 직전 토큰을 스냅샷 — 401 이후 self.access_token을 다시
            # 읽지 않습니다(다른 스레드가 이미 갱신했을 수 있어, 그 경우
            # 중복 갱신 방지 이중 확인이 무력화되는 버그가 있었습니다).
            token_used = self.access_token
            resp = self._send_request(text, token_used)
            # 토큰 만료(401) 시 자동 갱신 후 재시도
            if resp.status_code == 401 and self.refresh_token:
                logger.info("[KAKAO] 토큰 만료 — 자동 갱신 시도")
                if self._refresh_access_token(token_used):
                    resp = self._send_request(text, self.access_token)
            if resp.status_code == 200:
                return True
            # 2026-09-11 (4차 reclosure): verify_account()와 동일한 이유로
            # 카카오 응답 원문을 로그에 남기지 않습니다.
            error_code = _safe_kakao_error_code(resp)
            suffix = f" | {error_code}" if error_code else ""
            logger.warning(f"[KAKAO] 전송 실패 | HTTP {resp.status_code}{suffix}")
            return False
        except Exception as e:
            logger.warning(f"[KAKAO] 예외 발생: {e}")
            return False

    def _send_request(self, text: str, token: str) -> requests.Response:
        return requests.post(
            KAKAO_SEND_URL,
            headers={"Authorization": f"Bearer {token}"},
            data={
                "template_object": json.dumps({
                    "object_type": "text",
                    "text": text,
                    "link": {"web_url": "", "mobile_web_url": ""},
                })
            },
            timeout=5,
        )

    def _refresh_access_token(self, expected_stale_token: str | None = None) -> bool:
        """access_token을 refresh_token으로 갱신합니다.

        Args:
            expected_stale_token: 호출자가 요청을 보내기 "직전"에
                스냅샷해뒀던(그리고 401로 거절당한) access_token — 응답을
                받은 뒤 self.access_token을 다시 읽은 값이 아니라, 실제로
                그 요청에 사용한 값이어야 합니다(2026-09-11 3차 reclosure:
                응답 이후 재조회 시 그 사이 다른 스레드가 이미 갱신을
                끝내버리면 이 이중 확인이 무력화되는 버그가 있었습니다).
                락을 얻은 시점에 이미 self.access_token이 이 값과
                달라져 있다면, 그 사이 다른 스레드(예: TradingService와
                시작 알림이 같은 notifier를 공유할 때)가 이미 갱신을
                끝낸 것이므로 중복으로 카카오에 갱신 요청을 보내지 않고
                성공으로 처리합니다.
        """
        if not self.refresh_token or not self.rest_api_key:
            return False
        with self._refresh_lock:
            if expected_stale_token is not None and self.access_token != expected_stale_token:
                logger.info("[KAKAO] 다른 스레드가 이미 토큰을 갱신함 — 중복 갱신 생략")
                return True
            try:
                data_form = {
                    "grant_type":    "refresh_token",
                    "client_id":     self.rest_api_key,
                    "refresh_token": self.refresh_token,
                }
                # 2026-09-11: 앱의 "Client Secret 사용함"이 켜져 있으면
                # 이 값이 없으면 갱신 자체가 거부됨. 비어있으면(기존 앱처럼
                # 꺼져 있으면) 필드 자체를 보내지 않아 기존 동작과 동일.
                if self.client_secret:
                    data_form["client_secret"] = self.client_secret
                resp = requests.post(KAKAO_REFRESH_URL, data=data_form, timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    self.access_token = data["access_token"]
                    if "refresh_token" in data:
                        self.refresh_token = data["refresh_token"]
                    self._persist_tokens()
                    logger.info("[KAKAO] 토큰 갱신 완료")
                    return True
                # 2026-09-11 (3차 reclosure): 카카오 에러 응답 원문
                # (resp.text)을 그대로 로그에 남기지 않습니다 — 본문에
                # 민감한 값이 섞여 나오지 않는다는 가정에 기대지 않고,
                # HTTP 상태코드와 (파싱 가능하면) 안전한 에러코드만
                # 남깁니다.
                error_code = _safe_kakao_error_code(resp)
                suffix = f" | {error_code}" if error_code else ""
                logger.warning(f"[KAKAO] 토큰 갱신 실패 | HTTP {resp.status_code}{suffix}")
            except Exception as e:
                logger.warning(f"[KAKAO] 토큰 갱신 실패: {e}")
            return False

    def _persist_tokens(self) -> None:
        """회전된 access/refresh token을 재시작 후에도 이어 쓸 수 있게 저장합니다.

        .env는 코드가 직접 고치지 않습니다(사용자가 손으로 관리하는 설정
        파일이라 자동 수정은 포맷 손상·다른 값 덮어쓰기 위험이 있음).
        대신 .gitignore로 이미 제외된 data/ 아래에 이 프로그램 전용 상태
        파일로 저장하고, build_notifier()가 시작 시 .env보다 이 파일을
        우선해서 읽습니다(카카오가 refresh 시 기존 refresh_token을 폐기할
        수 있어, .env의 옛 값을 다시 읽으면 다음 갱신이 실패하기 때문).
        임시 파일에 쓴 뒤 os.replace()로 원자적으로 교체 — 쓰는 도중
        실패해도 기존 파일이 깨지지 않습니다. 토큰 값은 로그에 남기지
        않습니다.

        2026-09-11 (3차 reclosure): "schema_version"과
        "seed_refresh_token_fingerprint"(이 인스턴스를 만들 때 .env에
        있던 refresh_token의 SHA-256 지문 — 원문 아님)도 함께 저장합니다.
        build_notifier()가 다음 시작 시 지금 .env의 refresh_token 지문과
        이 값을 비교해서, 다른 사람의 .env로 바뀐 경우(계정 전환) 이
        상태 파일을 신뢰하지 않고 버리도록 하기 위함입니다.
        """
        if not self.token_state_file:
            return
        try:
            path = Path(self.token_state_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_name(path.name + ".tmp")
            tmp_path.write_text(
                json.dumps({
                    "schema_version": 2,
                    "seed_refresh_token_fingerprint": self.seed_refresh_token_fingerprint,
                    "access_token":  self.access_token,
                    "refresh_token": self.refresh_token,
                }),
                encoding="utf-8",
            )
            os.replace(tmp_path, path)
        except Exception as e:
            logger.warning(f"[KAKAO] 토큰 상태 저장 실패: {e}")


def build_notifier(settings) -> KakaoNotifier:
    """settings에서 KakaoNotifier를 생성합니다. 설정 없으면 비활성.

    2026-09-11: token_state_file에 이전에 회전된(refresh된) 토큰이
    저장돼 있으면 .env보다 그걸 우선 사용합니다 — 카카오가 refresh 시
    기존 refresh_token을 폐기할 수 있어, 재시작 때마다 .env의 옛 값을
    다시 읽으면 이미 폐기된 토큰으로 갱신을 시도하다 실패할 수 있기
    때문입니다. 상태 파일이 없으면(첫 실행) 기존처럼 .env 값을 그대로
    씁니다.

    2026-09-11 (3차 reclosure): 단, 위 "우선 사용"은 지금 .env의
    refresh_token 지문이 상태 파일에 저장된 seed 지문과 일치할 때만
    적용됩니다 — 즉 "같은 계정이 정상적으로 회전된" 경우로 한정합니다.
    지문이 다르면(예: data/ 폴더를 새 컴퓨터로 그대로 복사해갔는데 그
    사람이 자기 .env에 자기 토큰을 넣은 경우) 상태 파일이 이전
    사람(민우님)의 계정 것이라는 뜻이므로, 이를 버리고 지금 .env 값을
    새 seed로 삼아 fresh하게 시작합니다 — 그렇지 않으면 새 사람의
    .env가 무시되고 계속 예전 계정으로 동작해, 새 사람 카카오톡에는
    알림이 전혀 가지 않는 문제가 재현됐습니다. 지문이 아예 없는(구버전
    상태 파일, .env에 refresh_token이 없는 등) 경우도 안전하게 "불일치"
    로 취급해 신뢰하지 않습니다.
    """
    kakao = getattr(settings, "kakao", None)
    if kakao is None:
        return KakaoNotifier(access_token="")

    access_token     = getattr(kakao, "access_token",     "") or ""
    refresh_token    = getattr(kakao, "refresh_token",    "") or ""
    rest_api_key     = getattr(kakao, "rest_api_key",     "") or ""
    client_secret    = getattr(kakao, "client_secret",    "") or ""
    token_state_file = getattr(kakao, "token_state_file", "") or ""

    # 지금 .env의 refresh_token 지문 — 이 값이 "이 컴퓨터에 지금 설정된
    # 계정"을 원문 노출 없이 식별하는 seed입니다. .env는 코드가 고치지
    # 않으므로, 토큰이 회전돼도 이 지문 자체는 재시작 사이에 바뀌지
    # 않습니다(같은 계정이라면).
    seed_fingerprint = _fingerprint_token(refresh_token)

    if token_state_file:
        persisted = _load_persisted_tokens(token_state_file)
        if persisted and seed_fingerprint and persisted.get("seed_refresh_token_fingerprint") == seed_fingerprint:
            access_token  = persisted["access_token"]  or access_token
            refresh_token = persisted["refresh_token"] or refresh_token
        elif persisted:
            logger.info(
                "[KAKAO] 저장된 토큰 상태의 계정 지문이 지금 .env와 다릅니다 "
                "— 이전 상태를 버리고 .env 값으로 새로 시작합니다"
            )

    return KakaoNotifier(
        access_token=access_token,
        refresh_token=refresh_token,
        rest_api_key=rest_api_key,
        client_secret=client_secret,
        token_state_file=token_state_file,
        seed_refresh_token_fingerprint=seed_fingerprint,
    )


def _run_startup_check_and_notify(
    notifier: KakaoNotifier,
    app_logger,
    mode: str,
    now_str: str,
    condition_seqs,
) -> None:
    """계정 자체 점검 + 시작 알림 전송의 실제 작업 본체.

    2026-09-11 GPT reclosure: 원래 이 로직이 trading/WebSocket 시작 전에
    동기식으로 실행돼, verify_account()의 HTTP 호출(최악 401→refresh→
    재조회 3회, 5초 timeout씩 최대 약 15초)이 trading startup critical
    path를 지연시킬 수 있다는 지적을 받았습니다. 이 함수 자체는 여전히
    동기식(블로킹)이지만, 반드시 `send_startup_notification_async()`를
    통해 별도 스레드에서만 호출해서 매매 시작과 완전히 분리합니다.
    (테스트에서는 스레드 없이 이 함수를 직접 동기 호출해 결정론적으로
    검증합니다.)

    개인정보 처리: 카카오 닉네임은 카카오톡 알림 메시지 본문에만
    포함하고, app_logger(→ app.log → 일일 번들로 외부에 전달됨)에는
    남기지 않습니다. 로그에는 마스킹된 id 또는 성공/실패 여부만
    남깁니다.

    실패(네트워크 오류, 토큰 문제 등)해도 예외를 여기서 전부 흡수하고
    기존과 동일하게 fail-open — 매매 루프에는 어떤 영향도 주지 않고,
    시작 알림 전송 자체는 계속 시도합니다.
    """
    if not notifier.enabled:
        return

    account_line = ""
    try:
        info = notifier.verify_account()
    except Exception as e:
        # verify_account()가 내부에서 이미 예외를 흡수하지만, 백그라운드
        # 스레드에서 어떤 경로로든 예외가 새어 나와도 스레드 하나가
        # 조용히 죽는 것 이상으로 프로그램에 영향이 없도록 한 번 더 방어.
        app_logger.warning(f"[KAKAO] 계정 확인 중 예외: {e}")
        info = None

    if info:
        masked_id = mask_kakao_id(info.get("id", ""))
        nickname  = info.get("nickname") or "(닉네임 미확인)"
        account_line = f"\n📱 알림 수신 계정: {nickname} (id {masked_id})"
        # 닉네임은 로그에 남기지 않음 — 마스킹된 id와 성공 여부만.
        app_logger.info(f"[KAKAO] 알림 대상 계정 확인 완료 | id {masked_id}")
    else:
        app_logger.warning(
            "[KAKAO] 알림 대상 계정을 확인하지 못했습니다(토큰 문제 가능) "
            "— 알림 전송 자체는 시도합니다."
        )

    try:
        notifier.send(
            f"🚀 자동매매 시작\n"
            f"시각: {now_str} | 모드: {mode}\n"
            f"감시 종목: 조건검색식 {condition_seqs}"
            f"{account_line}"
        )
    except Exception as e:
        app_logger.warning(f"[KAKAO] 시작 알림 전송 중 예외: {e}")


def send_startup_notification_async(
    notifier: KakaoNotifier,
    app_logger,
    mode: str,
    now_str: str,
    condition_seqs,
) -> None:
    """계정 자체 점검 + 시작 알림 전송을 매매/WebSocket 시작을 막지 않는
    백그라운드 스레드에서 실행합니다. 이 함수 자체는 스레드만 띄우고
    즉시 반환하므로 trading startup critical path를 지연시키지 않습니다.
    """
    threading.Thread(
        target=_run_startup_check_and_notify,
        args=(notifier, app_logger, mode, now_str, condition_seqs),
        daemon=True,
        name="kakao-startup-notify",
    ).start()
