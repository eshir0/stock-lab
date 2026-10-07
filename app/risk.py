"""Deterministic sizing for cash-funded paper positions (a month-long swing horizon, or the older same-session one)."""
import math
import time
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP

from . import shares
from .instruments import SYMBOLS


class RiskError(ValueError):
    pass


# 'daily_focus': a data- and news-checked short list per market each morning; 'fixed': the fixed lineup.
UNIVERSE_MODES = ('daily_focus', 'fixed')

# 'month': positions are planned for up to a month, are held overnight and are protected by a trailing stop.
# 'intraday': the older same-session mode (it is also what a saved experiment without a horizon means).
HORIZONS = ('month', 'intraday')
# How wide a month position's stop and target are. 'ai': the director chooses (2-15% stop, target >= 1.5x). 'market':
# research_plan_market.json part B (2026-10-07) - US names get a server-set stop of 3x the daily range (ATR14, 2-15%) and a
# target of 3x the stop (3-40%), so the trailing stop gives a trend room; Korean names stay with the director's choice.
# 'market_long' (2026-10-07, research_plan_us_hold.json part A): the same US stops, and US positions may be held for 63
# sessions (90 calendar days) instead of the experiment's 30 days - with stops this wide 62% of trades used to end at the limit.
EXIT_PROFILES = ('ai', 'market', 'market_long')
US_STOP_ATR, US_TAKE_STOP = 3.0, 3.0
US_LONG_HOLD_MINUTES = 90*1440


def wide_us(settings, market):
    """True when this experiment sets US stops from the daily range (exit_profile 'market' or 'market_long')."""
    return market == 'US' and (settings or {}).get('exit_profile') in ('market', 'market_long')


def market_exit(market, atr_pct):
    """(stop %, target %) the 'market' profile sets for a name, or None when the director's own numbers stand."""
    if market != 'US' or not isinstance(atr_pct, (int, float)) or not math.isfinite(atr_pct) or atr_pct <= 0:
        return None
    stop = round(min(max(US_STOP_ATR*atr_pct, 2.0), 15.0), 2)
    return stop, round(min(max(US_TAKE_STOP*stop, 3.0), 40.0), 2)
MONTH_MINUTES = 30*24*60
# One place for every number that depends on the horizon: the AI's plan is validated and sized against these.
BOUNDS = {
    'month': {'stop': (2, 15), 'take': (3, 40), 'holding': (1440, MONTH_MINUTES), 'default_holding': MONTH_MINUTES,
              'placeholder': {'stop_loss_pct': 5, 'take_profit_pct': 10, 'max_holding_minutes': 20160}},
    'intraday': {'stop': (.2, 10), 'take': (.3, 40), 'holding': (15, 240), 'default_holding': 120,
                 'placeholder': {'stop_loss_pct': 2, 'take_profit_pct': 4, 'max_holding_minutes': 60}},
}
# Trailing stop of a month position: once the price is TRAIL_ARM of the way to the target the stop first rises to the
# entry price plus a small margin for costs, then follows the highest price by the planned stop distance.
TRAIL_ARM = .5
BREAKEVEN_PCT = .3


def horizon_of(settings):
    value = (settings or {}).get('horizon', 'intraday')
    return value if value in HORIZONS else 'intraday'


def trailed_stop(position, high_water):
    """The stop a month position should have now. It only ever rises and never reaches the target price."""
    stop, take = position.get('stop_price'), position.get('take_profit_price')
    average, trail = position.get('average'), position.get('trail_pct')
    if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0
               for x in (stop, take, average, trail, high_water)):
        return stop
    if high_water < average+(take-average)*TRAIL_ARM:
        return stop
    raised = max(stop, average*(1+BREAKEVEN_PCT/100), high_water*(1-trail/100))
    if raised <= stop:
        return stop
    if position.get('exit_mode') == 'trail':
        return max(stop, round(raised, 2))      # no sale at the target: the stop keeps following the high past it
    # Two decimals, and always strictly below the target: at the target the position is sold as a winner anyway.
    return max(stop, min(round(raised, 2), round(take-.01, 2)))


