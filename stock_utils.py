"""
notify_stock_price.py(하루 3회 알림), collect_daily_close.py(하루 1회 종가 수집)가
공통으로 쓰는 함수 모음.

네이버 금융 비공식 API(m.stock.naver.com)는 장중이면 실시간 체결가를, 장 시작 전/마감
후에는 가장 최근 종가를 marketStatus/closePrice 필드로 그대로 돌려주므로, 이 하나의
엔드포인트로 "현재가 조회"와 "일별 종가 기록"을 모두 처리한다.
"""
import os
import io
import re
import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

# pandas/matplotlib은 read_watchlist()/build_price_chart() 안에서 그때그때 import한다.
# 이 모듈에서 텔레그램 관련 함수만 가져다 쓰는 경우에는 이 두 (설치·로딩이 무거운) 패키지가
# 아예 필요 없는데, 모듈 최상단에서 import하면 그런 경우에도 강제로 설치돼 있어야 한다.

WATCHLIST_FILE = "watchlist.csv"
NUM_HISTORY_DAYS = 15
# 시간대를 명시하지 않으면 실행 환경에 따라 날짜가 하루 어긋날 수 있다(특히 밤늦게 실행할 때).
# 항상 이 KST를 명시해서 기준 시간대를 고정한다.
KST = ZoneInfo("Asia/Seoul")

# 이 모듈이 임포트되는 시점에 바로 .env를 로드해야, 아래 os.environ.get() 호출이
# 호출 스크립트의 import 순서와 무관하게 항상 올바른 값을 읽는다.
load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")


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


def get_turso_client():
    """Turso(libSQL) DB 클라이언트를 생성하고, price_history 테이블이 없으면 만듭니다.
    libsql_client는 여기서만 필요한 무거운(네트워크 연결) 의존성이라 모듈 최상단이 아니라
    이 함수 안에서 import한다 (pandas/matplotlib과 동일한 이유, 파일 상단 주석 참고).

    collect_daily_close.py처럼 종목을 여러 번 순회하며 저장하는 경우, 이 함수를 매번 새로
    부르지 않고 한 번만 호출해 얻은 클라이언트를 update_price_history()의 client 인자로
    넘겨 재사용할 수 있다 — 그러면 종목마다 새 연결을 맺고 이 CREATE TABLE을 반복하지 않는다."""
    import libsql_client

    if not TURSO_DATABASE_URL:
        raise RuntimeError("TURSO_DATABASE_URL 환경변수가 설정되지 않았습니다.")

    # turso db show --url이 돌려주는 libsql:// 스킴은 WebSocket(wss://)으로 연결되는데,
    # 학교/사내 방화벽이나 일부 실행 환경에서 WebSocket이 막혀 있으면 에러조차 없이 그냥
    # 멈춰버린다(타임아웃 없이 무한 대기). transaction() API를 쓰지 않아 HTTP만으로도 충분하므로,
    # 항상 https://로 바꿔 접속해 이 문제를 원천적으로 피한다.
    # .replace(old, new, 1)의 마지막 1은 "맨 앞 1개만 바꾸라"는 뜻이다 (문자열 안에 libsql://가
    # 또 나올 일은 없지만, 관례적으로 몇 번까지 바꿀지 명시해두면 의도가 분명해진다).
    url = TURSO_DATABASE_URL.replace("libsql://", "https://", 1)
    client = libsql_client.create_client_sync(url, auth_token=TURSO_AUTH_TOKEN)
    # IF NOT EXISTS: 테이블이 이미 있으면 아무 일도 안 하고, 없을 때만 새로 만든다 — 이 함수가
    # 여러 번 호출돼도(스크립트를 여러 번 실행해도) 안전하다.
    # 스키마: code(종목코드)+date(YYYYMMDD)를 합쳐 하나의 행을 유일하게 식별하는 기본키(PRIMARY
    # KEY)로 지정 — "같은 종목의 같은 날짜"는 딱 하나의 행만 존재할 수 있다는 제약이다.
    client.execute(
        "CREATE TABLE IF NOT EXISTS price_history ("
        "code TEXT NOT NULL, date TEXT NOT NULL, close INTEGER NOT NULL, "
        "PRIMARY KEY (code, date))"
    )
    return client


