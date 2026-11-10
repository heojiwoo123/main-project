# 이번 주에 달라진 것 — 8주차

**실행하는 스크립트가 두 개가 됩니다.** 그리고 종가가 데이터베이스에 쌓이기 시작합니다.

## 새로 생긴 파일
- `collect_daily_close.py` — 장 마감 후 하루 한 번 실행해 그날 종가를 DB에 저장합니다

## 새로 생긴 함수
- `get_turso_client()` — 클라우드 DB(Turso)에 연결합니다
- `update_price_history()` — 종가를 저장합니다(같은 날짜면 덮어쓰고, 오래된 것은 지웁니다)

## 세팅이 추가됩니다
- `requirements.txt` — `libsql-client` 추가
- `.env` — `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` 두 줄 추가 (Turso 가입이 필요합니다)

> **`watchlist.csv`는 지난주부터 압축에 안 들어 있습니다**(내가 고친 종목이 덮이지 않도록
> `.env`와 같은 방식으로 바꿨습니다). 들어 있는 것은 본보기인 `watchlist.example.csv`이고,
> 내 `watchlist.csv`가 이미 있으면 그냥 두면 됩니다. 없으면 본보기를 복사해 이름을
> `watchlist.csv`로 바꾸세요. → 자세한 것은 `FAQ.md` 2절 8번

## 눈여겨볼 것 — 같은 함수가 두 군데 있습니다
`read_watchlist()`와 `fetch_naver_current_price()`가 **두 파일에 똑같이 한 벌씩** 들어 있습니다.
두 스크립트가 같은 일을 해야 하는데 각자 갖고 있어서 그렇습니다. **한쪽을 고치면 다른 쪽도
똑같이 고쳐야 합니다.** 불편하죠? 다음 주에 이 문제를 다룹니다.

## 종가를 쌓았는데 그래프는 그대로입니다
정상입니다. 이번 주는 "쌓기"까지이고, 쌓인 것을 **읽어서 그리는** 일은 다음 주입니다.