def _decimal(value, name, minimum=None, maximum=None):
    if type(value) not in (int, float, Decimal):
        raise RiskError(f'{name}: 유효한 숫자가 필요합니다.')
    number = Decimal(str(value))
    if not number.is_finite():
        raise RiskError(f'{name}: 유효한 숫자가 필요합니다.')
    if minimum is not None and number < Decimal(str(minimum)):
        raise RiskError(f'{name}: 허용 범위보다 작습니다.')
    if maximum is not None and number > Decimal(str(maximum)):
        raise RiskError(f'{name}: 허용 범위보다 큽니다.')
    return number


def _money(value):
    return value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)


def _floor(value):
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def _quantity(value, name):
    number = _decimal(value, name, 0)
    if number != number.to_integral_value():
        raise RiskError(f'{name}: 정수 수량이 필요합니다.')
    return int(number)


def _shares(value, name, fractional):
    """A share count as a Decimal: whole numbers only, unless the market takes fractional shares (then rounded down to 4 decimals)."""
    number = _decimal(value, name, 0)
    if fractional:
        return shares.floor_to(number, True)
    if number != number.to_integral_value():
        raise RiskError(f'{name}: 정수 수량이 필요합니다.')
    return Decimal(int(number))


EXIT_MODES = ('target', 'trail')   # target: sell all at the take-profit (A) · trail: keep going on the trailing stop (C)


def normalize_settings(settings=None):
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise RiskError('전략 설정은 객체여야 합니다.')
    leveraged = settings.get('include_leveraged_etfs', True)
    if type(leveraged) is not bool:
        raise RiskError('레버리지 ETF 포함 여부는 참 또는 거짓이어야 합니다.')
    universe = settings.get('universe_mode', 'daily_focus')
    if universe not in UNIVERSE_MODES:
        raise RiskError('종목 구성 방식은 daily_focus 또는 fixed여야 합니다.')
    risk = _decimal(settings.get('risk_per_trade_pct', .5), '거래당 위험 비율', .1, 2)
    daily = _decimal(settings.get('daily_loss_limit_pct', 2), '일일 손실 한도', 1, 10)
    horizon = settings.get('horizon', 'intraday')
    if horizon not in HORIZONS:
        raise RiskError('투자 기간은 month 또는 intraday여야 합니다.')
    low, high = BOUNDS[horizon]['holding']
    holding = _decimal(settings.get('max_holding_minutes', BOUNDS[horizon]['default_holding']), '최대 보유 시간', low, high)
    if holding != holding.to_integral_value():
        raise RiskError('최대 보유 시간은 정수 분으로 입력하세요.')
    # The most of the account one name may take. Saved experiments without it keep the old 30%.
    position = _decimal(settings.get('max_position_pct', 30), '종목당 최대 비중', 10, 100)
    # Saved experiments without the key keep selling at the target, the rule they were started with.
    exit_mode = settings.get('exit_mode', 'target')
    signal_filter = settings.get('signal_filter', 'all')       # saved experiments act on every rule, as they started
    use_evidence = settings.get('evidence', 'off')              # ... and analyse without the archive's evidence packs
    scan = settings.get('scan', 'focus')                         # ... and only look at the daily focus list
    exit_profile = settings.get('exit_profile', 'ai')            # ... and let the AI choose every stop and target
    if exit_profile not in EXIT_PROFILES:
        raise RiskError('손절·익절 폭 방식은 ai, market, market_long 중 하나여야 합니다.')
    if scan not in ('focus', 'pool'):
        raise RiskError('후보 확인 범위는 focus 또는 pool이어야 합니다.')
    if use_evidence not in ('on', 'off'):
        raise RiskError('과거 근거 사용은 on 또는 off여야 합니다.')
    if signal_filter not in ('all', 'research'):
        raise RiskError('매수 신호 거르기는 all 또는 research여야 합니다.')
    if exit_mode not in EXIT_MODES:
        raise RiskError('익절 방식은 target 또는 trail이어야 합니다.')
    return {'include_leveraged_etfs': leveraged, 'universe_mode': universe, 'horizon': horizon,
            'risk_per_trade_pct': float(risk), 'daily_loss_limit_pct': float(daily), 'max_holding_minutes': int(holding),
            'max_position_pct': shares.number(position), 'exit_mode': exit_mode, 'signal_filter': signal_filter, 'evidence': use_evidence, 'scan': scan,
            'exit_profile': exit_profile}