def load_price_history() -> dict:
    """Turso DB에서 종목별 종가 히스토리를 읽어 {code: [{"date": "YYYYMMDD", "close": 가격}, ...]}
    형태(날짜 오름차순)로 반환합니다. 예전에는 이 함수가 price_history.json 파일을 읽었지만,
    Turso로 옮긴 뒤로는 DB에서 직접 SELECT한다."""
    try:
        # with 문(컨텍스트 매니저): 블록이 끝나면(정상 종료든 예외든) client.close()가 자동으로
        # 호출되어 DB 연결이 확실히 정리된다 — 파일을 open()으로 열었을 때와 같은 원리다.
        with get_turso_client() as client:
            # ORDER BY code, date: 종목코드 순으로, 같은 종목 안에서는 날짜 오름차순으로 정렬해서
            # 받아온다 (그래프를 그릴 때 날짜순 정렬이 이미 돼 있어야 하므로 여기서 미리 정렬한다).
            result = client.execute("SELECT code, date, close FROM price_history ORDER BY code, date")
    except Exception as e:
        print(f"❌ Turso DB에서 종가 히스토리를 읽지 못했습니다: {e}")
        return {}

    history: dict = {}
    # result.rows는 (code, date, close) 튜플들의 목록이다. for 문에서 (code, date, close)로 바로
    # 풀어서(unpacking) 받는다.
    for code, date, close in result.rows:
        # dict.setdefault(key, [])는 "key가 이미 있으면 그 값을 그대로 쓰고, 없으면 빈 리스트를
        # 새로 만들어 넣은 뒤 그걸 반환"한다 — 종목별로 리스트를 매번 존재 확인하며 만드는 코드
        # (if code not in history: history[code] = []) 를 한 줄로 줄인 것이다.
        history.setdefault(code, []).append({"date": date, "close": close})
    return history


def update_price_history(code: str, date_str: str, close_price: int,
                          num_days: int = NUM_HISTORY_DAYS, client=None) -> None:
    """종목의 종가 히스토리에 오늘자 종가를 Turso DB에 즉시 저장합니다. 같은 날짜가 이미 있으면
    덮어쓰고, 종목별로 최근 num_days개만 남기고 오래된 행은 삭제합니다.

    JSON 파일 방식과 달리 이 함수 호출 자체가 DB에 바로 반영되므로(트랜잭션 커밋), 예전의
    load_price_history() → 메모리에서 수정 → save_price_history()로 파일 통째로 다시 쓰기 같은
    별도의 "저장" 단계가 필요 없다 — 이것이 파일 기반 저장과 DB 기반 저장의 핵심 차이다.

    client를 안 넘기면(기본값 None) 이 함수가 알아서 하나 만들고 끝나면 스스로 닫는다 — 종목을
    한 번만 저장할 때 쓰는 방식. collect_daily_close.py처럼 여러 종목을 순회하며 반복 호출할
    때는, 호출부가 get_turso_client()로 미리 만들어둔 클라이언트를 client 인자로 넘겨서 종목마다
    새 연결을 맺지 않고 재사용할 수 있다 — 이때는 클라이언트를 만든 쪽(호출부)이 닫을 책임을
    지므로, 이 함수는 그 클라이언트를 닫지 않는다."""
    # own_client: 이 함수가 클라이언트를 직접 만들었는지(True) 아니면 밖에서 받았는지(False).
    # 직접 만든 경우에만 이 함수가 끝날 때 스스로 닫아야 한다 — 밖에서 받은 클라이언트를 여기서
    # 닫아버리면, 호출부가 다음 종목을 처리할 때 이미 닫힌 연결을 쓰려다 에러가 난다.
    own_client = client is None
    if own_client:
        client = get_turso_client()
    try:
        # "?"는 SQL 쿼리의 자리표시자(placeholder)다. 문자열을 f"...{code}..."처럼 직접
        # 끼워넣지 않고 별도의 리스트로 값을 넘기면, 라이브러리가 안전하게 값을 채워준다
        # (SQL 인젝션 방지 — 종목코드/날짜에 이상한 문자가 섞여 있어도 쿼리 구조가 깨지지 않는다).
        #
        # ON CONFLICT (code, date) DO UPDATE: "upsert"라고 부르는 패턴이다. (code, date)가
        # PRIMARY KEY라서 이미 같은 조합의 행이 있으면 INSERT가 충돌(conflict)나는데, 이 경우
        # 새로 넣는 대신 close 값만 덮어쓴다(UPDATE) — "있으면 수정, 없으면 추가"를 SQL 한
        # 문장으로 처리한다. excluded.close는 "이번에 넣으려던 새 close 값"을 가리킨다.
        client.execute(
            "INSERT INTO price_history (code, date, close) VALUES (?, ?, ?) "
            "ON CONFLICT (code, date) DO UPDATE SET close = excluded.close",
            [code, date_str, close_price],
        )
        # 이 종목(code)의 최근 num_days개보다 오래된 행을 지워 히스토리 길이를 일정하게 유지한다.
        # 안쪽 SELECT가 먼저 "날짜 내림차순으로 정렬해 최신 num_days개의 날짜"를 뽑고,
        # 바깥쪽 DELETE는 그 목록에 없는(NOT IN) 나머지 오래된 행만 지운다 — "남길 것"을 먼저
        # 정하고 "그 외 전부"를 지우는 서브쿼리 활용 패턴이다.
        client.execute(
            "DELETE FROM price_history WHERE code = ? AND date NOT IN ("
            "SELECT date FROM price_history WHERE code = ? ORDER BY date DESC LIMIT ?)",
            [code, code, num_days],
        )
    except Exception as e:
        print(f"❌ [{code}] Turso DB에 종가 저장 실패: {e}")
    finally:
        # own_client일 때만(이 함수가 직접 만들었을 때만) 닫는다 — finally라서 위에서 예외가
        # 나도 반드시 실행된다(연결이 계속 열린 채로 남는 걸 방지).
        if own_client:
            client.close()


