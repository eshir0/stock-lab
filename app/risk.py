"""Deterministic sizing for intraday, cash-funded paper positions."""
import time
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP

from .instruments import SYMBOLS


class RiskError(ValueError):
    pass


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


def normalize_settings(settings=None):
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise RiskError('전략 설정은 객체여야 합니다.')
    leveraged = settings.get('include_leveraged_etfs', True)
    if type(leveraged) is not bool:
        raise RiskError('레버리지 ETF 포함 여부는 참 또는 거짓이어야 합니다.')
    risk = _decimal(settings.get('risk_per_trade_pct', .5), '거래당 위험 비율', .1, 2)
    daily = _decimal(settings.get('daily_loss_limit_pct', 2), '일일 손실 한도', 1, 10)
    holding = _decimal(settings.get('max_holding_minutes', 120), '최대 보유 시간', 15, 240)
    if holding != holding.to_integral_value():
        raise RiskError('최대 보유 시간은 정수 분으로 입력하세요.')
    return {'include_leveraged_etfs': leveraged, 'risk_per_trade_pct': float(risk),
            'daily_loss_limit_pct': float(daily), 'max_holding_minutes': int(holding)}


def _instrument(symbol):
    instrument = SYMBOLS.get(symbol)
    if not instrument:
        raise RiskError('지원하지 않는 종목입니다.')
    factor = abs(_decimal(instrument.get('leverage_factor', 1), 'ETF 배율'))
    if factor < 1:
        raise RiskError('유효하지 않은 ETF 배율입니다.')
    return instrument, factor


def _stop_exit(stop, spread, slip, sell_fee):
    price = max(Decimal('0'), stop-spread)*(1-slip)
    return max(Decimal('0'), price*(1-sell_fee))


def size_order(state, symbol, quote, decision, constraints, config, now=None):
    """Size shares, never cash loans or synthetic leverage; a zero size is a valid hold."""
    now = time.time() if now is None else now
    _decimal(now, '현재 시각', 0)
    settings = normalize_settings(state.get('strategy_settings'))
    instrument, factor = _instrument(symbol)
    currency = instrument['currency']
    side = decision.get('stance')
    if side not in ('BUY', 'SELL'):
        raise RiskError('매수 또는 매도 판단이 필요합니다.')
    weight = _decimal(decision.get('target_weight_pct'), '목표 종목 비중', 0, 30)
    nav = _decimal(constraints.get('portfolio_equity'), '평가 자산', 0)
    ask = _decimal(quote.get('ask'), '매도 호가', .00000001)
    bid = _decimal(quote.get('bid'), '매수 호가', .00000001)
    if bid > ask:
        raise RiskError('매수 호가가 매도 호가보다 높습니다.')
    slip = _decimal(config.slippage_bps, '슬리피지', 0, 1000)/10000
    fee = _decimal(config.fee_kr if currency == 'KRW' else config.fee_us, '수수료', 0, 1000)/10000
    tax = _decimal(config.sell_tax_kr, '매도세', 0, 1000)/10000 if currency == 'KRW' else Decimal('0')
    sell_fee = fee+tax
    held = _quantity(state.get('positions', {}).get(symbol, {}).get('quantity', 0), '보유 수량')
    max_buy = min(10000, _quantity(constraints.get('max_buy_quantity', 0), '최대 매수 수량'))
    max_sell = min(10000, held, _quantity(constraints.get('max_sell_quantity', held), '최대 매도 수량'))
    entry = _money(ask*(1+slip))
    if entry <= 0:
        raise RiskError('체결 단가가 최소 계산 단위보다 작습니다.')
    target_total = _floor(nav*weight/100/entry)
    target_delta = max(0, target_total-held) if side == 'BUY' else max(0, held-target_total)
    result = {'quantity': 0, 'target_quantity': target_delta, 'risk_quantity': 0,
              'max_buy_quantity': max_buy, 'estimated_stop_risk': 0.0, 'reason': '',
              'stop_price': None, 'take_profit_price': None, 'expires_at': None}
    if side == 'SELL':
        quantity = min(target_delta, max_sell)
        result.update(quantity=quantity, reason='목표 비중까지 보유 수량을 줄입니다.' if quantity else '이미 목표 비중 이하입니다.')
        return result

    stop_pct = _decimal(decision.get('stop_loss_pct'), '손절 비율', .2, 10)
    take_pct = _decimal(decision.get('take_profit_pct'), '익절 비율', 0, 40)
    if take_pct < stop_pct*Decimal('1.5'):
        raise RiskError('익절 비율은 손절 비율의 1.5배 이상이어야 합니다.')
    holding = _decimal(decision.get('max_holding_minutes'), '계획 보유 시간', 15, 240)
    if holding != holding.to_integral_value():
        raise RiskError('계획 보유 시간은 정수 분이어야 합니다.')
    session_end = _decimal(quote.get('session_end'), '장 종료 시각', 0)
    stop = _money(entry*(1-stop_pct/100))
    take = _money(entry*(1+take_pct/100))
    if not Decimal('0') < stop < entry < take:
        raise RiskError('손절·익절 가격이 유효한 간격을 만들지 못합니다.')
    expiry = min(float(session_end)-120, now+min(int(holding), settings['max_holding_minutes'])*60)
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
        quantity = _quantity(position.get('quantity', 0), '보유 수량')
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
    risk_quantity = max(0, _floor(min(trade_budget, total_budget)/risk_per_share))
    exposure_quantity = max(0, _floor(max(Decimal('0'), nav-exposure)/(entry*factor)))
    quantity = min(target_delta, risk_quantity, max_buy, exposure_quantity)
    result.update(quantity=quantity, risk_quantity=risk_quantity,
                  estimated_stop_risk=float(_money(risk_per_share*quantity)))
    if quantity:
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
