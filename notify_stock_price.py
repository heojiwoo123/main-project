"""
하루 3회(장이 열리는 날 오전 10시/12시/2시) 실행되는 알림 스크립트.
관심종목(watchlist.csv)의 현재가를 네이버 API로 조회해 텔레그램 텍스트 메시지로 보내고,
collect_daily_close.py가 미리 저장해 둔 Turso DB의 최근 15거래일 종가에 오늘 분봉
(네이버 API로 즉시 조회, 저장하지 않음)을 이어붙인 추이 그래프를 종목별로 그려 sendPhoto로
전송한다. 과거 종가 자체는 매번 재조회하지 않는다.

send_price_notification()으로 알림 로직 전체를 함수 하나에 감싸 두었다.
`python notify_stock_price.py`로 직접 실행하면 그 함수가 그대로 실행된다.

"""
import html

from stock_utils import (
    read_watchlist,
    load_price_history,
    fetch_naver_current_price,
    fetch_naver_intraday_minutes,
    send_telegram_message,
    send_telegram_photo,
    build_price_chart,
    describe_price_trend,
    format_rate_badge,
    resolve_today_price,
    dedupe_daily_closes,
)


def send_price_notification() -> bool:
    """관심종목 현재가 텍스트 메시지와 종목별 추이 그래프를 텔레그램으로 전송합니다.

    관심종목을 하나라도 처리했으면(휴장일이라 보낼 게 없는 경우 포함) True, 아무것도 하지
    못했으면 False를 반환합니다. 여기서 "아무것도 하지 못했다"에는 조회에 전부 실패한 경우뿐
    아니라 조회는 됐는데 텔레그램 전송이 전부 실패한 경우도 포함됩니다.
    """
    watchlist_codes = read_watchlist()
    if watchlist_codes is None:
        return False

    # 1. 네이버 API로 현재가 조회 후 텔레그램 텍스트 메시지 전송
    print("🚀 관심종목 현재가 조회 시작...")

    current_prices = {}  # code -> fetch_naver_current_price() 결과
    # 종목별 상세 정보(가격·등락 등)는 사진 캡션에 담기므로, 헤더 문구만 먼저 만들어둔다
    # (추이 설명은 아래에서 대표 종목 데이터로 한 번만 덧붙인다). 아래 for문은 그래프 전송(2번)
    # 단계에서 쓸 current_prices만 채운다.
    telegram_message = "📊 <b>내 관심종목 현재가</b>"

    for code in watchlist_codes:
        info = fetch_naver_current_price(code)
        if info is None:
            # 이 종목만 조회 실패해도 프로그램을 멈추지 않고 다음 종목으로 넘어간다.
            continue

        current_prices[code] = info

    if not current_prices:
        print("❌ 관심종목의 현재가를 하나도 가져오지 못했습니다. 네이버 API 상태를 확인해 주세요.")
        return False

    # 2. collect_daily_close.py가 저장해 둔 최근 15거래일 종가를 읽어 그래프 생성 및 전송
    price_history = load_price_history()

    # 종목별 오늘 분봉을 미리 한 번씩만 조회해 딕셔너리에 담아둔다. 아래 헤더 문구(대표 종목)와
    # 그래프 루프가 같은 종목의 분봉을 각각 조회하면 같은 API를 두 번 부르게 되고, 조회 시각이
    # 달라 헤더가 세는 일수와 그래프의 점 개수가 어긋날 수 있다.
    intraday_by_code = {code: fetch_naver_intraday_minutes(code) for code in current_prices}

    # 추이 설명(describe_price_trend())은 종목마다 거의 같은 문구가 반복되므로, 대표로 첫
    # 종목의 데이터만 써서 헤더 문구 아래 한 번만 붙인다 (종목별 사진 캡션에는 넣지 않는다).
    # next(iter(current_prices)): current_prices는 {종목코드: 조회결과} 딕셔너리인데, 딕셔너리
    # 자체를 순회(iter)하면 키(종목코드)들이 나온다 — 그중 맨 처음 것 하나만 next()로 꺼낸다.
    first_code = next(iter(current_prices))
    first_info = current_prices[first_code]
    first_daily_closes = price_history.get(first_code, [])
    first_intraday_minutes = intraday_by_code[first_code]
    # resolve_today_price()로 "오늘" 값을 정하고, 그 값을 기준으로 dedupe_daily_closes()가
    # daily_closes에서 오늘 날짜를 걸러낸다 — 이렇게 해야 아래 describe_price_trend()가 세는
    # "최근 N일" 숫자가, 같은 데이터로 build_price_chart()가 실제로 그리는 점 개수와 항상
    # 일치한다(둘 다 같은 필터링을 거친 daily_closes를 보게 되므로).
    first_today_price = resolve_today_price(first_info["price"], first_info["is_open"],
                                            first_intraday_minutes, first_daily_closes)
    first_daily_closes = dedupe_daily_closes(first_daily_closes, first_today_price, first_intraday_minutes)
    telegram_message += f"\n\n{describe_price_trend(first_daily_closes, first_intraday_minutes)}"

    sent = 0  # 텔레그램으로 실제 전송에 성공한 건수(텍스트 1건 + 종목별 사진)를 센다.

    if send_telegram_message(telegram_message):
        sent += 1
        print("✅ 현재가 메시지를 텔레그램으로 전송했습니다!")
    else:
        print("❌ 현재가 메시지 전송에 실패했습니다.")

    # current_prices.items(): {종목코드: 조회결과} 딕셔너리를 (code, info) 쌍으로 순회한다.
    for code, info in current_prices.items():
        # price_history.get(code, []): 이 종목의 히스토리가 없으면 빈 리스트를 대신 쓴다.
        daily_closes = price_history.get(code, [])
        if not daily_closes:
            print(f"⚠️ [{code}] 저장된 종가 히스토리가 없어 현재가만으로 그래프를 그립니다. "
                  f"collect_daily_close.py가 아직 한 번도 실행되지 않았을 수 있습니다.")

        intraday_minutes = intraday_by_code[code]
        # resolve_today_price(): 분봉도 없고 장도 닫혀있으면(장마감 후~다음 장 시작 전) 지금
        # 조회한 "현재가"는 daily_closes의 마지막 종가와 필연적으로 같은 값이라 None을 돌려받는다
        # — build_price_chart()가 None을 받으면 "오늘" 점을 따로 안 그려서 중복 표시를 막는다.
        # daily_closes도 같이 넘기는 이유: 히스토리가 비어 있으면 중복될 종가 자체가 없으므로
        # 현재가를 살려둬야 한다(안 그러면 그릴 점이 없어 빈 그래프가 된다).
        today_price = resolve_today_price(info["price"], info["is_open"], intraday_minutes, daily_closes)
        chart_buffer = build_price_chart(daily_closes, today_price, intraday_minutes)
        # send_telegram_photo()가 parse_mode="HTML"로 보내므로, 종목명을 <b> 태그로 감싸면
        # 굵게 표시된다. html.escape()로 종목명에 HTML 특수문자가 섞여 있어도 태그 구조가
        # 깨지지 않게 한다. 텔레그램 캡션은 일반 텍스트라 HTML과 달리 공백이 줄어들지 않으므로,
        # "    "(공백 4칸)를 그대로 써도 들여쓰기가 유지된다.
        caption = (
            f"📈 <b>{html.escape(info['name'])}</b> ({code})\n"
            f"    {format_rate_badge(info['price'], info['rate'])}"
        )

        if send_telegram_photo(chart_buffer, caption):
            sent += 1
            print(f"🎉 [{code}] 추이 그래프를 텔레그램으로 전송했습니다!")
        else:
            print(f"❌ [{code}] 추이 그래프 전송에 실패했습니다.")

    # "하나도 못 보냈을 때만" 실패로 본다 — 일부 종목의 사진만 실패한 경우는 나머지가 정상적으로
    # 도착했으므로 실패가 아니다. 반대로 한 건도 못 보냈다면 봇 토큰 만료·텔레그램 장애처럼
    # 사용자가 알아채야 할 문제이므로 False를 돌려줘 종료 코드 1로 끝나게 한다.
    if sent == 0:
        print("❌ 텔레그램으로 아무것도 전송하지 못했습니다. 봇 토큰과 텔레그램 API 상태를 확인해 주세요.")
        return False

    return True


# if __name__ == "__main__": 은 "이 파일을 직접 실행했을 때만" 아래 코드를 돌리라는 뜻이다.
# 다른 스크립트가 `from notify_stock_price import send_price_notification`처럼 함수만
# 가져다 쓰는 경우에는 이 블록이 자동으로 실행되지 않는다.
if __name__ == "__main__":
    send_price_notification()