def fetch_naver_current_price(code: str, retries: int = 2) -> dict:
    """네이버 금융 비공식 API로 종목의 현재가(장중) 또는 최근 종가(장마감)를 조회합니다.

    순간적인 네트워크 오류에 대비해 최대 retries회까지 재시도합니다. 특히
    collect_daily_close.py가 이 함수의 실패를 그대로 "그날 종가 결측"으로 받아들여
    Turso 히스토리에 되돌릴 수 없는 구멍을 남기기 때문에, 최소한의 재시도로 그 위험을 줄인다.

    조회 자체가 실패했을 때뿐 아니라, 응답은 왔지만 가격을 숫자로 읽지 못했을 때(0원)도
    0이 담긴 dict 대신 None을 반환합니다 — 호출부가 "실패"로 명확히 구분할 수 있게.

    반환 dict에는 가격·등락률·장 개장 여부와 함께 그 가격이 체결된 시각("traded_at")도
    담깁니다 — 조회 시각이 아니라 체결 시각이라 저장할 거래일을 정하는 기준으로 쓸 수 있습니다."""
    url = f"https://m.stock.naver.com/api/stock/{code}/basic"
    # 왜 재시도가 필요한가: 이 함수는 notify_stock_price.py(현재가 알림)와 collect_daily_close.py
    # (종가 기록) 양쪽에서 쓰인다. notify 쪽은 실패해도 다음 알림 때 다시 시도되지만, collect 쪽은
    # 하루에 한 번만 실행되므로 여기서 실패하면 그날 종가는 영원히 기록되지 않는다(하루 지나면
    # 재조회할 방법이 없음). 순간적인 네트워크 지연/오류로 이런 영구 결측이 생기는 걸 막기 위해
    # 최소한의 재시도(기본 2회)를 넣었다.
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
                # 넘기면 텔레그램에 "0원"이라는 멀쩡해 보이는 가격이 표시되고, 그 값이 종가로
                # 저장되기까지 하면 그래프와 전일 대비 계산이 통째로 깨진다.
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
                        # collect_daily_close.py가 "언제 실행됐는가"가 아니라 "이 가격이 언제 체결된
                        # 것인가"를 기준으로 날짜를 정하는 데 쓴다.
                        "traded_at": data.get("localTradedAt"),
                    }
        except Exception as e:
            print(f"❌ [{code}] 네이버 현재가 조회 중 오류 발생: {e} ({attempt}/{retries}번째 시도)")

        if attempt < retries:
            time.sleep(1)  # 순간적인 오류일 수 있으니 짧게 대기 후 재시도

    return None


