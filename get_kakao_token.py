# test_kakao.py
import os

# .env 직접 로드
for line in open('.env', encoding='utf-8').readlines():
    if '=' in line and not line.startswith('#'):
        k, v = line.strip().split('=', 1)
        os.environ[k] = v

from infra.notify.kakao_notifier import KakaoNotifier

notifier = KakaoNotifier(
    access_token  = os.environ.get('KAKAO_ACCESS_TOKEN', ''),
    refresh_token = os.environ.get('KAKAO_REFRESH_TOKEN', ''),
    rest_api_key  = os.environ.get('KAKAO_REST_API_KEY', ''),
)

result = notifier.send("🟢 [테스트] 카카오 알림 연결 성공!")
print("전송 결과:", result)