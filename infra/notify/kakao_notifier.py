"""
카카오톡 나에게 보내기 알림 모듈.

준비:
1. https://developers.kakao.com 에서 앱 생성
2. settings.yaml kakao 섹션에 토큰 입력
3. 액세스 토큰 만료(6시간) 시 refresh_token으로 자동 갱신
"""
from __future__ import annotations

import json
import logging
import requests

logger = logging.getLogger(__name__)

KAKAO_SEND_URL    = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
KAKAO_REFRESH_URL = "https://kauth.kakao.com/oauth/token"


class KakaoNotifier:
    """카카오톡 나에게 보내기."""

    def __init__(
        self,
        access_token:  str,
        refresh_token: str = "",
        rest_api_key:  str = "",
    ):
        self.access_token  = access_token
        self.refresh_token = refresh_token
        self.rest_api_key  = rest_api_key
        self._enabled      = bool(access_token)

    def send(self, text: str) -> bool:
        """텍스트 메시지를 카카오톡으로 전송합니다."""
        if not self._enabled:
            return False
        try:
            resp = self._send_request(text)
            # 토큰 만료(401) 시 자동 갱신 후 재시도
            if resp.status_code == 401 and self.refresh_token:
                logger.info("[KAKAO] 토큰 만료 — 자동 갱신 시도")
                if self._refresh_access_token():
                    resp = self._send_request(text)
            if resp.status_code == 200:
                return True
            logger.warning(f"[KAKAO] 전송 실패: {resp.status_code} {resp.text[:100]}")
            return False
        except Exception as e:
            logger.warning(f"[KAKAO] 예외 발생: {e}")
            return False

    def _send_request(self, text: str) -> requests.Response:
        return requests.post(
            KAKAO_SEND_URL,
            headers={"Authorization": f"Bearer {self.access_token}"},
            data={
                "template_object": json.dumps({
                    "object_type": "text",
                    "text": text,
                    "link": {"web_url": "", "mobile_web_url": ""},
                })
            },
            timeout=5,
        )

    def _refresh_access_token(self) -> bool:
        if not self.refresh_token or not self.rest_api_key:
            return False
        try:
            resp = requests.post(
                KAKAO_REFRESH_URL,
                data={
                    "grant_type":    "refresh_token",
                    "client_id":     self.rest_api_key,
                    "refresh_token": self.refresh_token,
                },
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json()
                self.access_token = data["access_token"]
                if "refresh_token" in data:
                    self.refresh_token = data["refresh_token"]
                logger.info("[KAKAO] 토큰 갱신 완료")
                return True
        except Exception as e:
            logger.warning(f"[KAKAO] 토큰 갱신 실패: {e}")
        return False


def build_notifier(settings) -> KakaoNotifier:
    """settings에서 KakaoNotifier를 생성합니다. 설정 없으면 비활성."""
    kakao = getattr(settings, "kakao", None)
    if kakao is None:
        return KakaoNotifier(access_token="")
    return KakaoNotifier(
        access_token  = getattr(kakao, "access_token",  "") or "",
        refresh_token = getattr(kakao, "refresh_token", "") or "",
        rest_api_key  = getattr(kakao, "rest_api_key",  "") or "",
    )
