from __future__ import annotations

"""실전 계좌 전용 토큰 발급기.

조건검색 WebSocket은 실전 계좌로 연결해야 하므로
모의투자 브로커와 별도로 실전 계좌 토큰을 발급받습니다.
"""

import requests


def fetch_real_token(app_key: str, secret_key: str) -> str:
    """실전 계좌 앱키/시크릿키로 접근 토큰을 발급받습니다.

    Returns
    -------
    str : 접근 토큰 문자열
    """
    response = requests.post(
        "https://api.kiwoom.com/oauth2/token",
        json={
            "grant_type": "client_credentials",
            "appkey": app_key,
            "secretkey": secret_key,
        },
        timeout=10,
    )

    body = response.json()

    if response.status_code != 200 or body.get("return_code") != 0:
        raise RuntimeError(
            f"실전 계좌 토큰 발급 실패: http={response.status_code}, body={body}"
        )

    token = body.get("token")
    if not token:
        raise RuntimeError(f"토큰이 응답에 없습니다: {body}")

    return token
