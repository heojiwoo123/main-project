"""
관심종목(watchlist.csv)에 적어 둔 종목들의 현재가를 네이버 API로 한 번에 조회해
텔레그램 메시지 하나로 보내는 스크립트.

지난주까지는 종목 하나를 코드에 직접 적어 두고 조회했지만, 이번 주부터는 watchlist.csv를
읽어 그 안의 종목을 차례로 순회한다 — 종목을 늘리거나 줄일 때 코드를 고치지 않고 CSV 파일만
바꾸면 된다.

send_price_notification()으로 알림 로직 전체를 함수 하나에 감싸 두었다.
`python notify_stock_price.py`로 직접 실행하면 그 함수가 그대로 실행된다.
"""
import os
import time
import requests
from dotenv import load_dotenv

# pandas는 read_watchlist() 안에서 그때그때 import한다.
# 파일을 열자마자 무거운 패키지를 읽어들이지 않으려는 것이다.

WATCHLIST_FILE = "watchlist.csv"

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def read_watchlist(path: str = WATCHLIST_FILE) -> list:
    """watchlist.csv를 읽어 종목코드 리스트를 반환합니다. 실패 시 None."""
    if not os.path.exists(path):
        print(f"❌ '{path}' 파일이 존재하지 않습니다.")
        return None

    try:
        import pandas as pd

        # pd.read_csv()는 CSV 파일을 읽어 표 형태의 자료구조인 DataFrame으로 돌려준다.
        # dtype={"code": str}을 안 주면 pandas가 "005930"을 숫자로 착각해 앞자리 0을
        # 없애버린다(5930) — 앞자리에 0이 있는 종목코드가 깨지지 않도록 문자열로 강제한다.
        watchlist_df = pd.read_csv(path, dtype={"code": str})

        # 컬럼명의 공백을 제거하고 소문자로 통일하여 'code' 컬럼을 찾습니다.
        watchlist_df.columns = [col.strip().lower() for col in watchlist_df.columns]

        if "code" not in watchlist_df.columns:
            print("❌ CSV 파일에 'code' 컬럼이 존재하지 않습니다.")
            return None

        # DataFrame의 "code" 열(Series)을 파이썬 기본 리스트로 변환한다.
        codes = watchlist_df["code"].tolist()
        print(f"📋 읽어온 관심 종목 리스트: {codes}")
        return codes

    except Exception as e:
        print(f"❌ CSV 파일을 읽는 동안 오류가 발생했습니다: {e}")
        return None


def fetch_naver_current_price(code: str, retries: int = 2) -> dict:
    """네이버 금융 비공식 API로 종목의 현재가(장중) 또는 최근 종가(장마감)를 조회합니다.

    순간적인 네트워크 오류에 대비해 최대 retries회까지 재시도합니다 — 한 번 실패했다고
    그 종목을 건너뛰면 알림에서 통째로 빠져 버리기 때문입니다.

    조회 자체가 실패했을 때뿐 아니라, 응답은 왔지만 가격을 숫자로 읽지 못했을 때(0원)도
    0이 담긴 dict 대신 None을 반환합니다 — 호출부가 "실패"로 명확히 구분할 수 있게.

    반환 dict에는 가격·등락률·장 개장 여부와 함께 그 가격이 체결된 시각("traded_at")도
    담깁니다 — 조회 시각이 아니라 체결 시각이라 저장할 거래일을 정하는 기준으로 쓸 수 있습니다."""
    url = f"https://m.stock.naver.com/api/stock/{code}/basic"
    # 왜 재시도가 필요한가: 여기서 실패하면 그 종목은 이번 알림에서 통째로 빠진다. 순간적인
    # 네트워크 지연이나 일시적인 오류 때문에 그런 일이 생기는 걸 막으려고 최소한의
    # 재시도(기본 2회)를 넣었다.
    for attempt in range(1, retries + 1):
        try:
            # requests.get()으로 이 주소에 HTTP GET 요청을 보낸다. User-Agent 헤더가 없으면 일부
            # 서버가 "브라우저가 아닌 요청"으로 판단해 응답을 거부하기도 해서 브라우저인 척 흉내낸다.
            # timeout=3: 3초 안에 응답이 없으면 기다리지 않고 바로 예외를 발생시킨다(무한 대기 방지).
            response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=3)
            if response.status_code != 200:
                print(f"❌ [{code}] 네이버 현재가 조회 실패 (응답 코드: {response.status_code}, {attempt}/{retries}번째 시도)")
            else:
                # JSON(JavaScript Object Notation)은 API가 데이터를 주고받을 때 가장 흔히 쓰는 텍스트
                # 형식이다. 생김새가 파이썬의 딕셔너리·리스트와 거의 그대로 대응된다:
                #   {"stockName": "삼성전자", "closePrice": "71,000", "marketStatus": "OPEN"}
                # 위 문자열이 바로 JSON이고, response.json()은 이 문자열을 실제 파이썬 딕셔너리로
                # 변환해준다 — 그 뒤로는 data["stockName"]처럼 평범한 딕셔너리 다루듯 쓸 수 있다.
                data = response.json()
                # data.get("closePrice", "0"): "closePrice" 키가 없으면 기본값 "0"을 쓴다(에러 방지).
                # 네이버 응답은 가격에 천단위 콤마가 찍혀 있어서("70,000") 숫자로 바꾸기 전에 지운다.
                price_str = data.get("closePrice", "0").replace(",", "")
                # 삼항 표현식(조건부 표현식): "조건이 참이면 A, 아니면 B"를 한 줄로 쓴 것.
                # price_str.isdigit()은 문자열이 숫자로만 이루어졌는지 확인 — 혹시 이상한 값이
                # 와도 int() 변환 중 프로그램이 멈추지 않고 0으로 처리하고 넘어가게 한다.
                price = int(price_str) if price_str.isdigit() else 0
                # 0원짜리 결과는 절대 그대로 돌려주지 않는다. 파싱에 실패했을 뿐인데 값이 있는 것처럼
                # 넘기면 텔레그램에 "0원"이라는 멀쩡해 보이는 가격이 표시되고, 그 값으로 그래프까지
                # 그려지면 전일 대비 계산이 통째로 깨진다.
                # 그래서 "조회 실패"와 똑같이 취급해 (재시도 후에도 안 되면) None을 반환하게 만든다.
                if price <= 0:
                    print(f"❌ [{code}] 가격을 숫자로 읽지 못했습니다 "
                          f"(closePrice: {data.get('closePrice')!r}, {attempt}/{retries}번째 시도)")
                else:
                    return {
                        "code": code,
                        "name": data.get("stockName", "알 수 없음"),
                        "price": price,
                        # "fluctuationsRatio"가 없거나 빈 문자열("")이면 or 뒤의 "0"을 대신 쓴다.
                        "rate": float(data.get("fluctuationsRatio", "0") or "0"),
                        "is_open": data.get("marketStatus") == "OPEN",
                        # localTradedAt: 네이버가 주는 실제 체결(갱신) 시각. "2026-07-29T16:10:20+09:00"
                        # 형태이고 KST 오프셋(+09:00)까지 붙어 있다. 조회 시각이 아니라 체결 시각이라
                        # 자정을 넘겨 조회해도 그 종목이 마지막으로 거래된 날을 그대로 가리킨다 —
                        # 나중에 종가를 날짜별로 기록하게 되면, "언제 실행됐는가"가 아니라
                        # "이 가격이 언제 체결된 것인가"를 기준으로 날짜를 정하는 데 쓴다.
                        "traded_at": data.get("localTradedAt"),
                    }
        except Exception as e:
            print(f"❌ [{code}] 네이버 현재가 조회 중 오류 발생: {e} ({attempt}/{retries}번째 시도)")

        if attempt < retries:
            time.sleep(1)  # 순간적인 오류일 수 있으니 짧게 대기 후 재시도

    return None