def _instrument(symbol):
    instrument = SYMBOLS.get(symbol)
    if not instrument:
        raise RiskError('지원하지 않는 종목입니다.')
    factor = abs(_decimal(instrument.get('leverage_factor', 1), 'ETF 배율'))
    if factor < 1:
        raise RiskError('유효하지 않은 ETF 배율입니다.')
    return instrument, factor


US_FREE_ORDER = Decimal('10')     # Toss: a US order whose total fill is $10 or less pays no commission


def is_etf(symbol):
    item = SYMBOLS.get(symbol) or {}
    return bool(item.get('etf') or item.get('leveraged_etf'))


def taxed(currency, symbol=None):
    """Korea's securities transaction tax is due on selling a listed stock; ETFs are exempt. Without a symbol a Korean sale is
    assumed taxed (the conservative side)."""
    return currency == 'KRW' and not (symbol and is_etf(symbol))


def trade_fee(config, symbol, side, gross):
    """Commission, plus the Korean transaction tax on a stock sale, of one fill (a Decimal in the instrument's currency)."""
    currency = SYMBOLS[symbol]['currency']
    gross = Decimal(str(gross))
    bps = Decimal(str(config.fee_kr if currency == 'KRW' else config.fee_us))
    if currency == 'USD' and gross <= US_FREE_ORDER:
        bps = Decimal('0')
    if side == 'SELL' and taxed(currency, symbol):
        bps += Decimal(str(config.sell_tax_kr))
    return gross*bps/10000


def _round_trip_cost(config, currency, quote, symbol=None):
    """What one buy and one sell cost, in percent of the price (a Decimal): both fees, both slippages, the Korean sell tax and the
    quoted spread."""
    bid, ask = _decimal(quote.get('bid'), '매수 호가', .00000001), _decimal(quote.get('ask'), '매도 호가', .00000001)
    fee = _decimal(config.fee_kr if currency == 'KRW' else config.fee_us, '수수료', 0, 1000)
    slip = _decimal(config.slippage_bps, '슬리피지', 0, 1000)
    tax = _decimal(config.sell_tax_kr, '매도세', 0, 1000) if taxed(currency, symbol) else Decimal('0')
    spread = (ask-bid)/((ask+bid)/2)*10000 if ask >= bid else Decimal('0')
    return (2*fee+2*slip+tax+spread)/100


def _min_take(config, currency, quote, symbol=None):
    ratio = Decimal(str(getattr(config, 'min_take_cost_ratio', 0) or 0))
    return ratio*_round_trip_cost(config, currency, quote, symbol) if ratio > 0 else Decimal('0')


def round_trip_cost_pct(config, currency, quote, symbol=None):
    """The round-trip cost in percent. The judgement scoring deducts the same estimate (`DeskMixin.trade_cost_bps`)."""
    return float(_round_trip_cost(config, currency, quote, symbol))


def min_take_pct(config, currency, quote, symbol=None):
    """The narrowest take-profit (percent) a buy may have: `MIN_TAKE_COST_RATIO` times the round-trip cost; 0 when the rule is off."""
    return float(_min_take(config, currency, quote, symbol))


def _stop_exit(stop, spread, slip, sell_fee):
    price = max(Decimal('0'), stop-spread)*(1-slip)
    return max(Decimal('0'), price*(1-sell_fee))


