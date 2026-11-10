"""
장마감 직후 하루 1회 실행되는 스크립트.
관심종목(watchlist.csv)의 그날 최종 종가를 네이버 API로 조회해 Turso DB의 price_history
테이블에 누적 저장한다 (종목별 최근 15거래일치만 유지, 오래된 행은 삭제).

여기서 쌓은 종가를 실제로 읽어 그래프에 그리는 것은 다음 주에 한다 — 이번 주는 "쌓기"까지다.

예전에는 price_history.json 파일을 저장소에 직접 커밋해 상태를 유지했지만, DB 자체가 영속
저장소이므로 이제 그럴 필요가 없다.

⚠️ read_watchlist()와 fetch_naver_current_price()는 notify_stock_price.py에도 **똑같은 것이
한 벌 더** 들어 있다. 두 스크립트가 같은 일(관심종목 읽기, 현재가 조회)을 해야 하는데 각자
자기 파일 안에 갖고 있어서 그렇다. 한쪽을 고치면 다른 쪽도 똑같이 고쳐야 한다.
"""
import os
import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

WATCHLIST_FILE = "watchlist.csv"
NUM_HISTORY_DAYS = 15
# 시간대를 명시하지 않으면 실행 환경에 따라 날짜가 하루 어긋날 수 있다. 항상 이 KST를 기준으로 쓴다.
KST = ZoneInfo("Asia/Seoul")

load_dotenv()

TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")


# ─────────────────────────────────────────────────────────────────────────────
# 아래 두 함수는 notify_stock_price.py에도 똑같이 들어 있다 (위 docstring 참고).
# ─────────────────────────────────────────────────────────────────────────────
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


# ─────────────────────────────────────────────────────────────────────────────
# 아래 두 함수는 이 스크립트에만 있다 (DB에 쓰는 일은 여기서만 한다).
# ─────────────────────────────────────────────────────────────────────────────
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


# 이 파일은 함수로 감싸지 않고 맨 위에서 아래로 순서대로 실행되는 "스크립트" 형태다
# (notify_stock_price.py처럼 함수로 감싸 재사용할 필요가 없어서 더 단순하게 작성했다).
# 이 스크립트는 휴장일에 실행돼도 그냥 돈다. 저장할 날짜를 "지금 몇 시인가"가 아니라
# traded_at(체결 시각)에서 뽑기 때문에, 휴장일에 실행돼도 네이버가 돌려주는 마지막 거래일의
# 종가가 그 거래일 날짜로 저장된다 — 이미 저장돼 있으면 같은 값으로 덮어쓸 뿐이라(upsert)
# 중복도 생기지 않는다. 그래서 그 결과 로그에는 실행한 날이 아니라 마지막 거래일 날짜가 찍힌다
# (예: 광복절에 실행하면 그 전 거래일 날짜가 기록된다). 헷갈릴 수 있지만 그게 맞는 동작이다.
watchlist_codes = read_watchlist()
if watchlist_codes is None:
    # 관심종목 목록을 못 읽었으면 더 할 수 있는 일이 없으므로 여기서 프로그램을 끝낸다.
    exit()

print(f"🚀 종가 수집 시작... (실행 시각 {datetime.now(KST).strftime('%Y-%m-%d %H:%M')} KST)")

collected = 0  # 실제로 저장에 성공한 종목 수를 센다.
# get_turso_client()를 루프 밖에서 딱 한 번만 호출해 클라이언트 하나를 만들고, 종목마다
# update_price_history()에 그 client를 넘겨 재사용한다 — 관심종목이 몇 개 안 될 때는 차이가
# 작지만, 종목 수가 늘어날수록(예: 20개) 종목마다 새로 연결을 맺고 매번 테이블 존재 확인
# 쿼리까지 반복하는 비용을 줄여준다. with 문이라 루프가 끝나면(예외가 나도) client.close()가
# 자동으로 호출된다.
with get_turso_client() as client:
    for code in watchlist_codes:
        info = fetch_naver_current_price(code)
        if info is None:
            continue

        if info["is_open"]:
            # 장마감 후 실행되어야 하는 스크립트인데 아직 장중이면 종가가 확정되지 않은
            # 상태이므로 히스토리에 반영하지 않는다 (실행 시각 설정을 다시 확인해야 함).
            print(f"⚠️ [{code}] 아직 장중입니다. 확정되지 않은 가격이라 기록하지 않습니다.")
            continue

        # 저장할 날짜는 "지금 몇 시인가"가 아니라 "이 가격이 언제 체결된 것인가"로 정한다.
        # datetime.now()를 쓰면 실행이 늦어져 자정을 넘겼을 때 전날 종가가 다음날 날짜로
        # 저장된다. traded_at은 체결 시각이라 실행 시각과 무관하게 항상 옳은 거래일을 가리킨다.
        # "2026-07-29T16:10:20+09:00"에서 앞 10글자가 날짜("2026-07-29")이고, 하이픈을 빼면
        # DB의 date 컬럼 형식("20260729")이 된다.
        traded_at = info.get("traded_at") or ""
        if len(traded_at) >= 10:
            date_str = traded_at[:10].replace("-", "")
        else:
            # 네이버 응답에 이 필드가 없거나 형식이 바뀐 경우의 폴백 — 실행 시각을 쓴다.
            date_str = datetime.now(KST).strftime("%Y%m%d")
            print(f"⚠️ [{code}] 체결 시각을 읽지 못해 실행 시각 기준으로 날짜를 정합니다.")

        update_price_history(code, date_str, info["price"], client=client)
        collected += 1
        print(f"✅ [{code}] {info['name']} {date_str} 종가 {info['price']:,}원 기록")

if collected == 0:
    print("❌ 기록된 종가가 하나도 없습니다.")
    # 저장된 것이 하나도 없으므로, 아래 완료 메시지를 찍지 않고 여기서 끝낸다.
    exit()

print(f"🎉 종가 히스토리를 Turso DB에 저장했습니다 ({collected}/{len(watchlist_codes)}종목).")