def fetch_naver_intraday_minutes(code: str, date_str: str = None) -> list:
    """네이버 API로 당일 1분 단위 종가를 조회합니다. 반환값은 시간 오름차순
    [{"time": "HHMM", "price": 가격}, ...]. 실패하거나 아직 장이 시작 전이면 빈 리스트.

    이 API는 과거 데이터를 저장해두는 게 아니라 그 시점까지의 당일 흐름을 즉시 다시 계산해
    돌려주는 방식이라, 우리 쪽에 별도로 저장할 필요가 없다.
    응답이 EUC-KR 인코딩과 표준이 아닌 JSON이 섞여 있어 json.loads 대신 데이터 행만 정규식으로
    추출한다 (헤더의 한글 라벨은 깨지지만 숫자로 된 데이터 행은 영향받지 않는다)."""
    date_str = date_str or datetime.now(KST).strftime("%Y%m%d")
    url = "https://api.finance.naver.com/siseJson.naver"
    params = {"symbol": code, "requestType": 1, "startTime": date_str, "endTime": date_str, "timeframe": "minute"}
    try:
        # params에 담은 딕셔너리는 requests가 자동으로 "?symbol=005930&requestType=1&..." 같은
        # 쿼리스트링으로 바꿔서 URL 뒤에 붙여준다.
        response = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=3)
        if response.status_code != 200:
            print(f"❌ [{code}] 당일 분봉 조회 실패 (응답 코드: {response.status_code})")
            return []
        # 이 API는 표준 JSON이 아니라 EUC-KR 한글이 깨진 상태로 섞여 있어 response.json()이
        # 그대로 안 통한다. 대신 정규식(re.findall)으로 우리가 원하는 형태의 데이터 행만 직접
        # 골라낸다. 정규식 \["(\d{12})",\s*null,\s*null,\s*null,\s*(\d+),\s*\d+,\s*null\] 뜻:
        #   ["202607230905", null, null, null, 71000, ..., null] 같은 한 줄에서
        #   ( ) 로 감싼 두 부분 — 12자리 시각("YYYYMMDDHHMM")과 가격 숫자 — 만 뽑아온다.
        rows = re.findall(r'\["(\d{12})",\s*null,\s*null,\s*null,\s*(\d+),\s*\d+,\s*null\]', response.text)
        if not rows and re.search(r'\["\d{12}"', response.text):
            # 타임스탬프가 찍힌 행은 있는데 우리가 기대한 형식(위 정규식)과 안 맞는 경우 —
            # 장이 아직 안 열려서 데이터가 없는 것과는 다른, API 응답 형식 자체가 바뀐 상황일 수 있다.
            print(f"⚠️ [{code}] 당일 분봉 응답 형식이 예상과 달라 파싱하지 못했습니다. "
                  f"네이버 API 응답 형식이 바뀌었을 수 있습니다.")
        # rows는 [("202607230905", "71000"), ...] 같은 (시각, 가격) 튜플들의 리스트다.
        # 리스트 컴프리헨션으로 각 튜플을 {"time": "HHMM", "price": 정수} 딕셔너리로 바꾼다.
        # timestamp[8:]: 12자리 문자열("YYYYMMDDHHMM")에서 앞 8글자(날짜)를 잘라내고 뒤 4글자
        # (시:분)만 남긴다 — 문자열 슬라이싱.
        minutes = [{"time": timestamp[8:], "price": int(price)} for timestamp, price in rows]
        minutes.reverse()  # 응답이 최신순이라 시간 오름차순으로 뒤집는다
        return minutes
    except Exception as e:
        print(f"❌ [{code}] 당일 분봉 조회 중 오류 발생: {e}")
        return []


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