def size_order(state, symbol, quote, decision, constraints, config, now=None):
    """Size shares, never cash loans or synthetic leverage; a zero size is a valid hold."""
    now = time.time() if now is None else now
    _decimal(now, '현재 시각', 0)
    settings = normalize_settings(state.get('strategy_settings'))
    bounds = BOUNDS[settings['horizon']]
    instrument, factor = _instrument(symbol)
    currency = instrument['currency']
    side = decision.get('stance')
    if side not in ('BUY', 'SELL'):
        raise RiskError('매수 또는 매도 판단이 필요합니다.')
    weight = _decimal(decision.get('target_weight_pct'), '목표 종목 비중', 0, settings['max_position_pct'])
    fractional = bool(constraints.get('fractional_shares'))
    nav = _decimal(constraints.get('portfolio_equity'), '평가 자산', 0)
    ask = _decimal(quote.get('ask'), '매도 호가', .00000001)
    bid = _decimal(quote.get('bid'), '매수 호가', .00000001)
    if bid > ask:
        raise RiskError('매수 호가가 매도 호가보다 높습니다.')
    slip = _decimal(config.slippage_bps, '슬리피지', 0, 1000)/10000
    fee = _decimal(config.fee_kr if currency == 'KRW' else config.fee_us, '수수료', 0, 1000)/10000
    tax = _decimal(config.sell_tax_kr, '매도세', 0, 1000)/10000 if taxed(currency, symbol) else Decimal('0')
    sell_fee = fee+tax
    held = _shares(state.get('positions', {}).get(symbol, {}).get('quantity', 0), '보유 수량', fractional)
    max_buy = min(Decimal(shares.MAX_QUANTITY), _shares(constraints.get('max_buy_quantity', 0), '최대 매수 수량', fractional))
    max_sell = min(Decimal(shares.MAX_QUANTITY), held, _shares(constraints.get('max_sell_quantity', held), '최대 매도 수량', fractional))
    entry = _money(ask*(1+slip))
    if entry <= 0:
        raise RiskError('체결 단가가 최소 계산 단위보다 작습니다.')
    target_total = shares.floor_to(nav*weight/100/entry, fractional)
    target_delta = max(Decimal(0), target_total-held) if side == 'BUY' else max(Decimal(0), held-target_total)
    result = {'quantity': 0, 'target_quantity': shares.number(target_delta), 'risk_quantity': 0,
              'max_buy_quantity': shares.number(max_buy), 'estimated_stop_risk': 0.0, 'reason': '',
              'stop_price': None, 'take_profit_price': None, 'expires_at': None}
    if side == 'SELL':
        quantity = min(target_delta, max_sell)
        result.update(quantity=shares.number(quantity), reason='목표 비중까지 보유 수량을 줄입니다.' if quantity else '이미 목표 비중 이하입니다.')
        return result

    stop_pct = _decimal(decision.get('stop_loss_pct'), '손절 비율', *bounds['stop'])
    take_pct = _decimal(decision.get('take_profit_pct'), '익절 비율', 0, 40)
    if take_pct < stop_pct*Decimal('1.5'):
        raise RiskError('익절 비율은 손절 비율의 1.5배 이상이어야 합니다.')
    holding = _decimal(decision.get('max_holding_minutes'), '계획 보유 시간', *bounds['holding'])
    if holding != holding.to_integral_value():
        raise RiskError('계획 보유 시간은 정수 분이어야 합니다.')
    session_end = _decimal(quote.get('session_end'), '장 종료 시각', 0)
    stop = _money(entry*(1-stop_pct/100))
    take = _money(entry*(1+take_pct/100))
    if not Decimal('0') < stop < entry < take:
        raise RiskError('손절·익절 가격이 유효한 간격을 만들지 못합니다.')
    until = now+min(int(holding), settings['max_holding_minutes'])*60
    if settings.get('exit_profile') == 'market_long' and instrument['market'] == 'US' and settings['horizon'] == 'month':
        until = now+US_LONG_HOLD_MINUTES*60            # a US trend gets 63 sessions; the trailing stop usually ends it first
    # A month position is held overnight; only the older same-session mode has to be closed before the session ends.
    expiry = until if settings['horizon'] == 'month' else min(float(session_end)-120, until)
    result.update(stop_price=float(stop), take_profit_price=float(take), expires_at=expiry)
    if factor > 1 and not settings['include_leveraged_etfs']:
        result['reason'] = '이 실험은 레버리지 ETF 신규 매수를 허용하지 않습니다.'
        return result
    if float(session_end)-now < 300:
        result['reason'] = '장 종료까지 5분 미만이므로 신규 매수를 보류합니다.'
        return result
    if nav == 0:
        result['reason'] = '해당 통화의 가상 자산이 없습니다.'
        return result
    floor = _min_take(config, currency, quote, symbol)
    if floor > 0 and take_pct < floor:
        result['reason'] = (f'익절 폭 {float(take_pct):g}%가 왕복 비용({float(_round_trip_cost(config, currency, quote, symbol)):.2f}%)의 '
                            f'{float(config.min_take_cost_ratio):g}배(최소 {float(floor):.2f}%)에 못 미쳐 매수하지 않습니다. 목표가 비용에 잡아먹히는 거래입니다.')
        return result

    spread = ask-bid
    risk_per_share = entry*(1+fee)-_stop_exit(stop, spread, slip, sell_fee)
    if risk_per_share <= 0:
        raise RiskError('손절 위험 금액을 계산할 수 없습니다.')
    trade_budget = nav*Decimal(str(settings['risk_per_trade_pct']))/100
    open_risk, exposure = Decimal('0'), Decimal('0')
    for held_symbol, position in state.get('positions', {}).items():
        held_instrument, held_factor = _instrument(held_symbol)
        if held_instrument['currency'] != currency:
            continue
        quantity = _shares(position.get('quantity', 0), '보유 수량', fractional)
        if not quantity:
            continue
        mark_quote = quote if held_symbol == symbol else state.get('quotes', {}).get(held_symbol, {})
        mark = _decimal(mark_quote.get('last'), '보유 종목 평가 가격', .00000001)
        exposure += mark*quantity*held_factor
        stop_value = position.get('stop_price', position.get('risk_plan', {}).get('stop_price'))
        if stop_value is None:
            result['reason'] = '손절 계획이 없는 보유 종목을 먼저 정리해야 추가 매수할 수 있습니다.'
            return result
        held_stop = _decimal(stop_value, '보유 종목 손절 가격', .00000001)
        held_ask = _decimal(mark_quote.get('ask'), '보유 종목 매도 호가', .00000001)
        held_bid = _decimal(mark_quote.get('bid'), '보유 종목 매수 호가', .00000001)
        if held_bid > held_ask:
            raise RiskError('보유 종목 호가가 올바르지 않습니다.')
        basis = position.get('cost_basis')
        basis = _decimal(basis, '보유 종목 원가', 0)/quantity if basis is not None else _decimal(position.get('average'), '보유 종목 평균가', 0)
        open_risk += max(Decimal('0'), max(basis, mark)-_stop_exit(held_stop, held_ask-held_bid, slip, sell_fee))*quantity
    total_budget = max(Decimal('0'), trade_budget*3-open_risk)
    risk_quantity = max(Decimal(0), shares.floor_to(min(trade_budget, total_budget)/risk_per_share, fractional))
    exposure_quantity = max(Decimal(0), shares.floor_to(max(Decimal('0'), nav-exposure)/(entry*factor), fractional))
    quantity = min(target_delta, risk_quantity, max_buy, exposure_quantity)
    too_small = quantity > 0 and shares.below_minimum(quantity, entry, fractional)
    if too_small:
        quantity = Decimal(0)
    result.update(quantity=shares.number(quantity), risk_quantity=shares.number(risk_quantity),
                  estimated_stop_risk=float(_money(risk_per_share*quantity)))
    if too_small:
        result['reason'] = '소수점 주문은 최소 1달러 이상이어야 해서 이번 주문은 보류합니다.'
    elif quantity:
        result['reason'] = '목표 비중·거래당 위험·총 손절 위험·ETF 환산 노출·현금 한도 안에서 수량을 계산했습니다.'
    elif target_delta == 0:
        result['reason'] = '목표 비중에 이미 도달했거나 1주를 살 만큼의 목표 금액이 없습니다.'
    elif max_buy == 0:
        result['reason'] = '현금·종목 비중·호가 잔량 한도로 신규 매수 수량이 0주입니다.'
    elif exposure_quantity == 0:
        result['reason'] = 'ETF 배율을 반영한 총 노출 한도에 도달했습니다.'
    else:
        result['reason'] = '거래당 또는 전체 손절 위험 한도로 1주도 매수할 수 없습니다.'
    return result
