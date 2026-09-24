"""
내가 만든 텔레그램 봇으로 메시지를 보내보는 첫 스크립트.

먼저 telebot.py로 TELEGRAM_CHAT_ID를 알아내 .env에 적어 둔 다음 이 파일을 실행하면,
봇이 내 텔레그램으로 메시지를 보낸다. 아직 주가는 다루지 않는다 — 보내는 통로부터 만든다.

`python notify_stock_price.py`로 직접 실행한다.
"""
import os
import requests
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def send_telegram_message(text: str) -> bool:
    """텔레그램 sendMessage API로 텍스트 메시지를 전송합니다."""
    # 텔레그램 Bot API는 "https://api.telegram.org/bot{토큰}/{기능이름}" 형태의 URL로 호출한다.
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # parse_mode: "HTML"로 지정하면 text 안의 <b>굵게</b> 같은 간단한 HTML 태그가 실제로
    # 굵게/기울임 등으로 렌더링된다.
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        response = requests.post(url, data=data, timeout=15)
        #(예: 정상 성공은 200, 잘못된 요청은 400, 서버 권한 오류는 401 등)
        return response.status_code == 200
    except Exception as e:
        print(f"❌ 텔레그램 메시지 전송 중 오류 발생: {e}")
        return False


if send_telegram_message("안녕하세요! 제 봇이 보낸 첫 메시지입니다 🎉"):
    print("✅ 텔레그램으로 메시지를 전송했습니다!")
else:
    print("❌ 메시지 전송에 실패했습니다. 토큰과 CHAT_ID를 다시 확인해 주세요.")