def send_telegram_photo(photo_buffer: io.BytesIO, caption: str) -> bool:
    """텔레그램 sendPhoto API로 이미지를 전송합니다."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    # 파일 업로드는 requests의 files= 파라미터로 한다(멀티파트 폼 전송). 튜플의 세 값은
    # 각각 (파일이름, 파일 내용, MIME 타입)이다. photo_buffer는 디스크에 저장된 실제 파일이
    # 아니라 build_price_chart()가 메모리 안에 만들어둔 io.BytesIO(바이트 버퍼)인데, requests는
    # 이런 파일과 비슷하게 동작하는 객체("file-like object")도 그대로 파일처럼 다룰 수 있다.
    files = {"photo": ("chart.png", photo_buffer, "image/png")}
    # parse_mode="HTML": 캡션 안에 <b>...</b> 같은 HTML 태그를 서식(굵게 등)으로 해석하게 한다.
    data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML"}
    try:
        response = requests.post(url, data=data, files=files, timeout=15)
        return response.status_code == 200
    except Exception as e:
        print(f"❌ 텔레그램 이미지 전송 중 오류 발생: {e}")
        return False


def describe_price_trend(daily_closes: list, intraday_minutes: list) -> str:
    """그래프에 어떤 데이터가 담겼는지 설명하는 문구를 반환합니다. 그래프를 보여주는 쪽이
    어디든 같은 문구를 쓰도록 함수로 빼 두었습니다. "최근 N일"이라고 말하려면 N을 세어줄 종가 히스토리가
    반드시 있어야 하므로 daily_closes를 먼저 확인합니다."""
    # daily_closes(과거 종가)가 있어야만 "최근 N일"이라고 말할 수 있다. 분봉(intraday_minutes)을
    # 먼저 보면, 히스토리가 하나도 없는 상태에서 장중에 실행됐을 때 len(daily_closes)가 0이라
    # "최근 0일 종가 + 현재가 추이"라는 이상한 문구가 나온다.
    if daily_closes:
        return f"최근 {len(daily_closes)}일 종가 + 현재가 추이"
    # 여기까지 왔다면 종가 히스토리가 아직 없는 상태다(새로 추가한 종목이거나
    # collect_daily_close.py가 한 번도 실행되지 않은 최초 실행). 분봉이라도 있으면 오늘 하루치
    # 추이는 그려지므로 그렇게 안내한다.
    if intraday_minutes:
        return "오늘 현재가 추이 (종가 히스토리 누적 전)"
    # 히스토리도 분봉도 없으면 현재가 한 점만 찍힌다.
    return "현재가 (종가 히스토리 누적 전)"


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


def resolve_today_price(price: int, is_open: bool, intraday_minutes: list,
                        daily_closes: list) -> int:
    """분봉/현재가 조회 결과로 build_price_chart()에 넘길 "오늘" 가격을 결정합니다. 장이
    닫혀있고(is_open=False) 분봉도 없으면(장마감 후~다음 장 시작 전) 지금 조회한 가격은
    DB의 마지막 종가와 같은 값이 필연적이므로 None을 돌려줘, "오늘" 점을 따로 안 그리게
    합니다. 단 daily_closes(과거 종가)가 하나도 없으면 비교할 대상 자체가 없으므로 이 예외는
    적용하지 않고 현재가를 그대로 살립니다. notify_stock_price.py/dashboard.py가 공유합니다."""
    # 삼항 표현식(조건부 표현식): "조건이 참이면 price, 거짓이면 None"을 한 줄로 쓴 것.
    # is_open이 True(장중)면 무조건 살아있는 값이라 그대로 쓰고, is_open이 False라도
    # intraday_minutes가 비어있지 않으면(장마감 직후라 분봉은 남아있는 경우) 마찬가지로 살려둔다.
    # 세 번째 조건(not daily_closes)은 "지금 가격은 마지막 종가와 중복이라 뺀다"는 위 근거가
    # 언제 성립하는지를 따진 것이다 — 비교할 과거 종가가 하나도 없으면 중복될 값 자체가 없으므로
    # 그 근거가 성립하지 않는다. 이때 현재가는 그 종목의 유일한 데이터이므로 반드시 살려야 하고,
    # 버리면 그릴 점이 하나도 없어 축과 격자만 있는 빈 그래프가 만들어진다. 새로 추가한 종목이나
    # collect_daily_close.py가 아직 한 번도 실행되지 않은 최초 실행에서 이 상황이 생긴다.
    # 위 셋 다 아니면(장마감 후~다음 장 시작 전, 히스토리는 있음) None을 돌려준다. 빈 리스트([])는
    # 파이썬에서 False로 취급되므로 intraday_minutes/daily_closes만 써도 "비어있는지" 확인이 된다.
    return price if (is_open or intraday_minutes or not daily_closes) else None


def dedupe_daily_closes(daily_closes: list, today_price: int, intraday_minutes: list) -> list:
    """daily_closes에 오늘 날짜 종가가 섞여 있어도, 분봉/현재가로 "오늘" 구간을 따로 보여줄
    때는(today_price가 있거나 intraday_minutes가 있으면) 제외한 리스트를 반환합니다.
    build_price_chart()와 describe_price_trend() 호출부가 항상 같은 개수를 세도록 공용으로
    뺐습니다. 분봉/현재가 조회 자체가 실패했으면(둘 다 없음) 원본 그대로 돌려줘 정보 손실을
    막습니다."""
    # today_price가 None(오늘 값 없음)이고 intraday_minutes도 비어있으면(둘 다 없음) "오늘"
    # 구간을 아예 안 그리는 상황이므로, 걸러낼 필요 없이 원본 daily_closes를 그대로 돌려준다 —
    # 이 경우 DB에 저장된 오늘 종가가 있어도(장마감 후 이미 수집된 경우) 정보 손실 없이 보여준다.
    if today_price is None and not intraday_minutes:
        return daily_closes
    # 위 조건에 안 걸렸다는 건 "오늘" 구간을 실제로 따로 그린다는 뜻이므로, daily_closes 안에
    # 오늘 날짜(today_str)와 같은 항목이 있으면 제외한다. 리스트 컴프리헨션으로 "오늘 날짜가
    # 아닌 항목만" 새 리스트에 담는다 — 원본 리스트는 그대로 두고 필터링된 복사본을 반환한다.
    today_str = datetime.now(KST).strftime("%Y%m%d")
    return [entry for entry in daily_closes if entry["date"] != today_str]


def _evenly_spaced(start: float, end: float, n: int) -> list:
    """[start, end] 구간 안에 n개의 점을 균등한 간격으로 배치한 좌표 리스트를 반환합니다.
    n=0이면 빈 리스트, n=1이면 구간 시작점(start) 하나만 반환합니다."""
    if n <= 0:
        return []
    if n == 1:
        return [start]
    step = (end - start) / (n - 1)
    return [start + step * i for i in range(n)]


def build_price_chart(daily_closes: list, current_price: int = None,
                      intraday_minutes: list = None) -> io.BytesIO:
    """종목별 최근 N일 종가 + 오늘 추이를 선 그래프로 그려 PNG 이미지 버퍼로 반환합니다.
    daily_closes는 [{"date": "YYYYMMDD", "close": 가격}, ...] (날짜 오름차순).
    intraday_minutes를 넘기면(오늘 분봉, 시간 오름차순) 오늘 구간을 분 단위 선으로 그리고,
    없으면 current_price 한 점만 표시합니다. current_price도 None이면(네이버 API 조회가
    실패한 경우) "오늘" 지점 없이 daily_closes만으로 그립니다 — 이 경우 daily_closes에
    이미 오늘 종가가 들어있어도(장마감 후 collect_daily_close.py가 실행된 뒤라면) 그대로 표시되어
    정보 손실이 없습니다. 반대로 분봉/현재가 조회에 성공하면, daily_closes에 오늘 날짜 종가가
    섞여 있어도 제외하고 그립니다 — "오늘" 구간과 중복 표시되는 것을 막기 위해서입니다."""
    import matplotlib
    matplotlib.use("Agg")  # 화면 출력 없이 이미지 파일(버퍼)로만 저장
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    # 그래프에 한글을 표시하려면 한글 폰트가 필요한데, 실행 환경마다 사정이 다르다:
    # Windows는 맑은 고딕, macOS는 애플고딕이 기본으로 깔려 있지만, 리눅스에는 한글 폰트가
    # 없는 경우가 많아 나눔고딕을 따로 설치해 줘야 한다. 이 프로젝트도 나중에 클라우드(리눅스)
    # 에서 돌릴 때 그렇게 한다.
    #
    # 그렇다고 세 이름을 그냥 다 나열하면, matplotlib이 "이 환경에 없는 이름"마다 findfont 경고를
    # 글자 요소 하나하나에 대해 찍어서 로그가 수백 줄로 뒤덮인다 — 목록에서 쓸 폰트를 하나 찾으면
    # 멈추는 게 아니라 목록 전체를 확인하기 때문이다(그래서 순서를 바꿔도 경고는 그대로다).
    # 그래서 지금 이 환경에 실제로 설치돼 있는 것만 골라서 넘긴다.
    #
    # font_manager.fontManager.ttflist: matplotlib이 시스템에서 찾아낸 폰트 목록. 각 항목의
    # .name이 폰트 이름이라, 집합(set)으로 만들어 두면 "이 이름이 있나?"를 빠르게 확인할 수 있다.
    installed_fonts = {f.name for f in font_manager.fontManager.ttflist}
    korean_fonts = [name for name in ("Malgun Gothic", "NanumGothic", "AppleGothic")
                    if name in installed_fonts]
    # 한글 폰트가 하나도 없으면 빈 리스트가 되므로 기본 sans-serif로 떨어뜨린다. 이때는 한글이
    # 네모(두부)로 깨지고 경고도 그대로 뜨는데, 그건 실제로 알아야 할 문제라 일부러 감추지 않는다.
    plt.rcParams["font.family"] = korean_fonts or ["sans-serif"]
    plt.rcParams["axes.unicode_minus"] = False  # 마이너스 기호 깨짐 방지

    # "오늘" 구간에 그릴 값들을 상황별로 결정한다 (우선순위: 분봉 > 현재가 한 점 > 아무것도 없음).
    if intraday_minutes:
        today_prices = [m["price"] for m in intraday_minutes]
    elif current_price is not None:
        today_prices = [current_price]
    else:
        today_prices = []  # 분봉/현재가 조회 실패: "오늘" 지점 없이 과거 종가만 그린다.

    # daily_closes에 오늘 날짜 종가가 섞여 있어도, "오늘" 구간을 따로 그릴 때는 제외한다 —
    # describe_price_trend() 호출부도 같은 헬퍼를 써서 항상 같은 개수를 세게 만든다.
    daily_closes = dedupe_daily_closes(daily_closes, current_price, intraday_minutes)

    # daily_closes는 [{"date": "20260722", "close": 71000}, ...] 형태다.
    # x축에 표시할 짧은 날짜 라벨("07/22")을 문자열 슬라이싱으로 만든다:
    #   entry['date'][4:6] → "07"(5~6번째 글자, 월),  entry['date'][6:] → "22"(7번째 글자부터 끝, 일)
    daily_labels = [f"{entry['date'][4:6]}/{entry['date'][6:]}" for entry in daily_closes]
    daily_prices = [entry["close"] for entry in daily_closes]

    # 과거 종가 리스트 + 오늘 값 리스트를 이어붙여 하나의 선으로 그릴 전체 값 목록을 만든다.
    prices = daily_prices + today_prices

    if intraday_minutes:
        # 분봉은 하루에도 수십~수백 개가 나와서, x좌표를 그냥 "몇 번째 점인지"(0,1,2,...)로 매기면
        # 점 개수가 훨씬 적은 과거 종가(15일 이하) 구간이 왼쪽 끝에 조밀하게 몰리고 그래프 대부분을
        # 오늘 분봉이 차지해버린다. 그래서 점 개수 비율과 무관하게 x좌표를 절반씩
        # (과거 종가 0.0~0.5, 오늘 분봉 0.5~1.0) 강제로 나눠 배치한다.
        daily_x = _evenly_spaced(0.0, 0.5, len(daily_prices))
        today_x = _evenly_spaced(0.5, 1.0, len(today_prices))
        x = daily_x + today_x
    else:
        # 분봉이 없으면(현재가 한 점 또는 아예 없음) "오늘" 쪽 점이 하나뿐이라 절반씩 나눌 이유가
        # 없으므로, 예전처럼 그냥 순서대로 이어지는 정수 좌표를 쓴다.
        x = list(range(len(prices)))
        daily_x = x[:len(daily_prices)]

    # fig(전체 도화지)와 ax(실제로 선·점을 그리는 좌표축)를 함께 만든다. figsize는 (가로, 세로)
    # 인치 단위 그림 크기다.
    fig, ax = plt.subplots(figsize=(7, 4))
    # ax.plot(x좌표들, y좌표들, ...): 점들을 순서대로 이어 선 그래프를 그린다.
    ax.plot(x, prices, color="#1f77b4", linewidth=1.5)

    # 과거 종가는 점 + 값 라벨로 강조한다 (분봉까지 전부 라벨을 붙이면 수백 개가 겹쳐 안 보인다).
    if daily_x:
        # marker="o", linestyle="None": 선은 안 그리고 동그란 점만 찍는다 (선은 위에서 이미 그렸다).
        ax.plot(daily_x, daily_prices, marker="o", linestyle="None", color="#1f77b4")
        # zip(daily_x, daily_prices)로 (x좌표, y값) 쌍을 하나씩 돌면서, 각 점 위에 실제 가격
        # 숫자를 텍스트로 붙인다. f"{yi:,}"의 ":,"는 천단위마다 콤마를 넣는 서식 지정자다
        # (71000 → "71,000"). xytext=(0, 8)은 점에서 위로 8포인트 띄워서 라벨을 쓴다는 뜻이다.
        for xi, yi in zip(daily_x, daily_prices):
            ax.annotate(f"{yi:,}", (xi, yi), textcoords="offset points", xytext=(0, 8), ha="center", fontsize=13)

    # "오늘" 지점(현재가/분봉)이 있을 때만 마지막 지점을 강조 표시한다.
    if today_prices:
        # x[-1], prices[-1]: 리스트의 마지막(-1번째) 원소, 즉 그래프에서 가장 최근 값이다.
        ax.plot(x[-1], prices[-1], marker="o", color="#d62728")
        ax.annotate(f"{prices[-1]:,}", (x[-1], prices[-1]), textcoords="offset points",
                    xytext=(0, 8), ha="center", fontsize=13, color="#d62728")

    # x축 눈금: 과거 일자는 전부, 오늘 분봉은 정시(HH:00)만 골라 표시한다 (전부 표시하면 겹침).
    # list(daily_x)/list(daily_labels): 원본 리스트를 복사해서 새 리스트를 만든다 — 이후 append로
    # 눈금을 더 추가해도 daily_x/daily_labels 원본은 그대로 유지된다.
    tick_positions = list(daily_x)
    tick_labels = list(daily_labels)
    if intraday_minutes:
        # zip(today_x, intraday_minutes): 위에서 절반 구간(0.5~1.0)에 재배치한 x좌표와 분봉을
        # 순서대로 짝지어, 정각(HH:00)에 해당하는 분봉만 눈금으로 남긴다(전부 표시하면 겹침).
        for xi, m in zip(today_x, intraday_minutes):
            if m["time"].endswith("00"):  # "HHMM" 문자열이 "00"으로 끝나면 정각(예: "0900")
                tick_positions.append(xi)
                # m['time'][:2]는 앞 2글자(시), m['time'][2:]는 그 뒤 전부(분) — "09:00" 형태로 조합.
                tick_labels.append(f"{m['time'][:2]}:{m['time'][2:]}")
    elif current_price is not None:
        tick_positions.append(x[-1])
        tick_labels.append("현재")
    ax.set_xticks(tick_positions)
    # rotation=45, ha="right": 라벨이 많아 겹치지 않도록 45도 기울여서 오른쪽 정렬로 표시한다.
    ax.set_xticklabels(tick_labels, rotation=45, ha="right", fontsize=8)

    # 그림 안에는 더 이상 제목을 넣지 않는다 — 그래프를 보여주는 쪽에서
    # 이미 같은 문구(describe_price_trend())를 네이티브 텍스트로 보여주므로 중복이었다.
    ax.set_ylabel("가격 (원)")
    ax.grid(True, alpha=0.3)  # 배경에 옅은(투명도 0.3) 격자선을 넣어 값을 읽기 쉽게 한다.
    fig.tight_layout()  # 라벨/제목이 그림 밖으로 잘리지 않도록 여백을 자동으로 조정한다.

    # 그래프를 디스크의 실제 파일이 아니라 메모리 상의 바이트 버퍼(io.BytesIO)에 PNG 형식으로
    # "저장"한다 — 파일로 안 남기고 바로 텔레그램 전송/화면 표시에 쓸 수 있어서 편리하다.
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png")
    plt.close(fig)  # 다 그린 figure를 메모리에서 정리한다 (안 닫으면 반복 호출 시 계속 쌓인다).
    # savefig 직후엔 버퍼의 "커서"가 맨 끝(쓰기가 끝난 지점)에 있다. seek(0)으로 다시 맨 앞으로
    # 되돌려야, 이 버퍼를 읽는 쪽(sendPhoto, st.image 등)이 처음부터 제대로 읽을 수 있다.
    buffer.seek(0)
    return buffer
