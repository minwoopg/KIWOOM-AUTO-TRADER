"""키움 모의투자 토큰 발급 단독 테스트 파일.

프로젝트 브로커를 바로 실행하기 전에,
앱키/시크릿키가 정상인지 빠르게 점검하고 싶을 때 사용합니다.
"""

import os
import requests

BASE_URL = "https://mockapi.kiwoom.com"
APP_KEY = os.getenv("KIWOOM_APP_KEY", "")
SECRET_KEY = os.getenv("KIWOOM_SECRET_KEY", "")


def request_token() -> None:
    """모의투자 토큰 발급을 요청합니다."""

    payload = {
        "grant_type": "client_credentials",
        "appkey": APP_KEY,
        "secretkey": SECRET_KEY,
    }
    response = requests.post(f"{BASE_URL}/oauth2/token", json=payload, timeout=10)
    print("status:", response.status_code)
    print("body :", response.text)


if __name__ == "__main__":
    request_token()
