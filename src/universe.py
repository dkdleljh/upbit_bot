from .utils import parse_market


def _find_price(tickers: dict[str, dict], market: str, default: float = 0.0) -> float:
    t = tickers.get(market)
    if not t:
        return default
    return float(t.get("trade_price", default) or default)


def compute_notional_krw(ticker: dict, tickers: dict[str, dict]) -> float:
    market = ticker["market"]
    parts = parse_market(market)
    acc_price_24h = float(ticker.get("acc_trade_price_24h", 0.0) or 0.0)
    last = float(ticker.get("trade_price", 0.0) or 0.0)
    vol = float(ticker.get("acc_trade_volume_24h", 0.0) or 0.0)

    btc_krw = _find_price(tickers, "KRW-BTC", default=130000000.0)
    usdt_krw = _find_price(tickers, "KRW-USDT", default=1300.0)

    if parts.quote == "KRW":
        return acc_price_24h
    if parts.quote == "BTC":
        if acc_price_24h > 0:
            return acc_price_24h * btc_krw
        return vol * last * btc_krw
    if parts.quote == "USDT":
        if acc_price_24h > 0:
            return acc_price_24h * usdt_krw
        return vol * last * usdt_krw
    return 0.0


def select_universe(
    tickers_list: list[dict],
    orderbooks: dict[str, dict],
    top30_n: int,
    top10_n: int,
) -> tuple[list[dict], list[dict], list[dict]]:
    """유니버스 선택.

    요구사항: 진입 마켓을 고정하지 않고,
    - 거래대금(acc_trade_price_24h → KRW 환산)
    - 거래량(acc_trade_volume_24h)
    두 기준 모두 상위 10 종목 위주로 거래.

    구현:
    - top30 후보는 거래대금 기준으로 뽑고,
    - top10은 (거래대금 top10) ∩ (거래량 top10) 교집합을 우선 사용
      (부족하면 두 리스트의 합집합을 "거래대금" 우선으로 채움)

    주의:
    - 거래량은 종목마다 단위가 다르므로 완전한 비교는 아니지만,
      '대금+량 모두 상위' 필터로 과도한 잡음을 줄이는 목적입니다.
    """

    tickers = {x["market"]: x for x in tickers_list}

    scored: list[dict] = []
    for t in tickers_list:
        tt = dict(t)
        tt["notional_krw"] = compute_notional_krw(t, tickers)
        tt["volume_24h"] = float(t.get("acc_trade_volume_24h", 0.0) or 0.0)
        scored.append(tt)

    scored_by_notional = sorted(scored, key=lambda x: x.get("notional_krw", 0.0), reverse=True)
    top30 = scored_by_notional[:top30_n]

    tradable: list[dict] = []
    for t in top30:
        # 1. Stablecoin 제외 (변동성 부족)
        if "USDT" in t["market"]:
            continue

        # 2. 최소 변동성 필터 (24시간 고저폭 3% 이상이어야 단타 각이 나옴)
        high = float(t.get("high_price", 0.0))
        low = float(t.get("low_price", 0.0))
        if low > 0:
            volatility = (high - low) / low
            if volatility < 0.03: # 3% 미만 변동성은 패스
                continue
        
        # 3. 추세 필터 (오늘 양전 상태이거나, 적어도 폭락 중은 아닌 것)
        # signed_change_rate: 1일 등락률
        change_rate = float(t.get("signed_change_rate", 0.0))
        if change_rate < -0.03: # -3% 이상 폭락 중인 칼날은 제외
            continue

        ob = orderbooks.get(t["market"])
        if not ob:
            continue
        units = ob.get("orderbook_units", [])
        if len(units) < 3:
            continue
        bid1 = float(units[0].get("bid_price", 0.0))
        ask1 = float(units[0].get("ask_price", 0.0))
        if bid1 <= 0 or ask1 <= 0:
            continue
        spread = (ask1 - bid1) / ((ask1 + bid1) / 2)
        # 멀티-quote(BTC/USDT) 마켓도 후보에 들어올 수 있도록 스프레드 컷을 완화
        # (실제 진입은 execution quality gate에서 한 번 더 걸러집니다)
        if spread <= 0.02:
            tradable.append(t)

    if top10_n <= 0:
        return top30, tradable, []

    by_market = {x["market"]: x for x in tradable}

    # 단순 거래대금 순보다는 "거래대금 x 변동성" 점수로 핫한 종목 우대
    # 거래대금이 터지면서 가격도 움직이는 녀석이 진짜다.
    for t in tradable:
        vol = float(t.get("notional_krw", 0.0))
        chg = abs(float(t.get("signed_change_rate", 0.0)))
        # 점수 = 거래대금 * (변동률 + 가중치)
        # 변동률이 0이어도 거래대금 기본 점수는 가져가도록 +0.01
        t["hot_score"] = vol * (chg + 0.01)

    trad_by_score = sorted(tradable, key=lambda x: x.get("hot_score", 0.0), reverse=True)
    
    # Top 10 선정 (Hot Score 기준)
    top10 = trad_by_score[:top10_n]

    return top30, tradable, top10