def send_telegram_message(text: str) -> bool:
    """텔레그램 sendMessage API로 텍스트 메시지를 전송합니다."""
    # 텔레그램 Bot API는 "https://api.telegram.org/bot{토큰}/{기능이름}" 형태의 URL로 호출한다.
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # parse_mode: "HTML"로 지정하면 text 안의 <b>굵게</b> 같은 간단한 HTML 태그가 실제로
    # 굵게/기울임 등으로 렌더링된다.
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        response = requests.post(url, data=data, timeout=15)
        return response.status_code == 200
    except Exception as e:
        print(f"❌ 텔레그램 메시지 전송 중 오류 발생: {e}")
        return False


def format_rate_badge(price: int, rate: float) -> str:
    """가격과 등락률(%)을 "가격원 세모이모지 부호율%" 형태의 문자열로 변환합니다
    (예: "254,000원 🔺 +1.2%", "254,000원 ▼ -0.5%"). 세모 이모지 바로 앞에 가격 숫자를
    붙입니다. 텔레그램 텍스트 메시지와 사진 캡션이 동일한 표기를 쓰도록 공용으로 뺐습니다."""
    prefix = f"{price:,}원"
    if rate > 0:
        return f"{prefix} 🔺 +{rate}%"
    if rate < 0:
        return f"{prefix} ▼ {rate}%"
    return f"{prefix} ▫️ 0.0%"


def send_price_notification() -> bool:
    """관심종목의 현재가를 텔레그램 메시지 하나로 전송합니다.

    한 종목이라도 전송했으면 True, 아무것도 하지 못했으면 False를 반환합니다.
    """
    watchlist_codes = read_watchlist()
    if watchlist_codes is None:
        return False

    print("🚀 관심종목 현재가 조회 시작...")

    # 메시지는 헤더 한 줄로 시작해, 종목을 하나씩 조회할 때마다 두 줄씩 이어 붙인다.
    telegram_message = "📊 내 관심종목 현재가\n"
    found = 0  # 실제로 조회에 성공한 종목 수

    for code in watchlist_codes:
        info = fetch_naver_current_price(code)
        if info is None:
            # 이 종목만 조회 실패해도 프로그램을 멈추지 않고 다음 종목으로 넘어간다.
            continue

        # 종목마다 두 줄: 첫 줄은 이름과 종목코드, 둘째 줄은 4칸 들여쓴 가격·등락.
        telegram_message += (
            f"\n📈 {info['name']} ({code})"
            f"\n    {format_rate_badge(info['price'], info['rate'])}"
        )
        found += 1

    if found == 0:
        print("❌ 관심종목의 현재가를 하나도 가져오지 못했습니다. 네이버 API 상태를 확인해 주세요.")
        return False

    if send_telegram_message(telegram_message):
        print(f"✅ 현재가 메시지를 텔레그램으로 전송했습니다! ({found}/{len(watchlist_codes)}종목)")
        return True

    print("❌ 현재가 메시지 전송에 실패했습니다.")
    return False


# if __name__ == "__main__": 은 "이 파일을 직접 실행했을 때만" 아래 코드를 돌리라는 뜻이다.
# 다른 스크립트가 `from notify_stock_price import send_price_notification`처럼 함수만
# 가져다 쓰는 경우에는 이 블록이 자동으로 실행되지 않는다.
if __name__ == "__main__":
    send_price_notification()
