import copy
import math
import threading
import time
import uuid
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .agents import Agents, DESK_ROLES, market_context
from .desk import DESK_CALLS
from . import entry
from .evaluation import score_days, summarize, symbols_due, update_outcomes
from .live.shadow import shadow_summary
from .intel import FLOW_DAYS, FLOW_TTL, MarketIntel, RANK_KINDS, RANK_RETRY, RANK_TTL, parse_investor_trading, parse_rankings
from .config import ROLES
from .instruments import INSTRUMENTS, SYMBOLS
from .desk import DeskMixin
from .focus import FocusMixin, universe_mode
from . import universe
from . import shares
from .risk import min_take_pct, normalize_settings, RiskError, round_trip_cost_pct, trade_fee
from . import scorecard, verification
from .notify import Notifier
from .providers import DemoProvider, ProviderError, RateLimited, TossProvider
from .performance import performance_summary, record_performance
from .store import event
from .usage import LABELS as PROVIDER_LABELS


class RuleError(ValueError):
    pass


def money(x):
    return float(Decimal(str(x)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def equity(s, currency):
    value = s['cash'][currency]
    for symbol, pos in s['positions'].items():
        if SYMBOLS[symbol]['currency'] == currency:
            price = s['quotes'].get(symbol, {}).get('last', pos['average'])
            value += pos['quantity'] * price
    return money(value)


class Engine(DeskMixin, FocusMixin):
    def __init__(self, config, store, provider=None):
        self.c, self.store = config, store
        self.focus_attempts = {}   # market -> (time of the last build attempt, consecutive failures, session date)
        self.daily_cache = {}      # symbol -> (read at, completed daily bars of the last ~3 months)
        self.days_scored_at = 0
        self.benchmark_at = 0
        self.checked_at = 0
        self.notifier = Notifier(config.notify_url)
        self.signal_gate = config.mode != 'demo'   # demo answers are scripted, so there is nothing to conserve
        self.active_provider = None
        self.provider = provider or (DemoProvider() if config.mode == 'demo' else TossProvider(config))
        self.agents = Agents(config, store)
        self.intel = MarketIntel()
        self.intel_pause = .25     # seconds between ranking calls: gentle on the ranking group's small burst
        self.busy = threading.Lock()

    def boot(self):
        """Restore after a process start. A run the OWNER left running (`resume`, set by 시작 and cleared by 중지 or
        전량 매도) continues on its own, so a reboot or a crash does not silently leave the desk idle. Every in-flight
        cycle and pending proposal is dropped either way; the ledger is untouched. Waiting price plans (entry.py) survive a
        restart within `entry.RESTART_GRACE` of the last quote and are dropped after a longer outage. Paper trading only."""
        with self.store.edit() as s:
            resume = bool(s.get('resume')) and not s['liquidating']
            now = time.time()
            alive = max([q.get('received', 0) for q in (s.get('quotes') or {}).values()] or [0])
            s['running'] = resume
            s['generation'] += 1
            # A restart must not jump the queue: an analysis that was already scheduled for later (the pacing, a quota wait) keeps
            # its time, one that was due while the server was down starts at once. Pressing 시작 still starts at once.
            s['next_run'] = max(now, s.get('next_run') or 0) if resume else now
            s['scheduler_status'] = '거래 가능한 종목을 확인하고 있습니다.' if resume else '중지됨 · 시작 버튼으로 실행하세요.'
            for p in s['proposals']:
                if p['status'] == 'pending':
                    p['status'] = 'invalidated'
            for run in s['runs']:
                if run['status'] == 'running':
                    run['status'] = 'cancelled'
            # A short restart (a deploy) keeps the AI's price plans; after a longer outage they may be stale, so they go.
            kept = len(entry.waiting(s)) if resume and now-alive <= entry.RESTART_GRACE else 0
            if not kept:
                entry.close_waiting(s, 'cancelled', '서버가 다시 시작되어 취소했습니다.', now)
            else:
                event(s, f'재시작이 짧아 대기 중이던 조건 진입 계획 {kept}개는 그대로 유지합니다.')
            event(s, '서버가 다시 시작되어 실행 중이던 상태를 이어갑니다. 잔고·보유 종목은 그대로이고 진행 중이던 분석만 다시 합니다.'
                  if resume else '서버가 시작되었습니다. 잔고를 보존하고 중지 상태로 복구했습니다.')

    def shutdown(self):
        """Process is going away (deploy, reboot). Same clean-up as a stop, but the owner's wish to keep running stays."""
        with self.store.edit() as s:
            s['running'] = False
            s['generation'] += 1
            for p in s['proposals']:
                if p['status'] == 'pending':
                    p['status'] = 'invalidated'
            for run in s['runs']:
                if run['status'] == 'running':
                    run['status'] = 'cancelled'
            # waiting price plans stay: the next start decides whether they are still fresh enough (see boot)

    def current(self, gen):
        s = self.store.read()
        return s['running'] and s['generation'] == gen

    def start(self):
        with self.store.edit() as s:
            if s['liquidating']:
                raise RuleError('전량 매도 요청 처리 중입니다. 중지로 요청을 취소한 뒤 시작하세요.')
            s['resume'] = True
            if not s['running']:
                s['running'] = True
                s['generation'] += 1
                s['next_run'] = time.time()
                s['scheduler_status'] = '거래 가능한 종목을 확인하고 있습니다.'
                mode = '자동 모의체결' if s.get('execution_mode') == 'auto' else '사용자 승인'
                event(s, f'에이전트 실행을 시작했습니다. {mode} 모드입니다.')

    def stop(self):
        with self.store.edit() as s:
            s['running'], s['liquidating'], s['resume'] = False, False, False
            s['scheduler_status'] = '중지됨 · 보유 종목을 유지합니다.'
            s['generation'] += 1
            for p in s['proposals']:
                if p['status'] == 'pending':
                    p['status'] = 'invalidated'
            for run in s['runs']:
                if run['status'] == 'running':
                    run['status'] = 'cancelled'
            entry.close_waiting(s, 'cancelled', '중지로 취소했습니다.', time.time())
            event(s, '중지했습니다. 대기 제안과 조건 진입을 무효화하고 보유 종목은 유지합니다.')

    def set_execution(self, mode):
        if mode not in ('manual', 'auto'):
            raise RuleError('사용자 승인 또는 자동 모의체결을 선택하세요.')
        with self.store.edit() as s:
            if s['running'] or s['liquidating']:
                raise RuleError('먼저 중지한 뒤 실행 방식을 변경하세요.')
            s['execution_mode'] = mode
            s['generation'] += 1
            for p in s['proposals']:
                if p['status'] == 'pending':
                    p['status'] = 'invalidated'
            entry.close_waiting(s, 'cancelled', '운용 방식 변경으로 취소했습니다.', time.time())
            label = '자동 모의체결' if mode == 'auto' else '사용자 승인'
            event(s, f'{label} 모드로 설정했습니다. 시작 버튼을 누르면 실행됩니다.')

    def new_experiment(self, krw, usd, name='', max_order_pct=30, strategy_mode='legacy', strategy_settings=None):
        values = (krw, usd, max_order_pct)
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
            raise RuleError('원금과 매수 한도는 유효한 숫자여야 합니다.')
        if not (0 <= krw <= 1e12 and 0 <= usd <= 1e9 and 1 <= max_order_pct <= 100):
            raise RuleError('원금 범위와 매수 한도(1~100%)를 확인하세요.')
        krw, usd = money(krw), money(usd)
        if krw == 0 and usd == 0:
            raise RuleError('최소 한 통화의 가상 원금이 필요합니다.')
        if not isinstance(name, str) or len(name.strip()) > 80:
            raise RuleError('실험 이름은 80자 이내로 입력하세요.')
        name = name.strip() or datetime.now(timezone.utc).strftime('실험 %Y-%m-%d %H:%M UTC')
        if strategy_mode not in ('legacy', 'intraday'):
            raise RuleError('지원하지 않는 전략입니다.')
        try:
            settings = normalize_settings(strategy_settings) if strategy_mode == 'intraday' else {}
            result = self.store.reset_experiment(krw, usd, name, max_order_pct/100, strategy_mode, settings)
        except ValueError as exc:
            raise RuleError(str(exc)) from None
        if strategy_mode == 'intraday':
            # The bar this experiment must clear is fixed now, before any result exists (verification.py).
            with self.store.edit() as s:
                s['verification'] = result['verification'] = verification.start(self.c, s['started_at'])
        return result

    def fractional(self, currency):
        """US instruments trade in fractional shares (a simulation assumption, FRACTIONAL_US); Korean ones in whole shares."""
        return currency == 'USD' and self.c.fractional_us

    @staticmethod
    def position_ratio(state):
        """The most of the account one name may take: a day-trading experiment sets its own (up to 100%), the rest keep 30%."""
        if state.get('strategy_mode') != 'intraday':
            return .30
        return (state.get('strategy_settings') or {}).get('max_position_pct', 30)/100

    def order_constraints(self, s, symbol, q):
        currency = SYMBOLS[symbol]['currency']
        nav = equity(s, currency)
        price = money(q['ask']*(1+self.c.slippage_bps/10000))
        unit_cost = price*(1+(self.c.fee_kr if currency == 'KRW' else self.c.fee_us)/10000)
        held = s['positions'].get(symbol, {}).get('quantity', 0)
        order_ratio, name_ratio = s.get('max_order_ratio', .10), self.position_ratio(s)
        fractional = self.fractional(currency)
        if fractional:
            # Money-based limits, rounded down to four decimals, with two cents of room for the cent rounding of fees.
            def afford(amount):
                return shares.floor_to(max(0.0, amount-.02)/unit_cost, True)
            room = min(afford(s['cash'][currency]), afford(nav*order_ratio), afford(nav*name_ratio-held*price),
                       shares.floor_to(q['ask_size'], True), Decimal(shares.MAX_QUANTITY))
            max_buy = 0 if shares.below_minimum(room, price, True) else shares.number(room)
            max_sell = min(held, q['bid_size']) if s.get('strategy_mode') == 'intraday' else held
        else:
            max_buy = max(0, min(int(s['cash'][currency]/unit_cost),
                                 int(nav*order_ratio/unit_cost),
                                 int(nav*name_ratio/price)-held, int(q['ask_size']), 10000))
            max_sell = min(held, int(q['bid_size'])) if s.get('strategy_mode') == 'intraday' else held
        return {'max_order_equity_ratio': order_ratio,
                'max_position_equity_ratio': name_ratio, 'portfolio_equity': nav,
                'round_trip_cost_pct': round(round_trip_cost_pct(self.c, currency, q, symbol), 3),
                'min_take_profit_pct': round(min_take_pct(self.c, currency, q, symbol), 3),
                'max_buy_quantity': max_buy, 'max_sell_quantity': max_sell,
                'integer_shares_only': not fractional, 'fractional_shares': fractional,
                'shorting': False, 'leverage': False,
                'leveraged_etfs': s.get('strategy_settings', {}).get('include_leveraged_etfs', False)}

    def position_cap(self, state, currency):
        """The most one name may cost right now: the smaller of the per-order and per-name limits that `order_constraints`
        applies, less trading costs and a 3% cushion for the price moving before the order."""
        ratio = min(state.get('max_order_ratio', .10), self.position_ratio(state))
        fee = self.c.fee_kr if currency == 'KRW' else self.c.fee_us
        costs = (1+self.c.slippage_bps/10000)*(1+fee/10000)*1.03
        return equity(state, currency)*ratio/costs

    def request_cycle(self, symbol):
        if symbol not in SYMBOLS:
            raise RuleError('지원하지 않는 관심종목입니다.')
        with self.store.edit() as s:
            symbols = [i['symbol'] for i in self.active_instruments(s)]
            if symbol not in symbols:
                raise RuleError('이 실험에서 허용한 관심종목이 아닙니다.')
            if not s['running']:
                raise RuleError('시작 버튼을 먼저 누르세요.')
            if any(x['status'] == 'running' for x in s['runs']):
                raise RuleError('현재 분석이 끝난 뒤 요청하세요.')
            s['cursor'] = symbols.index(symbol)
            s['requested_symbol'] = symbol
            s['next_run'] = time.time()

    def refresh(self):
        try:
            if hasattr(self.provider, 'set_universe'):
                state = self.store.read()
                symbols = {i['symbol'] for i in self.active_instruments(state)} | set(state['positions'])
                self.provider.set_universe(sorted(symbols))
            quotes = self.provider.quotes()
            with self.store.edit() as s:
                for symbol, q in quotes.items():
                    cached = s['quotes'].get(symbol, {})
                    if all(q[key] >= cached.get(key, 0) for key in ('received', 'asof', 'book_asof')):
                        s['quotes'][symbol] = q
                s['last_error'] = ''
                now = time.time()
                for p in s['proposals']:
                    if p['status'] == 'pending' and p['expires'] < now:
                        p['status'] = 'expired'
                record_performance(s, now=now, quote_age=self.c.quote_age)
                self.update_desk_risk(s, now=now)
                update_outcomes(s, now, self.c.quote_age)
        except Exception as e:
            message = str(e) if isinstance(e, ProviderError) else '시세 갱신 실패. 최신 시세를 받을 때까지 거래를 차단합니다.'
            with self.store.edit() as s:
                s['last_error'] = message

    def score_days(self, now=None):
        """Score month decisions against later daily closes. The bars are read first, outside the state lock; rare (each
        15 minutes at most) and silent when nothing has waited long enough."""
        now = time.time() if now is None else now
        if now-self.days_scored_at < 900:
            return
        self.days_scored_at = now
        needed = symbols_due(self.store.read().get('evaluations', []), now)
        bars = {}
        for symbol in sorted(needed):
            try:
                bars[symbol] = self.daily_bars(symbol, now)
            except (ProviderError, KeyError, ValueError):
                continue
        if bars:
            with self.store.edit() as s:
                score_days(s, now, bars)

    def refresh_benchmark(self, now=None):
        """Daily closes of the index ETFs the experiment is compared with (scorecard.BENCHMARKS), read at most every 15 minutes
        outside the state lock. The starting close is fixed the first time and never moves."""
        now = time.time() if now is None else now
        if now-self.benchmark_at < 900:
            return
        self.benchmark_at = now
        state = self.store.read()
        if state.get('strategy_mode') != 'intraday':
            return
        rows = {}
        for currency, symbol in scorecard.BENCHMARKS.items():
            if (state.get('initial') or {}).get(currency, 0) <= 0:
                continue
            try:
                rows[currency] = (symbol, self.daily_bars(symbol, now))
            except (ProviderError, KeyError, ValueError):
                continue
        if not rows:
            return
        with self.store.edit() as s:
            if s['experiment_id'] != state['experiment_id']:
                return
            bench = s.setdefault('benchmark', {})
            for currency, (symbol, bars) in rows.items():
                found = scorecard.benchmark_entry(symbol, bars, s['started_at'], bench.get(currency))
                if found:
                    bench[currency] = found

    CHECK_EVERY = 24*3600          # a provider that has not answered for this long is sent one tiny request
    CHECK_RETRY = 3*3600           # a failed check is repeated after this long

    def usable_providers(self):
        gate = self.agents.gate
        return list(gate.plan()[0]) if gate.enabled else list(self.c.provider_order)

    def check_providers(self, now=None):
        """A fallback AI that has not answered for a day gets one tiny request as soon as it is usable again, so a broken
        fallback (a new model, a CLI update) shows on the dashboard before the desk has to rely on it. One check per round,
        at most every ten minutes; a real answer counts as a check."""
        now = time.time() if now is None else now
        if self.c.mode == 'demo' or not self.c.bridge_configured or now-self.checked_at < 600:
            return
        self.checked_at = now
        checks = self.store.read().get('ai_checks') or {}
        for provider in self.usable_providers():
            if provider not in ('claude', 'codex'):
                continue
            last = checks.get(provider) or {}
            answered = max(self.agents.answered.get(provider, 0), last.get('time', 0) if last.get('ok') else 0)
            if now-answered < self.CHECK_EVERY or (last and not last.get('ok') and now-last.get('time', 0) < self.CHECK_RETRY):
                continue
            result = self.agents.self_check(provider)
            label = PROVIDER_LABELS.get(provider, provider)
            with self.store.edit() as s:
                s.setdefault('ai_checks', {})[provider] = dict(result, time=time.time())
                event(s, f'{label} 응답 확인: ' + (f'정상 ({result["model"]}, {result["seconds"]}초)' if result['ok']
                                                     else '실패 - '+result['message']), 'info' if result['ok'] else 'warning')
            return

    def notify(self):
        """Phone notifications for new fills and warnings (notify.py); nothing is read while they are off."""
        if self.notifier.enabled:
            self.notifier.poll(self.store.read())

    def refresh_intel(self):
        """Official ranking and investor-flow reads on their own slow cadence.

        A failure here only removes extra context for the AI; quotes, exits and trading never wait on it.
        """
        if not hasattr(self.provider, 'rankings'):
            return
        state = self.store.read()
        now = time.time()
        universe = self.active_instruments(state)
        today = datetime.now(ZoneInfo('Asia/Seoul')).date().isoformat()
        for market in sorted({item['market'] for item in universe}):
            quotes = [state['quotes'].get(item['symbol'], {}) for item in universe if item['market'] == market]
            is_open = any(q.get('session_start', 0) <= now < q.get('session_end', 0) for q in quotes)
            age, attempt = self.intel.rank_age(market), self.intel.attempt_age(market)
            missing = [entry for entry in RANK_KINDS if entry[0] not in self.intel.kinds(market)]
            due = attempt is None or attempt >= RANK_RETRY
            # One snapshot while closed keeps the cache warm; refresh on a schedule only while it trades.
            # A list that failed is retried on its own soon, without refetching the ones that worked.
            full = due and (age is None or (is_open and age >= RANK_TTL))
            retry = due and is_open and bool(missing) and not full
            if full or retry:
                self.intel.mark_attempt(market)
                kinds = {}
                for key, kind, duration in (RANK_KINDS if full else missing):
                    try:
                        parsed = parse_rankings(self.provider.rankings(market, kind, duration, 100))
                    except RateLimited as exc:
                        self.intel.note_error(market, key, exc)
                        break                       # the group is busy: stop asking until the next round
                    except ProviderError as exc:
                        self.intel.note_error(market, key, exc)
                        continue
                    finally:
                        time.sleep(self.intel_pause)
                    if parsed is None:
                        self.intel.note_error(market, key, '응답 형식을 해석하지 못했습니다.')
                        continue
                    self.intel.note_error(market, key, '')
                    kinds[key] = parsed['items']
                if kinds:
                    self.intel.store_rankings(market, kinds, merge=not full)
            if market != 'KR':
                continue
            for item in universe:
                if item['market'] != 'KR':
                    continue
                flow_age = self.intel.flow_age(item['symbol'])
                if flow_age is not None and not (is_open and flow_age >= FLOW_TTL):
                    continue
                try:
                    data = parse_investor_trading(self.provider.investor_trading(item['symbol'], FLOW_DAYS), today)
                    self.intel.note_error('KR', 'flow-'+item['symbol'], '' if data else '응답 형식을 해석하지 못했습니다.')
                except ProviderError as exc:
                    self.intel.note_error('KR', 'flow-'+item['symbol'], exc)
                    data = None
                # A failed or unsupported symbol is retried on the normal schedule, never in a tight loop.
                self.intel.store_flow(item['symbol'], data)

    def validate_quote(self, q, symbol):
        if q.get('mode') != self.c.mode or q.get('symbol') != symbol or q.get('currency') != SYMBOLS[symbol]['currency']:
            raise RuleError('시세의 모드·종목·통화가 다릅니다.')
        if not q.get('tradable'):
            raise RuleError('정규장 또는 유효한 호가를 기다리고 있습니다.')
        now = time.time()
        if not q.get('session_start', 0) <= now < q.get('session_end', 0):
            raise RuleError('거래 가능 시간이 종료되었습니다. 다음 정규장을 기다립니다.')
        for key in ('asof', 'book_asof', 'received'):
            age = now-q.get(key, 0)
            if not math.isfinite(age) or not -5 <= age <= self.c.quote_age:
                raise RuleError('시세가 오래되었거나 기준 시각이 없습니다. 최신 시세가 필요합니다.')
        for key in ('last', 'bid', 'ask'):
            if not math.isfinite(q[key]) or q[key] <= 0:
                raise RuleError('유효하지 않은 가격입니다.')
        if q['bid'] > q['ask']:
            raise RuleError('유효하지 않은 호가입니다.')

    def quote_for_trade(self, symbol):
        q = self.provider.quote(symbol)
        self.validate_quote(q, symbol)
        return q

    def fill(self, s, symbol, side, qty, q, ref, liquidation=False, limit=None):
        self.validate_quote(q, symbol)
        s['quotes'][symbol] = q
        currency = SYMBOLS[symbol]['currency']
        fractional = self.fractional(currency)
        if side not in ('BUY', 'SELL') or not shares.is_valid(qty, fractional):
            raise RuleError('수량 또는 매매 방향이 올바르지 않습니다.')
        qty_d = shares.dec(qty)
        qty = shares.number(qty_d)
        depth = q['ask_size'] if side == 'BUY' else q['bid_size']
        if depth < qty:
            raise RuleError('최우선 호가 잔량이 부족합니다. 이번 주문은 체결하지 않습니다.')
        slip = Decimal(str(self.c.slippage_bps))/10000
        price = money(Decimal(str(q['ask'] if side == 'BUY' else q['bid'])) *
                      (1+slip if side == 'BUY' else 1-slip))
        if limit is not None:
            # A take-profit is a resting limit order (one side of a bracket): once the market reaches it, it fills at its own price.
            if side != 'SELL' or not q['bid'] >= limit:
                raise RuleError('지정가에 도달하지 않아 체결하지 않습니다.')
            price = money(limit)
        gross = money(Decimal(str(price))*qty_d)
        fee = money(trade_fee(self.c, symbol, side, gross))
        pos = s['positions'].get(symbol, {'quantity': 0, 'average': 0})
        cost_basis = pos.get('cost_basis', money(pos['average']*pos['quantity']))
        if side == 'BUY':
            if s.get('strategy_mode') == 'intraday':
                self.desk_buy_allowed(s, symbol)
            total = money(Decimal(str(gross))+Decimal(str(fee)))
            if total > s['cash'][currency]:
                raise RuleError('해당 통화의 가상 현금이 부족합니다.')
            s['quotes'][symbol] = q
            for held in s['positions']:
                if SYMBOLS[held]['currency'] == currency:
                    try:
                        self.validate_quote(s['quotes'].get(held, {}), held)
                    except (RuleError, KeyError):
                        raise RuleError('보유 종목의 최신 평가 시세가 부족해 신규 매수를 차단했습니다.') from None
            nav = equity(s, currency)
            ratio, name_ratio = s.get('max_order_ratio', .10), self.position_ratio(s)
            if not .01 <= ratio <= 1:
                raise RuleError('실험의 매수 한도가 유효하지 않습니다.')
            if shares.below_minimum(qty, price, fractional):
                raise RuleError('소수점 주문은 최소 1달러 이상이어야 합니다.')
            if total > nav*ratio:
                raise RuleError(f'한 번의 매수는 해당 통화 자산의 {ratio*100:g}%까지 허용됩니다.')
            new_qty = shares.dec(pos['quantity'])+qty_d
            if float(new_qty)*price > nav*name_ratio:
                raise RuleError(f'한 종목의 비중은 해당 통화 자산의 {name_ratio*100:g}%까지 허용됩니다.')
            new_cost = money(Decimal(str(cost_basis))+Decimal(str(total)))
            average = money(Decimal(str(new_cost))/new_qty)
            s['positions'][symbol] = {**pos, 'quantity': shares.number(new_qty), 'average': average, 'cost_basis': new_cost}
            s['cash'][currency] = money(Decimal(str(s['cash'][currency]))-Decimal(str(total)))
            realized = 0
        else:
            held_d = shares.dec(pos['quantity'])
            if qty_d > held_d:
                raise RuleError('보유 수량보다 많이 매도할 수 없습니다.')
            if qty_d < held_d and shares.below_minimum(qty, price, fractional):
                raise RuleError('소수점 주문은 최소 1달러 이상이어야 합니다. 남은 수량을 모두 매도하세요.')
            s['cash'][currency] = money(Decimal(str(s['cash'][currency]))+Decimal(str(gross))-Decimal(str(fee)))
            allocated = cost_basis if qty_d == held_d else money(Decimal(str(cost_basis))*qty_d/held_d)
            realized = money(Decimal(str(gross))-Decimal(str(fee))-Decimal(str(allocated)))
            remaining = held_d-qty_d
            if remaining:
                s['positions'][symbol]['quantity'] = shares.number(remaining)
                s['positions'][symbol]['cost_basis'] = money(Decimal(str(cost_basis))-Decimal(str(allocated)))
                s['positions'][symbol]['average'] = money(Decimal(str(s['positions'][symbol]['cost_basis']))/remaining)
            else:
                s['positions'].pop(symbol, None)
        s['revision'] += 1
        s['quotes'][symbol] = q
        trade = {'id': str(uuid.uuid4()), 'reference': ref, 'time': time.time(), 'symbol': symbol,
                 'side': side, 'quantity': qty, 'price': price, 'fee': fee, 'currency': currency,
                 'realized': realized, 'mode': self.c.mode, 'liquidation': liquidation,
                 'execution_mode': 'liquidation' if liquidation else s.get('execution_mode', 'manual'),
                 'quote_asof': q['book_asof']}
        s['trades'].append(trade)
        record_performance(s, quote_age=self.c.quote_age, force=True)
        event(s, f'{SYMBOLS[symbol]["name"]} {qty}주 모의 {"매수" if side == "BUY" else "매도"} 체결')
        return trade

    def approve(self, proposal_id):
        snapshot = self.store.read()
        p = next((x for x in snapshot['proposals'] if x['id'] == proposal_id), None)
        if not p:
            raise RuleError('제안을 찾을 수 없습니다.')
        if p['status'] == 'filled':
            return {'status': 'already_filled'}
        if p['status'] != 'pending' or not snapshot['running'] or snapshot.get('execution_mode') == 'auto':
            raise RuleError('승인할 수 없는 제안입니다.')
        q = self.quote_for_trade(p['symbol'])
        if p.get('strategy_mode') == 'intraday':
            # Persist market observations and a latched daily halt even when the
            # subsequent order-validation transaction rejects this proposal.
            with self.store.edit() as s:
                if s['running'] and s['generation'] == p['generation']:
                    s['quotes'][p['symbol']] = q
                    self.update_desk_risk(s)
        with self.store.edit() as s:
            p = next((x for x in s['proposals'] if x['id'] == proposal_id), None)
            if p and p['status'] == 'filled':
                return {'status': 'already_filled'}
            if not p or p['status'] != 'pending' or not s['running'] or s['liquidating'] or s.get('execution_mode') == 'auto':
                raise RuleError('중지되었거나 이미 처리된 제안입니다.')
            if p['generation'] != s['generation'] or p['mode'] != self.c.mode:
                raise RuleError('이전 실행의 제안입니다.')
            if p['expires'] < time.time():
                raise RuleError('제안의 유효시간이 지났습니다. 다시 분석하세요.')
            if p['revision'] != s['revision']:
                raise RuleError('계좌가 변경되었습니다. 다시 분석하세요.')
            fresh = q['ask'] if p['side'] == 'BUY' else q['bid']
            if abs(fresh/p['reference_price']-1) > .005:
                raise RuleError('분석 당시 가격에서 0.5% 이상 변했습니다. 다시 분석하세요.')
            sizing = None
            if p.get('strategy_mode') == 'intraday':
                try:
                    sizing = self.validate_desk_proposal(s, p, q)
                except RiskError as exc:
                    raise RuleError(str(exc)) from None
            self.fill(s, p['symbol'], p['side'], p['quantity'], q, p['id'])
            if sizing is not None:
                self.apply_desk_plan(s, p['symbol'], p, sizing)
            if p.get('watch_id'):
                s['trades'][-1]['entry_watch'] = p['watch_id']
            p['status'] = 'filled'
            for other in s['proposals']:
                if other['status'] == 'pending':
                    other['status'] = 'invalidated'
        return {'status': 'filled'}

    def reject(self, proposal_id):
        with self.store.edit() as s:
            p = next((p for p in s['proposals'] if p['id'] == proposal_id), None)
            if p and p['status'] == 'pending':
                p['status'] = 'rejected'
                event(s, '매매 제안을 거절했습니다.')

    def liquidate(self):
        with self.store.edit() as s:
            s['running'], s['resume'] = False, False
            s['generation'] += 1
            for p in s['proposals']:
                if p['status'] == 'pending':
                    p['status'] = 'invalidated'
            for run in s['runs']:
                if run['status'] == 'running':
                    run['status'] = 'cancelled'
            entry.close_waiting(s, 'cancelled', '전량 매도 요청으로 취소했습니다.', time.time())
            s['liquidating'] = bool(s['positions'])
            event(s, '전량 모의매도를 요청했습니다. 거래 가능 시간과 호가를 확인하며 남은 수량을 처리합니다.')

    def process_liquidation(self):
        snap = self.store.read()
        if not snap['liquidating']:
            return
        gen = snap['generation']
        for symbol in list(snap['positions']):
            try:
                q = self.quote_for_trade(symbol)
                with self.store.edit() as s:
                    if not s['liquidating'] or s['generation'] != gen:
                        return
                    qty = min(s['positions'].get(symbol, {}).get('quantity', 0), q['bid_size'], 10000)
                    if qty:
                        self.fill(s, symbol, 'SELL', qty, q, 'liquidation-'+str(gen), True)
            except (RuleError, ProviderError):
                continue
        with self.store.edit() as s:
            if s['liquidating'] and s['generation'] == gen and not s['positions']:
                s['liquidating'] = False
                event(s, '전량 모의매도가 완료되었습니다. 중지 상태입니다.')

    def wait_for_cycle(self, generation, message, next_run=None):
        with self.store.edit() as s:
            if s['running'] and s['generation'] == generation:
                s['scheduler_status'] = message
                s['next_run'] = next_run if next_run is not None else time.time()+30

    def begin_ai_cycle(self, gen):
        """Choose this cycle's AI providers BEFORE anything is spent: skip the ones that are exhausted or past the
        switch level (see usage.py). False means none is usable right now, so the cycle waits quietly, a minute at a
        time, until the earliest reset instead of starting and failing halfway."""
        gate = self.agents.gate
        usable, statuses, message = gate.plan()
        if not gate.enabled or not self.c.provider_order:
            self.agents.cycle_order = None
            return True
        if not usable:
            self.agents.cycle_order = None
            self.wait_for_cycle(gen, message, time.time()+60)
            return False
        self.agents.cycle_order = usable
        if usable[0] != self.active_provider:
            previous, self.active_provider = self.active_provider, usable[0]
            order = list(self.c.provider_order)
            # Only providers ranked ahead of the one in use were "skipped"; a later one being unusable is not news.
            skipped = [s for s in statuses if s['state'] != 'ok' and order.index(s['name']) < order.index(usable[0])]
            label = PROVIDER_LABELS.get(usable[0], usable[0])
            if skipped:
                with self.store.edit() as s:
                    event(s, f'이번 분석은 {label}로 진행합니다. ' + ' · '.join(f'{x["label"]} {x["note"]}' for x in skipped), 'warning')
            elif previous is not None and usable[0] == order[0]:
                with self.store.edit() as s:
                    event(s, f'{label}로 되돌아왔습니다. 사용량이 전환 기준 아래입니다.')
        return True

    def cycle(self):
        if not self.busy.acquire(False):
            return
        run_id = str(uuid.uuid4())
        try:
            snapshot = self.store.read()
            now = time.time()
            if not snapshot['running'] or snapshot['next_run'] > now:
                return
            if snapshot.get('strategy_mode') == 'intraday':
                return self.desk_cycle(snapshot, run_id)
            gen = snapshot['generation']
            day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
            if self.c.mode != 'demo' and self.c.gemini_only and self.c.ai_daily_calls-snapshot['daily_ai'].get(day, 0) < len(ROLES):
                self.wait_for_cycle(gen, '오늘의 AI 호출 한도 대기 · 5개 역할을 완료할 잔여 호출이 필요합니다.',
                                    (int(now)//86400+1)*86400+1)
                return

            selected = None
            message = '정규장과 최신 시세를 기다립니다. 30초 후 다시 확인합니다.'
            symbols = [i['symbol'] for i in self.active_instruments(snapshot)]
            for offset in range(len(symbols)):
                index = (snapshot['cursor']+offset) % len(symbols)
                candidate = symbols[index]
                currency = SYMBOLS[candidate]['currency']
                if snapshot['initial'][currency] <= 0:
                    continue
                if not self.current(gen):
                    return
                try:
                    self.validate_quote(snapshot['quotes'].get(candidate, {}), candidate)
                    q = self.quote_for_trade(candidate)
                    limits = self.order_constraints(snapshot, candidate, q)
                    if not limits['max_buy_quantity'] and not limits['max_sell_quantity']:
                        message = '현금·매수 한도 안에서 1주를 살 수 있는 종목이 없습니다. 가격을 다시 확인합니다.'
                        continue
                    selected = (candidate, index, q)
                    break
                except RuleError:
                    continue
                except ProviderError as exc:
                    message = str(exc)
            if selected is None:
                self.wait_for_cycle(gen, message)
                return
            if not self.begin_ai_cycle(gen):
                return
            symbol, index, quote = selected
            with self.store.edit() as s:
                if not s['running'] or s['generation'] != gen:
                    return
                s['cursor'] = index+1
                s.pop('requested_symbol', None)
                rev = s['revision']
                s['next_run'] = time.time()+self.c.interval_seconds
                s['scheduler_status'] = SYMBOLS[symbol]['name']+' 분석 중'
                s['runs'].append({'id': run_id, 'symbol': symbol, 'time': time.time(),
                                  'status': 'running', 'reports': [], 'execution_mode': s['execution_mode']})
                account = copy.deepcopy(s)
            candles = self.provider.candles(symbol)
            context = market_context(symbol, quote, candles, account)
            context['constraints'].update(self.order_constraints(account, symbol, quote))
            for role, name in ROLES:
                if not self.current(gen):
                    return
                with self.store.edit() as s:
                    if not s['running'] or s['generation'] != gen:
                        return
                    run = next(x for x in s['runs'] if x['id'] == run_id)
                    run['active_role'] = role
                report = self.agents.run(role, context, gen)
                report.update({'role': role, 'name': name, 'time': time.time()})
                context['reports'].append(report)
                with self.store.edit() as s:
                    if not s['running'] or s['generation'] != gen:
                        return
                    next(x for x in s['runs'] if x['id'] == run_id)['reports'].append(report)
            # An analysis may take minutes. Validate a new quote before creating an actionable proposal.
            fresh_quote = self.quote_for_trade(symbol)
            with self.store.edit() as s:
                if not s['running'] or s['generation'] != gen:
                    return
                run = next(x for x in s['runs'] if x['id'] == run_id)
                run['status'] = 'completed'
                run['active_role'] = None
                s['scheduler_status'] = '분석 완료 · 다음 분석 시각을 기다립니다.'
                decision = context['reports'][-1]
                if decision['stance'] in ('BUY', 'SELL') and decision['quantity'] > 0 and rev == s['revision']:
                    side = decision['stance']
                    proposal = {'id': str(uuid.uuid4()), 'symbol': symbol, 'side': side,
                                'quantity': decision['quantity'], 'reference_price': quote['ask'] if side == 'BUY' else quote['bid'],
                                'created': time.time(), 'expires': time.time()+self.c.proposal_seconds,
                                'generation': gen, 'revision': rev, 'mode': self.c.mode, 'status': 'pending',
                                'summary': decision['summary'], 'risks': decision['risks'], 'run_id': run_id}
                    # Preliminary deterministic validation against the analysis snapshot.
                    try:
                        reference = quote['ask'] if side == 'BUY' else quote['bid']
                        fresh_price = fresh_quote['ask'] if side == 'BUY' else fresh_quote['bid']
                        if abs(fresh_price/reference-1) > .005:
                            raise RuleError('분석 중 가격이 0.5% 이상 변해 제안을 보류했습니다.')
                        self.fill(copy.deepcopy(s), symbol, side, decision['quantity'], fresh_quote, 'validation')
                        if s['execution_mode'] == 'auto':
                            self.fill(s, symbol, side, decision['quantity'], fresh_quote, proposal['id'])
                            proposal['status'] = 'filled'
                            proposal['execution_mode'] = 'auto'
                            for p in s['proposals']:
                                if p['status'] == 'pending':
                                    p['status'] = 'invalidated'
                            s['proposals'].append(proposal)
                            event(s, SYMBOLS[symbol]['name']+' 분석을 반영해 자동 모의체결했습니다.')
                        else:
                            for p in s['proposals']:
                                if p['status'] == 'pending' and p['symbol'] == symbol:
                                    p['status'] = 'superseded'
                            s['proposals'].append(proposal)
                            event(s, SYMBOLS[symbol]['name']+' 분석 완료. 승인을 기다립니다.')
                    except RuleError as e:
                        run['blocked'] = str(e)
                        event(s, '서버 규칙이 제안을 차단했습니다: '+str(e), 'warning')
                else:
                    event(s, SYMBOLS[symbol]['name']+' 분석 완료. 관망 또는 계좌 변경으로 주문하지 않습니다.')
        except Exception as e:
            message = str(e) if isinstance(e, (RuleError, ProviderError)) else '분석 중 오류가 발생했습니다. 거래하지 않고 기록합니다.'
            with self.store.edit() as s:
                run = next((x for x in s['runs'] if x['id'] == run_id), None)
                if run and run['status'] == 'running':
                    run['status'], run['error'] = 'error', message
                    event(s, message, 'warning')
        finally:
            self.agents.cycle_order = None
            self.busy.release()

    def public_state(self):
        s = self.store.read()
        tracking = copy.deepcopy(s.get('performance') or {})        # with the daily equity record the summary leaves out
        s['instruments'] = self.active_instruments(s)
        s['equity'] = {c: equity(s, c) for c in ('KRW', 'USD')}
        s['performance'] = performance_summary(s, quote_age=self.c.quote_age)
        history = s['history']
        if len(history) > 1000:
            s['history'] = history[::math.ceil(len(history)/999)]
            if s['history'][-1] != history[-1]:
                s['history'].append(history[-1])
        s['intel'] = self.intel.status()
        # The published lists without their working data; the reference prices stay server-side.
        s['focus'] = {market: {k: v for k, v in entry.items() if k not in ('ranked', 'metrics')}
                      for market, entry in (s.get('focus') or {}).items()}
        s['focus_eval'] = universe.summarize(s.get('focus_history') or [])
        s['focus_history'] = [{k: v for k, v in r.items() if k != 'ref'} for r in (s.get('focus_history') or [])[-10:]]
        s['focus_config'] = {'mode': universe_mode(s) if s.get('strategy_mode') == 'intraday' else 'fixed',
                             'per_market': self.c.focus_per_market, 'ai': self.c.focus_ai,
                             'profile': self.focus_profile(s) if s.get('strategy_mode') == 'intraday' else None}
        s['live'] = {'config': self.c.live.public(), 'shadow': shadow_summary(s),
                     'halt': (s.get('live') or {}).get('halt', {'active': False})}
        s.pop('shadow_orders', None)
        evaluations = s.get('evaluations', [])
        s['evaluation'] = summarize(evaluations)
        card = scorecard.report(s['trades'])                         # every trade, before the list is cut for the page
        s['scorecard'] = card
        s['verification'] = verification.judge(s, tracking=tracking, report=card, evaluation=s['evaluation'], config=self.c,
                                               now=time.time())
        s['evaluations'] = [{k: v for k, v in e.items() if k != 'candidates'} for e in evaluations[-30:]]
        watches = s.get('watches') or []
        s['watches'] = [w for w in watches if w['status'] == 'waiting'] + [w for w in watches if w['status'] != 'waiting'][-8:]
        s['trades_count'] = len(s['trades'])
        s['trades'] = s['trades'][-100:]
        s['server_time'] = time.time()
        s['ai'] = self.agents.gate.summary()
        s['config'] = {'ai_configured': self.c.ai_configured,
                       'toss_configured': bool(self.c.toss_id and self.c.toss_secret),
                       'toss_rate': {group: dict(info) for group, info in (getattr(self.provider, 'rates', None) or {}).items()},
                       'model': self.c.model, 'providers': self.c.provider_order, 'daily_limit': self.c.ai_daily_calls,
                       'poll': self.c.poll_seconds, 'interval': self.interval_plan(s)[0], 'interval_active': self.interval_plan(s)[1],
                       'conditional_entry': self.c.conditional_entry, 'min_take_cost_ratio': self.c.min_take_cost_ratio,
                       'ai_light_roles': list(self.c.ai_light_roles), 'models': {'main': self.c.claude_model, 'light': self.c.claude_model_light},
                       'fee_kr_bps': self.c.fee_kr, 'fee_us_bps': self.c.fee_us,
                       'sell_tax_kr_bps': self.c.sell_tax_kr, 'slippage_bps': self.c.slippage_bps,
                       'roles': [{'id': role, 'name': name} for role, name in (DESK_ROLES if s.get('strategy_mode') == 'intraday' else ROLES)],
                       'analysis_calls_per_cycle': DESK_CALLS if s.get('strategy_mode') == 'intraday' else len(ROLES)}
        return s
