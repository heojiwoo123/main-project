"""
1회성 설정 스크립트: 새로 만든 텔레그램 봇의 TELEGRAM_CHAT_ID를 알아내기 위한 용도.

사용 순서:
1. @BotFather로 봇을 만들고 TELEGRAM_BOT_TOKEN을 발급받아 .env에 저장한다.
2. 텔레그램 앱에서 그 봇을 찾아 아무 메시지나 하나 보낸다 (예: "안녕").
3. 이 스크립트(telebot.py)를 실행한다.
4. 출력된 JSON 안에서 message.chat.id 값을 찾아 TELEGRAM_CHAT_ID로 .env에 저장한다.

이후 프로젝트의 다른 스크립트들은 이 값을 직접 찾을 필요 없이 stock_utils.py의
TELEGRAM_CHAT_ID(환경변수에서 이미 읽어온 값)를 그대로 재사용한다. getUpdates 응답의
원본 구조를 눈으로 직접 확인해보는 게 이 스크립트의 목적이라, stock_utils.py처럼
결과를 가공하지 않고 그대로 출력한다.
"""
import json
import os
import requests
# .env 파일을 읽어오기 위한 라이브러리 import
from dotenv import load_dotenv

# 동일한 폴더에 있는 .env 파일의 내용을 환경 변수로 로드합니다.
load_dotenv()

# os.environ.get()을 통해 안전하게 환경 변수 값을 가져옵니다.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")


# 1. 텔레그램 봇 TELEGRAM_CHAT_ID 정보 알아내기


# getUpdates: 이 봇에게 온 메시지들을 가져오는 API. 아직 아무 설정(offset 등)도 안 한 채로
# 그냥 호출하면, 봇이 받은 메시지들을 최근 것까지 전부 돌려준다.
url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
res = requests.get(url)
print(res)  # <Response [200]> 처럼 상태 코드만 간단히 보여주는 requests의 기본 출력.
if res.status_code == 200:
    # res.text(응답 원문 문자열)를 json.loads()로 파이썬 딕셔너리/리스트로 변환해 그대로 출력한다.
    # 출력된 내용 중 result 리스트 안의 각 항목 → message → chat → id 가 바로 내 TELEGRAM_CHAT_ID다.
    # (직접 보내려는 메시지가 안 보이면, 텔레그램에서 봇에게 메시지를 먼저 보낸 뒤 다시 실행해본다.)
    print(json.loads(res.text))
