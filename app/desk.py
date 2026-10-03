"""Bounded research workflow and independent, paper-only exit monitoring (month swing or same-session horizon)."""
import copy
import math
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import afterexit, entry, evidence, gate, history, pacing, reuse
from .agents import DESK_ROLES, STOPPED, market_context
from .config import INSTRUMENTS as BASE_INSTRUMENTS
from .instruments import INSTRUMENTS, SYMBOLS
from .evaluation import mid, record_decision
from .live.shadow import record_shadow
from .rules import RULE_NAMES, signals
from .performance import performance_summary
from .providers import ProviderError
from .risk import RiskError, horizon_of, is_etf, min_take_pct, round_trip_cost_pct, size_order, trailed_stop
from .store import event
from .universe import ROTATION_WARMUP

MARKET_LABELS = {'KR': '국내', 'US': '미국'}
DESK_CALLS = len(DESK_ROLES)+1  # stock selection + six research roles
DAILY_TTL = 20*60               # seconds a name's daily bars are reused (they only change once a day)


def _pct(new, old):
    return round((new/old-1)*100, 3) if old else None


def candidate_summary(symbol, quote, candles, state, limits, now, extra=None):
    """Compact, server-computed facts per candidate so the selector compares like with like."""
    closes = [c['close'] for c in candles]
    moves = [math.log(b/a) for a, b in zip(closes[-21:], closes[-20:]) if a > 0 and b > 0]
    mean = sum(moves)/len(moves) if moves else 0
    volumes = [c['volume'] for c in candles]
    prior = volumes[-25:-5]
    mid = (quote['ask']+quote['bid'])/2
    position = state['positions'].get(symbol, {})
    runs = [r for r in state.get('runs', []) if r.get('symbol') == symbol and r.get('status') == 'completed']
    last = runs[-1] if runs else None
    decision = next((r for r in reversed(last['reports']) if r.get('role') == 'director'), None) if last else None
    item = SYMBOLS[symbol]
    summary = {'symbol': symbol, 'name': item['name'], 'market': item['market'], 'currency': item['currency'],
            'leveraged_etf': item.get('leveraged_etf', False), 'leverage_factor': item.get('leverage_factor', 1),
            'underlying': item.get('underlying', ''), 'last': closes[-1], 'bid': quote['bid'], 'ask': quote['ask'],
            'spread_bps': round((quote['ask']-quote['bid'])/mid*10000, 2) if mid else None,
            'return_5m_pct': _pct(closes[-1], closes[-6]) if len(closes) > 5 else None,
            'return_20m_pct': _pct(closes[-1], closes[-21]) if len(closes) > 20 else None,
            'return_60m_pct': _pct(closes[-1], closes[-61]) if len(closes) > 60 else None,
            'volatility_1m_pct': round(math.sqrt(sum((x-mean)**2 for x in moves)/len(moves))*100, 4) if moves else None,
            'volume_ratio_5m': round(sum(volumes[-5:])/5/(sum(prior)/len(prior)), 3) if prior and sum(prior) > 0 else None,
            'session_minutes_left': int((quote['session_end']-now)//60),
            'position_quantity': position.get('quantity', 0),
            'position_unrealized_pct': _pct(quote['bid'], position['average']) if position.get('average') else None,
            'max_buy_quantity': limits['max_buy_quantity'], 'max_sell_quantity': limits['max_sell_quantity'],
            'last_analyzed_minutes_ago': int((now-last['time'])//60) if last else None,
            'last_decision': decision.get('stance') if decision else None}
    if extra:
        summary.update(extra)
    return summary


def clock(timestamp):
    return datetime.fromtimestamp(timestamp, ZoneInfo('Asia/Seoul')).strftime('%H:%M')


class DeskMixin:
    def daily_bars(self, symbol, now=None):
        """Completed daily bars of the last ~3 months, reused for a while. Raises ProviderError when unreadable."""
        now = time.time() if now is None else now
        cached = self.daily_cache.get(symbol)
        if cached and now-cached[0] < DAILY_TTL:
            return cached[1]
        bars = history.completed_bars(self.provider.candles(symbol, '1d', history.FETCH_BARS), now)
        self.daily_cache[symbol] = (now, bars)
        return bars

    @staticmethod
    def last_evaluation(state, symbol):
        return next((e for e in reversed(state.get('evaluations') or []) if e.get('symbol') == symbol), None)

    @staticmethod
    def focus_note(state, symbol):
        """Why today's list holds this name (the morning news read), if it does."""
        entry = (state.get('focus') or {}).get(SYMBOLS[symbol]['market']) or {}
        pick = next((p for p in entry.get('picks') or [] if p.get('symbol') == symbol), None)
        return {'source': pick.get('source'), 'score': pick.get('score'), 'ai': pick.get('ai')} if pick else None

    def interval_plan(self, state):
        """(usual seconds between analyses, faster seconds while busy or None) for this experiment. A month plan checks
        every ANALYSIS_INTERVAL_SECONDS (20 min). Day trading looks every DAYTRADE_INTERVAL_SECONDS (10 min) and every
        DAYTRADE_ACTIVE_INTERVAL_SECONDS (5 min) while a position is open or a proposal waits."""
        if state.get('strategy_mode') != 'intraday' or horizon_of(state.get('strategy_settings')) == 'month':
            return self.c.interval_seconds, None
        idle = self.c.daytrade_interval_seconds
        return idle, min(idle, self.c.daytrade_active_interval_seconds)

    def window_usage(self):
        """(provider, percent used, reset time) of the five-hour window of the AI the next cycle runs on; the last two are
        None when that AI reports no window (Codex) or the gate is off."""
        gate = self.agents.gate
        if not gate.enabled:
            return None, None, None
        usable = gate.plan()[0]
        if not usable:
            return None, None, None
        window = (gate.status(usable[0]).get('windows') or {}).get('five_hour')
        return (usable[0], window['pct'], window['resets_at']) if window else (usable[0], None, None)

    def paced_interval(self, state, until, now):
        """(seconds until the next analysis, dashboard info). The usual interval, stretched when the AI window cannot pay for
        one every interval until `until` (the session end or the window reset, whichever comes first)."""
        base = self.analysis_interval(state)
        cost = float((state.get('pacing') or {}).get('cost_pct', pacing.COST_DEFAULT))
        info = {'base': base, 'interval': base, 'paced': False, 'cost_pct': cost, 'pct': None, 'until': until}
        if not self.c.quota_pacing or until is None:
            return base, info
        provider, pct, resets = self.window_usage()
        interval = pacing.pace(base, pct=pct, switch_pct=self.c.ai_switch_pct, resets_at=resets, until=until, now=now,
                               cost=cost, cap=self.c.pace_max_seconds)
        info.update(interval=interval, paced=interval > base, pct=pct, resets_at=resets, provider=provider)
        return interval, info

    def analysis_interval(self, state):
        idle, busy = self.interval_plan(state)
        holding = bool(state.get('positions')) or any(p.get('status') == 'pending' for p in state.get('proposals', []))
        return busy if busy is not None and holding else idle

    def month_gate(self, snapshot, candidates, now, gen):
        """Read each candidate's daily bars and keep only those a rule flags (or the owner asked for). Records what was
        checked; when nothing qualifies the cycle waits a full interval and makes no AI call.
        -> (eligible candidates, verdict per symbol, daily bars per symbol)"""
        enforce = self.signal_gate
        requested = snapshot.get('requested_symbol')
        eligible, verdicts, bars, checks = [], {}, {}, []
        for cand in candidates:
            symbol, quote = cand[0], cand[2]
            name = SYMBOLS[symbol]['name']
            held = bool((snapshot['positions'].get(symbol) or {}).get('quantity'))
            forced = symbol == requested or not enforce
            problem = ''
            try:
                rows = self.daily_bars(symbol, now)
            except (ProviderError, KeyError, ValueError) as exc:
                rows, problem = None, str(exc)[:120] or type(exc).__name__
            signal = {}
            if rows is not None:
                bars[symbol] = rows
            if rows is not None and len(rows) >= history.MIN_BARS:
                signal = signals(rows)
                ignore = gate.dropped((snapshot.get('strategy_settings') or {}).get('signal_filter', 'all'),
                                      SYMBOLS[symbol]['market'], is_etf(symbol))
                verdict = gate.assess(held=held, signals=signal, last=self.last_evaluation(snapshot, symbol),
                                      price=mid(quote), now=now, forced=forced, ignore=ignore)
            else:
                verdict = {'eligible': forced, 'reason': 'requested' if symbol == requested else 'no_data', 'rules': []}
                problem = problem or f'완료된 일봉 {len(rows or [])}개 (필요 {history.MIN_BARS}개)'
            verdicts[symbol] = dict(verdict, signals=signal, held=held)
            checks.append({'symbol': symbol, 'name': name, 'held': held, 'eligible': verdict['eligible'],
                           'reason': verdict['reason'], 'rules': verdict['rules'], 'filtered': verdict.get('filtered', []),
                           'signals': {k: v for k, v in signal.items() if v}, **({'detail': problem} if problem else {})})
            if verdict['eligible']:
                eligible.append(cand)
        skipped = not eligible
        with self.store.edit() as s:
            if not s['running'] or s['generation'] != gen:
                return [], verdicts, bars
            previous = s.get('desk_gate') or {}
            s['desk_gate'] = {'time': now, 'skipped': skipped, 'checked': checks,
                              'skipped_cycles': previous.get('skipped_cycles', 0)+(1 if skipped else 0)}
            if skipped:
                until = now+self.analysis_interval(snapshot)
                s['next_run'] = until
                s['scheduler_status'] = f'규칙 신호가 없어 AI를 호출하지 않았습니다 · {gate.summary_line(checks)} · 다음 확인 {clock(until)}'
                if not previous.get('skipped'):
                    event(s, '규칙 신호가 없어 AI 호출 없이 대기합니다: '+gate.summary_line(checks))
            elif previous.get('skipped'):
                event(s, '규칙 신호가 나타나 AI 분석을 다시 시작합니다: '+gate.summary_line([c for c in checks if c['eligible']]))
        return eligible, verdicts, bars

    @staticmethod
    def day_extra(state, symbol):
        """Per-candidate daily volatility for the same-session selector, from today's focus data (no extra requests)."""
        entry = (state.get('focus') or {}).get(SYMBOLS[symbol]['market']) or {}
        metrics = (entry.get('metrics') or {}).get(symbol)
        if not metrics:
            return None
        return {'daily_volatility': {k: metrics.get(k) for k in ('range_pct', 'atr_pct', 'avg_move_pct', 'volume_ratio')}}

    def month_extra(self, state, symbol, bars, verdicts):
        """Per-candidate facts the stock selector compares (month horizon)."""
        digest = history.summary(bars.get(symbol) or [])
        verdict = verdicts.get(symbol) or {}
        keys = ('ret_1w_pct', 'ret_1m_pct', 'ret_3m_pct', 'from_high_pct', 'gap_sma20_pct', 'gap_sma60_pct',
                'atr_pct', 'volume_ratio_5d', 'max_drawdown_pct')
        return {'daily': {k: digest.get(k) for k in keys},
                'rule_signals': {k: v for k, v in (verdict.get('signals') or {}).items() if v},
                'rule_triggers': [RULE_NAMES.get(r, r) for r in verdict.get('rules') or []],
                'focus': self.focus_note(state, symbol),
                **({'evidence': evidence.brief(self.evidence.get(symbol), verdict.get('rules'))} if self.uses_evidence(state) else {})}

    @staticmethod
    def uses_evidence(state):
        return (state.get('strategy_settings') or {}).get('evidence') == 'on'

    def month_context(self, account, symbol, bars, verdict, now):
        """What the analysts get on top of the live quote: up to three months of daily bars and why they are being asked."""
        verdict = verdict or {}
        return {'daily_history': history.block(bars or []),
                'rule_signals': dict(verdict.get('signals') or {}),
                'rule_triggers': {'side': 'SELL' if verdict.get('held') else 'BUY', 'why': verdict.get('reason'),
                                  'rules': [RULE_NAMES.get(r, r) for r in verdict.get('rules') or []]},
                'own_history': history.own_records(account, symbol, now),
                'focus': self.focus_note(account, symbol)}

    @staticmethod
    def trail(pos, quote):
        """Month positions: follow the highest bid seen, and raise the stop once the price is half way to the target."""
        pos['high_water'] = max(pos.get('high_water') or 0, quote['bid'])
        raised = trailed_stop(pos, pos['high_water'])
        if raised > pos['stop_price']:
            pos['stop_price'], pos['trailing'] = raised, True

    def active_instruments(self, state):
        """What the desk quotes, analyses and may buy: the fixed lineup, or today's focus list per market (a market with
        no usable list falls back to the fixed lineup). Held names always stay in, so their exits keep being managed."""
        if state.get('strategy_mode') != 'intraday':
            return BASE_INSTRUMENTS
        settings = state['strategy_settings']
        if settings.get('universe_mode', 'daily_focus') != 'daily_focus':
            include = settings.get('include_leveraged_etfs', False)
            return [i for i in INSTRUMENTS if include or not i.get('leveraged_etf')]
        symbols = self.buyable_symbols(state)
        symbols += [s for s in state.get('positions', {}) if s not in symbols]
        return [SYMBOLS[s] for s in symbols if s in SYMBOLS]

    def update_desk_risk(self, state, now=None):
        if state.get('strategy_mode') != 'intraday':
            return
        now = time.time() if now is None else now
        limit = state['strategy_settings']['daily_loss_limit_pct']
        metrics = performance_summary(state, now=now, quote_age=self.c.quote_age)
        records = state.setdefault('risk_days', {})
        changes, halted = {}, []
        for currency, zone in [('KRW', 'Asia/Seoul'), ('USD', 'America/New_York')]:
            day = datetime.fromtimestamp(now, ZoneInfo(zone)).date().isoformat()
            p = metrics[currency]
            record = records.get(currency)
            if p['valuation_fresh']:
                if record is None or record['date'] != day:
                    # The previous observed equity includes losses across a date boundary.
                    base = record['last_equity'] if record else p['equity']
                    record = {'date': day, 'baseline': base, 'last_equity': p['equity'], 'halted': False}
                    records[currency] = record
                record['last_equity'] = p['equity']
                pnl = (p['equity']/record['baseline']-1)*100 if record['baseline'] > 0 else 0
                if pnl <= -limit and not record['halted']:
                    record['halted'] = True
                    event(state, f'{currency} 일일 손실 한도에 도달했습니다. 신규 매수를 중단하고 보유분 청산을 시도합니다.', 'warning')
            if record:
                changes[currency] = (record['last_equity']/record['baseline']-1)*100 if record['baseline'] > 0 else 0
                if record['halted']:
                    halted.append(currency)
            else:
                changes[currency] = None
        state['risk_status'] = {'halted': bool(halted), 'halted_currencies': halted,
                                'day_pnl_pct': changes,
                                'reason': ', '.join(halted)+' 일일 손실 한도 · 신규 매수 중단, 청산 감시 유지' if halted else '위험 한도 내 · 실행 중에만 청산 규칙을 감시합니다.'}

    def desk_buy_allowed(self, state, symbol):
        if not self.focus_allows(state, symbol):
            raise ProviderError('오늘의 집중 종목이 아니어서 신규 매수하지 않습니다. 보유 중이면 추가 매수 없이 청산 규칙으로 관리합니다.')
        currency = SYMBOLS[symbol]['currency']
        for held in state['positions']:
            if SYMBOLS[held]['currency'] == currency:
                self.validate_quote(state['quotes'].get(held, {}), held)
        self.update_desk_risk(state)
        if SYMBOLS[symbol]['currency'] in state.get('risk_status', {}).get('halted_currencies', []):
            raise ProviderError('해당 시장의 일일 손실 한도로 신규 매수가 중단되었습니다.')
        if SYMBOLS[symbol].get('leveraged_etf') and not state['strategy_settings'].get('include_leveraged_etfs'):
            raise ProviderError('이 실험은 레버리지 ETF를 허용하지 않습니다.')
        pos = state['positions'].get(symbol)
        if pos:
            q = state['quotes'][symbol]
            if (q['bid'] <= pos.get('stop_price', 0) or q['bid'] >= pos.get('take_profit_price', math.inf)
                    or time.time() >= pos.get('expires_at', 0)
                    or (pos.get('horizon') != 'month' and time.time() >= q['session_end']-120)):
                raise ProviderError('청산 조건에 도달한 보유 종목에는 추가 매수하지 않습니다.')

    def apply_desk_plan(self, state, symbol, proposal, sizing):
        trade = state['trades'][-1]
        trade['strategy_mode'] = 'intraday'
        trade['exit_reason'] = proposal.get('exit_reason', '')
        trade['sizing'] = copy.deepcopy(sizing)
        trade['origin'] = 'exit' if proposal.get('exit_reason') else proposal.get('origin', 'ai')
        if proposal.get('reused'):
            trade['reused'] = True
        origin_run = next((r for r in state.get('runs') or [] if r.get('id') == proposal.get('run_id')), None)
        if proposal['side'] == 'BUY' and origin_run and origin_run.get('evidence'):
            trade['evidence'] = origin_run['evidence'].get('sign')
        if proposal['side'] != 'BUY':
            if proposal.get('exit_reason') and symbol not in state['positions']:
                afterexit.record(state, symbol, proposal.get('position'), trade, proposal['exit_reason'])
            return
        pos = state['positions'][symbol]
        horizon = horizon_of(state.get('strategy_settings'))
        # Never extend the lifetime or loosen the stop on an existing position.
        pos.update(strategy_mode='intraday', horizon=horizon, entry_thesis=proposal['summary'],
                   stop_price=max(pos.get('stop_price', 0), sizing['stop_price']),
                   take_profit_price=min(pos.get('take_profit_price', math.inf), sizing['take_profit_price']),
                   expires_at=min(pos.get('expires_at', math.inf), sizing['expires_at']))
        if horizon == 'month':
            pos.setdefault('exit_mode', (state.get('strategy_settings') or {}).get('exit_mode', 'target'))
            stop_pct = proposal.get('stop_loss_pct')
            if isinstance(stop_pct, (int, float)) and stop_pct > 0:
                pos['trail_pct'] = min(pos.get('trail_pct') or stop_pct, stop_pct)
            pos.setdefault('initial_stop', pos['stop_price'])

    def validate_desk_proposal(self, state, proposal, quote):
        if proposal.get('exit_reason'):
            return proposal.get('sizing', {})
        if proposal['side'] == 'BUY':
            self.desk_buy_allowed(state, proposal['symbol'])
        sizing = size_order(state, proposal['symbol'], quote,
                            dict(proposal, stance=proposal['side']),
                            self.order_constraints(state, proposal['symbol'], quote), self.c)
        if proposal['quantity'] > sizing['quantity']:
            raise ProviderError('현재 위험 한도에서 제안 수량을 체결할 수 없습니다. 다시 분석하세요.')
        if sizing['quantity'] > proposal['quantity']:
            sizing['estimated_stop_risk'] = round(sizing['estimated_stop_risk']*proposal['quantity']/sizing['quantity'], 2)
            sizing['quantity'] = proposal['quantity']
        return sizing

    def shadow(self, s, proposal, quote, source, sizing=None):
        """Record the live order this proposal would have produced. It never touches a broker and can never
        interrupt paper trading: any failure is logged and swallowed."""
        if not self.c.live.shadow:
            return
        if float(proposal.get('quantity', 0)) != int(proposal.get('quantity', 0)):
            return                       # the live order structure is whole shares only; a fractional paper order has no counterpart
        try:
            record_shadow(s, proposal=proposal, quote=quote, instrument=SYMBOLS[proposal['symbol']],
                          config=self.c.live, source=source, now=time.time(), sizing=sizing)
        except Exception as exc:
            event(s, '실거래 그림자 기록에 실패했습니다(모의매매에는 영향 없음): '+type(exc).__name__, 'warning')

    def process_desk_exits(self):
        snapshot = self.store.read()
        if snapshot.get('strategy_mode') != 'intraday' or not snapshot['running'] or snapshot['liquidating']:
            return
        gen = snapshot['generation']
        for symbol in list(snapshot['positions']):
            seen = snapshot['quotes'].get(symbol) or {}
            if seen and not seen.get('tradable') and time.time()-seen.get('received', 0) < self.c.quote_age:
                continue                     # the poll just saw this market closed: nothing can be sold, so no Toss call
            try:
                q = self.quote_for_trade(symbol)
                with self.store.edit() as s:
                    if not s['running'] or s['liquidating'] or s['generation'] != gen:
                        return
                    pos = s['positions'].get(symbol)
                    if not pos or pos.get('strategy_mode') != 'intraday':
                        continue
                    s['quotes'][symbol] = q
                    self.update_desk_risk(s)
                    now = time.time()
                    month = pos.get('horizon') == 'month'
                    if month:
                        self.trail(pos, q)
                        if pos.get('exit_mode') == 'trail' and not pos.get('target_hit') and q['bid'] >= pos['take_profit_price']:
                            pos['target_hit'] = time.time()
                            event(s, f'{SYMBOLS[symbol]["name"]} 목표가 도달 · 팔지 않고 추적 손절(최고가 −{pos.get("trail_pct")}%)로 계속 보유합니다.')
                    # An unrelated quote failure can prevent refresh() from expiring
                    # proposals. A fresh exit quote must still replace an expired one.
                    for pending in s['proposals']:
                        if (pending['status'] == 'pending' and pending['symbol'] == symbol
                                and pending.get('exit_reason') and pending['expires'] < now):
                            pending['status'] = 'expired'
                    reason = ''
                    if SYMBOLS[symbol]['currency'] in s['risk_status']['halted_currencies']:
                        reason = '일일 손실 한도'
                    elif q['bid'] <= pos['stop_price']:
                        reason = '추적 손절(이익 보호)' if pos.get('trailing') else '손절 조건'
                    elif q['bid'] >= pos['take_profit_price'] and pos.get('exit_mode') != 'trail':
                        reason = '익절 조건'
                    elif now >= pos['expires_at']:
                        reason = '최대 보유시간'
                    elif not month and now >= q['session_end']-120:
                        reason = '장 마감 전 청산'
                    elif (pos.get('rotation') or {}).get('action') == 'sell' and now >= q['session_start']+ROTATION_WARMUP:
                        reason = '종목 교체 청산'
                    if not reason:
                        continue
                    qty = min(pos['quantity'], q['bid_size'], 10000)
                    if qty <= 0:
                        continue
                    if any(p['status'] == 'pending' and p['symbol'] == symbol and p.get('exit_reason') for p in s['proposals']):
                        continue
                    proposal = {'id': str(uuid.uuid4()), 'symbol': symbol, 'side': 'SELL', 'quantity': qty,
                                'reference_price': q['bid'], 'created': now, 'expires': now+self.c.proposal_seconds,
                                'generation': gen, 'revision': s['revision'], 'mode': self.c.mode,
                                'status': 'pending', 'strategy_mode': 'intraday', 'exit_reason': reason,
                                'summary': reason+'에 따른 모의매도', 'risks': ['지정한 손절 가격은 체결 가격을 보장하지 않습니다.'],
                                'sizing': {'quantity': qty, 'reason': reason},
                                'position': {k: pos.get(k) for k in ('average', 'stop_price', 'trail_pct', 'high_water',
                                                                     'expires_at')}}
                    for other in s['proposals']:
                        if other['status'] == 'pending' and other['symbol'] == symbol:
                            other['status'] = 'invalidated'
                    if s['execution_mode'] == 'auto':
                        self.fill(s, symbol, 'SELL', qty, q, proposal['id'],
                                  limit=pos['take_profit_price'] if reason == '익절 조건' else None)
                        self.apply_desk_plan(s, symbol, proposal, proposal['sizing'])
                        proposal.update(status='filled', execution_mode='auto')
                        for other in s['proposals']:
                            if other['status'] == 'pending':
                                other['status'] = 'invalidated'
                        event(s, f'{SYMBOLS[symbol]["name"]} {reason}: {qty}주 자동 모의매도')
                    else:
                        event(s, f'{SYMBOLS[symbol]["name"]} {reason}: 모의매도 승인을 기다립니다.')
                    s['proposals'].append(proposal)
                    self.shadow(s, proposal, q, 'exit')
            except (ValueError, ProviderError, KeyError):
                # Closed sessions and missing books never become synthetic fills.
                continue

    def trade_cost_bps(self, symbol, quote):
        """Cost of a round trip in bps: fees, slippage, the Korean sell tax and the quoted spread."""
        return round_trip_cost_pct(self.c, SYMBOLS[symbol]['currency'], quote, symbol)*100

    @staticmethod
    def usable_candles(candles, quote, now):
        """(this session's completed 1-minute candles, '') or (None, why not): at least 20, the newest under 3 minutes old and
        no gap over 3 minutes among the last 20."""
        rows = [x for x in candles if x.get('completed') and quote['session_start'] <= x['time'] < now]
        if len(rows) < 20 or now-rows[-1]['time'] > 180:
            return None, '당일 완료된 1분봉 20개와 최신 봉을 기다립니다. AI를 호출하지 않습니다.'
        if any(b['time']-a['time'] > 180 for a, b in zip(rows[-20:], rows[-19:])):
            return None, '분봉에 큰 공백이 있어 단기 분석을 보류합니다.'
        return rows, ''

    @staticmethod
    def trigger_side(verdict):
        """What the rule that triggered a month analysis said (the "규칙대로" baseline the AI is scored against), or None
        when the analysis was not started by a rule."""
        if not verdict or verdict.get('reason') != 'signal':
            return None
        return 'SELL' if verdict.get('held') else 'BUY'

    @staticmethod
    def previous_note(state, symbol, now, quote):
        """What the last finished analysis of this name concluded, for the next one to start from (see agents.PREVIOUS_PROMPT)."""
        runs = [r for r in state.get('runs', []) if r.get('symbol') == symbol and r.get('status') == 'completed']
        director = next((r for r in reversed(runs[-1]['reports']) if r.get('role') == 'director'), None) if runs else None
        if director is None:
            return None
        last = runs[-1]
        record = next((e for e in reversed(state.get('evaluations') or []) if e.get('run_id') == last['id']), None)
        then, current = (record or {}).get('price'), mid(quote)
        note = {'minutes_ago': int((now-last['time'])//60), 'stance': director.get('stance'),
                'summary': str(director.get('summary') or '')[:500], 'price_then': then,
                'price_change_pct': round((current/then-1)*100, 2) if then and current else None}
        watch = next((w for w in reversed(state.get('watches') or []) if w.get('symbol') == symbol), None)
        if watch:
            note['entry_plan'] = {'type': entry.LABELS[watch['type']], 'level': watch['level'], 'invalidate': watch['invalidate'],
                                  'result': '대기 중' if watch['status'] == 'waiting' else entry.FINISHED.get(watch['status'], watch['status'])}
        return note

    def place_desk_order(self, s, symbol, decision, fresh, *, gen, rev, run_id, reference_quote=None, run=None, source='ai',
                         reused=False):
        """One BUY or SELL decision becomes a paper order under the desk's risk rules: size it, dry-run the fill on a copy, then fill
        it (auto) or queue it for approval (manual). Raises RiskError, ProviderError or ValueError when a rule refuses, and nothing
        has been filled then. The analysis cycle and a triggered conditional entry both come through here."""
        side = decision['stance']
        s['quotes'][symbol] = fresh
        self.update_desk_risk(s)
        if side == 'BUY':
            self.desk_buy_allowed(s, symbol)
        latest = fresh['ask'] if side == 'BUY' else fresh['bid']
        if reference_quote is not None:
            reference = reference_quote['ask'] if side == 'BUY' else reference_quote['bid']
            if abs(latest/reference-1) > .005:
                raise RiskError('조사 중 가격이 0.5% 이상 움직여 다음 분석을 기다립니다.')
        sizing = size_order(s, symbol, fresh, decision, self.order_constraints(s, symbol, fresh), self.c)
        if run is not None:
            run['sizing'] = sizing
        if sizing['quantity'] <= 0:
            raise RiskError(sizing['reason'])
        proposal = {'id': str(uuid.uuid4()), 'symbol': symbol, 'side': side, 'quantity': sizing['quantity'],
                    'reference_price': latest, 'created': time.time(), 'expires': time.time()+self.c.proposal_seconds,
                    'generation': gen, 'revision': rev, 'mode': self.c.mode, 'status': 'pending', 'run_id': run_id,
                    'summary': decision['summary'], 'risks': decision['risks'], 'sizing': sizing, 'strategy_mode': 'intraday'}
        if source != 'ai':
            proposal['origin'] = source
        if reused:
            proposal['reused'] = True
        for key in ('target_weight_pct', 'stop_loss_pct', 'take_profit_pct', 'max_holding_minutes'):
            proposal[key] = decision[key]
        self.fill(copy.deepcopy(s), symbol, side, sizing['quantity'], fresh, 'validation')
        for p in s['proposals']:
            if p['status'] == 'pending' and p['symbol'] == symbol:
                p['status'] = 'invalidated'
        if s['execution_mode'] == 'auto':
            self.fill(s, symbol, side, sizing['quantity'], fresh, proposal['id'])
            self.apply_desk_plan(s, symbol, proposal, sizing)
            proposal.update(status='filled', execution_mode='auto')
            for p in s['proposals']:
                if p['status'] == 'pending':
                    p['status'] = 'invalidated'
        s['proposals'].append(proposal)
        self.shadow(s, proposal, fresh, source, sizing)
        return proposal, sizing

    # ---- conditional entries (entry.py): the director's "buy there, not now", watched without any AI call ----------------

    def plan_entry(self, s, symbol, decision, quote, run, gen, now):
        """After an analysis: the name's older waiting plan is replaced, and a HOLD that carries a well-formed conditional entry
        becomes a new watch. What the server declines is written on the run with the reason."""
        item = SYMBOLS[symbol]
        entry.close_waiting(s, 'replaced', '같은 종목의 새 분석으로 교체되었습니다.', now, symbol)
        if decision.get('entry_type') not in entry.TYPES or decision['stance'] != 'HOLD':
            return
        fields, note = None, ''
        if not self.c.conditional_entry:
            note = '조건 진입이 꺼져 있어(CONDITIONAL_ENTRY=off) 계획을 저장하지 않았습니다.'
        elif (s['positions'].get(symbol) or {}).get('quantity'):
            note = '이미 보유 중인 종목이라 조건 진입을 만들지 않았습니다.'
        elif len(entry.waiting(s)) >= entry.MAX_WAITING:
            note = f'대기 중인 조건 진입이 이미 {entry.MAX_WAITING}개라 새로 만들지 않았습니다.'
        else:
            month = horizon_of(s.get('strategy_settings')) == 'month'
            fields, note = entry.plan_from(decision, quote=quote, horizon='month' if month else 'intraday', now=now,
                                           currency=item['currency'], min_take_pct=min_take_pct(self.c, item['currency'], quote, symbol))
        if fields is None:
            run['watch'] = {'status': 'rejected', 'note': note}
            event(s, f'{item["name"]} · {note}')
            return
        watch = entry.make_watch(fields, symbol=symbol, name=item['name'], market=item['market'], currency=item['currency'],
                                 horizon=horizon_of(s.get('strategy_settings')), reference=mid(quote), summary=decision['summary'],
                                 engine=decision.get('engine'), run_id=run['id'], generation=gen, now=now)
        watch['reused'] = bool(run.get('reuse'))
        s.setdefault('watches', []).append(watch)
        entry.trim(s)
        run['watch'] = {'status': 'waiting', 'id': watch['id'], 'note': entry.describe(watch)}
        event(s, f'{item["name"]} · 조건 진입 등록: {entry.describe(watch)} · {int((watch["expires"]-now)//60)}분 동안 서버가 가격만 확인합니다.')

    def step_watch(self, s, watch, now):
        """Bookkeeping for one waiting watch on the quote the poll just stored; no I/O. True when it is ready to be tried."""
        symbol, name = watch['symbol'], watch['name']
        if now >= watch['expires']:
            entry.close(watch, 'expired', '유효 시간 안에 조건이 충족되지 않았습니다.', now)
            event(s, f'{name} 조건 진입 기한이 지나 폐기합니다. 조건이 충족되지 않았습니다.')
            return False
        if not self.focus_allows(s, symbol):
            # Checked before the quote: a name that left the list is no longer quoted, and would otherwise wait unseen.
            entry.close(watch, 'cancelled', '오늘의 집중 종목에서 빠져 취소했습니다.', now)
            event(s, f'{name} 조건 진입을 취소했습니다. 오늘의 집중 종목이 아닙니다.')
            return False
        quote = s['quotes'].get(symbol)
        try:
            self.validate_quote(quote or {}, symbol)
        except (ValueError, KeyError):
            return False                                   # closed market or no fresh book: nothing is judged now
        if quote['received'] <= watch['last_received']:
            return False                                   # nothing new since the last look
        watch['last_received'], watch['last_price'], watch['checked'] = quote['received'], mid(quote), now
        verdict = entry.evaluate(watch, quote)
        if verdict == 'invalid':
            entry.close(watch, 'invalid', f'가격이 무효 기준 {entry.fmt_price(watch["invalidate"], watch["currency"])} 아래로 내려가 폐기했습니다.', now)
            event(s, f'{name} 조건 진입을 폐기했습니다. 가격이 무효 기준 아래로 내려갔습니다.')
            return False
        watch['note'] = '돌파 구간을 이미 지나 쫓아 사지 않고 되돌림을 기다립니다.' if verdict == 'above' else ''
        watch['hits'] = watch['hits']+1 if verdict == 'hit' else 0
        return verdict == 'hit' and watch['hits'] >= entry.CONFIRM_POLLS and now >= watch['volume_after']

    def trigger_watch(self, watch_id, gen):
        """The price condition has held for several quotes in a row: confirm with a fresh quote (and, for a breakout, the volume),
        then order through the ordinary rules. Anything the rules refuse closes the watch; a bad moment only waits."""
        snapshot = self.store.read()
        watch = entry.find(snapshot, watch_id)
        if watch is None or watch['status'] != 'waiting' or snapshot['generation'] != gen:
            return
        symbol, now = watch['symbol'], time.time()
        fresh = self.quote_for_trade(symbol)
        problem = ''
        if entry.evaluate(watch, fresh) != 'hit':
            problem = 'reset'
        elif watch['type'] == 'breakout':
            candles, _ = self.usable_candles(self.provider.candles(symbol, interval='1m'), fresh, now)
            if candles is None:
                problem = '1분봉을 확인하지 못해 돌파 거래량을 확인하는 중입니다.'
            elif not entry.volume_ok(candles):
                problem = '돌파했지만 거래량이 받쳐주지 않아 기다립니다.'
        with self.store.edit() as s:
            if not s['running'] or s['liquidating'] or s['generation'] != gen:
                return
            live = entry.find(s, watch_id)
            if live is None or live['status'] != 'waiting':
                return
            if problem:
                if problem == 'reset':
                    live['hits'] = 0
                else:
                    live['note'], live['volume_after'] = problem, now+entry.VOLUME_RECHECK
                return
            name, now = live['name'], time.time()
            decision = {'stance': 'BUY', **live['plan'],
                        'summary': f'조건 진입({entry.describe(live)}): '+live['summary'],
                        'risks': ['가격 조건이 맞아 AI 호출 없이 체결했습니다. 조건을 정한 뒤 뉴스·시장 상황이 달라졌을 수 있습니다.']}
            try:
                proposal, sizing = self.place_desk_order(s, symbol, decision, fresh, gen=gen, rev=s['revision'],
                                                         run_id=live['run_id'], source='watch',
                                                         reused=bool(live.get('reused')))
            except (RiskError, ProviderError) as exc:
                entry.close(live, 'blocked', str(exc), now)
                event(s, f'{name} 조건 진입이 위험 규칙에 막혀 폐기되었습니다: '+str(exc), 'warning')
                return
            except ValueError as exc:
                live['failures'] += 1
                live['note'] = str(exc)
                if live['failures'] >= entry.MAX_FAILURES:
                    entry.close(live, 'blocked', str(exc), now)
                    event(s, f'{name} 조건 진입을 여러 번 시도했지만 체결하지 못해 폐기했습니다: '+str(exc), 'warning')
                return
            auto = proposal['status'] == 'filled'
            proposal['watch_id'] = live['id']
            if auto:
                s['trades'][-1]['entry_watch'] = live['id']
            live['proposal_id'] = proposal['id']
            entry.close(live, 'filled' if auto else 'proposed',
                        f'{sizing["quantity"]}주 '+('자동 모의매수' if auto else '매수 제안(승인 대기)'), now)
            scorecard = record_decision(
                s, run_id=live['run_id'], symbol=symbol, market=live['market'], decision={
                    'stance': 'BUY', 'engine': f'{live["engine"]} · 조건 진입', 'target_weight_pct': live['plan']['target_weight_pct']},
                quote=fresh, candidates={}, selected_by='watch', now=now, rules={}, horizon=live['horizon'],
                cost_bps=self.trade_cost_bps(symbol, fresh))
            if scorecard:
                scorecard['action'] = proposal['status']
            event(s, f'{name} 조건 진입: {entry.describe(live)} → 조건이 맞아 {sizing["quantity"]}주 '
                     +('자동 체결' if auto else '승인 대기'))

    def process_entry_watches(self):
        """Every poll: expire, drop or try the conditional entries. No AI call: the quotes the poll just stored are compared with
        the plan's prices, and a triggered plan passes the same sizing and risk rules as an analysed BUY."""
        snapshot = self.store.read()
        if snapshot.get('strategy_mode') != 'intraday' or not snapshot['running'] or snapshot['liquidating']:
            return
        if not entry.waiting(snapshot):
            return
        gen, due = snapshot['generation'], []
        with self.store.edit() as s:
            if not s['running'] or s['liquidating'] or s['generation'] != gen:
                return
            now = time.time()
            for watch in entry.waiting(s):
                if self.step_watch(s, watch, now):
                    due.append(watch['id'])
        for watch_id in due:
            try:
                self.trigger_watch(watch_id, gen)
            except (ValueError, ProviderError, KeyError):
                continue                                   # a quote or candle problem: look again on the next poll

    def desk_cycle(self, snapshot, run_id):
        gen, now = snapshot['generation'], time.time()
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        if self.c.mode != 'demo' and self.c.gemini_only and self.c.ai_daily_calls-snapshot['daily_ai'].get(day, 0) < DESK_CALLS:
            self.wait_for_cycle(gen, 'AI 호출 한도 대기 · 종목 선정·기획·전문가·검토·최종결정 7회 호출이 필요합니다.',
                                (int(now)//86400+1)*86400+1)
            return
        symbols = [i['symbol'] for i in self.active_instruments(snapshot)]
        candidates, message, skipped = [], '정규장·최신 호가·거래 가능한 수량을 기다립니다.', {}
        watching = {w['symbol'] for w in entry.waiting(snapshot)}
        for offset in range(len(symbols)):
            index = (snapshot['cursor']+offset) % len(symbols)
            symbol = symbols[index]
            if snapshot['initial'][SYMBOLS[symbol]['currency']] <= 0:
                continue
            if symbol in watching and symbol != snapshot.get('requested_symbol'):
                message = '조건 진입을 기다리는 종목은 그 조건이 끝날 때까지 다시 분석하지 않습니다.'
                skipped[symbol] = '조건 진입 대기 중'
                continue
            if not self.current(gen):
                return
            seen = snapshot['quotes'].get(symbol) or {}
            if seen and not seen.get('tradable') and now-seen.get('received', 0) < self.c.quote_age:
                continue                     # the quote poll just saw this market closed: no need to ask Toss again
            try:
                q = self.polled_quote(snapshot, symbol)
                limits = self.order_constraints(snapshot, symbol, q)
                if SYMBOLS[symbol]['currency'] in snapshot.get('risk_status', {}).get('halted_currencies', []):
                    skipped[symbol] = '일일 손실 한도'
                    continue
                if q['session_end']-now < 300 or not (limits['max_buy_quantity'] or limits['max_sell_quantity']):
                    skipped[symbol] = '장 마감 직전' if q['session_end']-now < 300 else '살 수 있는 수량 없음'
                    continue
                candles, problem = self.usable_candles(self.provider.candles(symbol, interval='1m'), q, now)
                if problem:
                    message = skipped[symbol] = problem
                    continue
                candidates.append((symbol, index, q, candles, limits))
            except (ValueError, ProviderError, KeyError) as exc:
                message = skipped[symbol] = str(exc)[:120] or type(exc).__name__
        # Why each name was left out of this round, and any candle data the provider had to repair: shown on the dashboard.
        notes = getattr(self.provider, 'candle_notes', {})
        scan = {'time': now, 'skipped': skipped,
                'repairs': {key: dict(n) for key, n in notes.items() if (n['repaired'] or n['dropped']) and now-n['time'] < 3600}}
        if not candidates:
            self.wait_for_cycle(gen, message, scan=scan)
            return
        with self.store.edit() as s:
            if s['running'] and s['generation'] == gen:
                s['desk_scan'] = scan
        month = horizon_of(snapshot.get('strategy_settings')) == 'month'
        verdicts, bars = {}, {}
        if month:
            candidates, verdicts, bars = self.month_gate(snapshot, candidates, now, gen)
            if not candidates:
                return                               # nothing to look at: no AI call, the gate set the next check
        if not self.begin_ai_cycle(gen):
            return
        selection = None
        requested = snapshot.get('requested_symbol')
        chosen = next((c for c in candidates if c[0] == requested), None)
        # Korean and US names are never compared with each other: pick a market first, then a stock in it.
        markets = sorted({SYMBOLS[c[0]]['market'] for c in candidates})
        market = SYMBOLS[chosen[0]]['market'] if chosen else (
            next((m for m in markets if m != snapshot.get('last_market')), markets[0]))
        market_pool = [c for c in candidates if SYMBOLS[c[0]]['market'] == market]
        label = MARKET_LABELS.get(market, market)
        if chosen is None and len(market_pool) > 1:
            with self.store.edit() as s:
                if not s['running'] or s['generation'] != gen:
                    return
                s['scheduler_status'] = f'메인 디렉터가 {label} 후보 {len(market_pool)}개 중 분석할 종목을 고릅니다.'
            currency = SYMBOLS[market_pool[0][0]]['currency']
            risk = snapshot.get('risk_status', {})
            ctx = {'strategy_mode': 'intraday', 'as_of_utc': datetime.now(timezone.utc).isoformat(), 'reports': [],
                   'market': market, 'market_name': label, 'currency': currency,
                   'strategy_settings': snapshot['strategy_settings'],
                   'portfolio': {'cash': snapshot['cash'][currency],
                                 'day_pnl_pct': (risk.get('day_pnl_pct') or {}).get(currency)},
                   'candidates': [dict(candidate_summary(c[0], c[2], c[3], snapshot, c[4], now,
                                                         self.month_extra(snapshot, c[0], bars, verdicts) if month
                                                         else self.day_extra(snapshot, c[0])),
                                       **self.intel.features(c[0], market)) for c in market_pool]}
            try:
                selection = self.agents.run('selector', ctx, gen)
                selection.update(role='selector', name=f'메인 디렉터 · 종목 선정 ({label})', time=time.time())
                selection['inputs'] = ctx['candidates']
                chosen = next(c for c in market_pool if c[0] == selection['symbol'])
            except ProviderError as exc:
                if str(exc) == STOPPED:
                    return
                with self.store.edit() as s:
                    event(s, f'{label} AI 종목 선정 실패로 순서대로 선택합니다: '+str(exc)[:300], 'warning')
        symbol, index, quote, candles, _ = chosen or market_pool[0]
        pace_seconds, pace_info = self.paced_interval(snapshot, quote['session_end'], time.time())
        usage_before = self.window_usage()[1]
        with self.store.edit() as s:
            if not s['running'] or s['generation'] != gen:
                return
            s['cursor'], s['next_run'] = index+1, time.time()+pace_seconds
            s['pacing'] = {**(s.get('pacing') or {}), **pace_info}
            s.pop('requested_symbol', None)
            s['last_market'] = SYMBOLS[symbol]['market']
            s['quotes'][symbol] = quote
            self.update_desk_risk(s)
            rev = s['revision']
            s['scheduler_status'] = SYMBOLS[symbol]['name']+' · 디렉터가 조사 업무를 배정합니다.'
            s['runs'].append({'id': run_id, 'symbol': symbol, 'time': time.time(), 'status': 'running', 'price': mid(quote),
                              'reports': [selection] if selection else [], 'execution_mode': s['execution_mode'],
                              'strategy_mode': 'intraday', 'horizon': 'month' if month else 'intraday',
                              'selected_by': 'ai' if selection else ('user' if symbol == requested else 'server')})
            account = copy.deepcopy(s)
        context = market_context(symbol, quote, candles, account)
        evidence_note = None
        context.update(strategy_mode='intraday', candle_interval='1m', intraday_candles=candles, strategy_settings=account['strategy_settings'],
                       instrument=SYMBOLS[symbol],
                       portfolio={'cash': account['cash'], 'positions': account['positions'], 'risk_status': account.get('risk_status', {})},
                       universe=[{'symbol': i['symbol'], 'name': i['name'], 'quote': account['quotes'].get(i['symbol'])} for i in self.active_instruments(account)])
        context['constraints'].update(self.order_constraints(account, symbol, quote))
        if month:
            context.update(horizon='month', **self.month_context(account, symbol, bars.get(symbol), verdicts.get(symbol), now))
            # A month plan needs the recent tape only to time the entry, so send 30 compact rows instead of the
            # full minute list (which the context used to carry twice).
            context['intraday'] = history.minute_block(candles)
            context.pop('candles', None)
            context.pop('intraday_candles', None)
            if self.uses_evidence(account):
                pack, fired = self.evidence.get(symbol), (verdicts.get(symbol) or {}).get('rules') or []
                context['evidence'] = evidence.for_ai(pack, fired)
                evidence_note = evidence.summary(pack, fired)
        context['market_intel'] = self.intel.features(symbol, SYMBOLS[symbol]['market'])
        previous = self.previous_note(account, symbol, time.time(), quote)
        if previous:
            context['previous'] = previous
        # The owner's own "지금 분석" always investigates afresh; otherwise a recent, still-valid research is reused (reuse.py).
        try:
            found, reuse_note = (None, '') if symbol == requested else reuse.find(
                account, symbol, price=mid(quote), spike=entry.volume_ratio(candles), now=time.time(), ttl=self.c.research_reuse_seconds,
                move_pct=self.c.research_reuse_move_pct, horizon='month' if month else 'intraday')
        except (KeyError, TypeError, ValueError, AttributeError):
            found, reuse_note = None, ''                  # reuse only saves calls: anything odd in old data means a full analysis
        role_names = dict(DESK_ROLES)

        def research(role, specific_context):
            if not self.current(gen):
                raise ProviderError('중지된 분석입니다.')
            result = self.agents.run(role, specific_context, gen)
            result.update(role=role, name=role_names[role], time=time.time())
            return result

        def save_reports(reports, active, roles=None, note='', fields=None, status=''):
            with self.store.edit() as s:
                if not s['running'] or s['generation'] != gen:
                    raise ProviderError('중지된 분석입니다.')
                run = next(x for x in s['runs'] if x['id'] == run_id)
                run['reports'].extend(reports)
                run['active_role'] = active
                run['active_roles'] = roles if roles is not None else (
                    ['fundamental', 'technical', 'news'] if active == 'research' else ([active] if active else []))
                run.update(fields or {})
                if status:
                    s['scheduler_status'] = status
                if note:
                    event(s, note)

        name = SYMBOLS[symbol]['name']
        if found is None:
            save_reports([], 'planner', note=f'{name} · {reuse_note}' if reuse_note else '')
            planner = research('planner', context)
            context['reports'].append(planner)
            save_reports([planner], 'research')
            assignments = {t['role']: t for t in planner.get('tasks', [])}
            if set(assignments) != {'fundamental', 'technical', 'news'}:
                raise ProviderError('조사 업무 배정이 불완전하여 분석을 중단했습니다.')
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = [pool.submit(research, role, dict(copy.deepcopy(context), assignment=assignments[role]))
                           for role in ('fundamental', 'technical', 'news')]
                reports = [f.result() for f in futures]
            fresh_reports = reports
        else:
            # Planner, company and news research are reused (their original time and price stay on them); only the tape is read again.
            planner, fundamental, news = (found['reports'][role] for role in reuse.REUSED)
            assignments = {t['role']: t for t in planner['tasks']}
            context['reports'].append(planner)
            age = found['age_minutes']
            save_reports([planner, fundamental, news], 'research', roles=[reuse.TECHNICAL],
                         fields={'reuse': {'from_run': found['run_id'], 'age_minutes': age, 'saved_calls': reuse.SAVED_CALLS}},
                         status=f'{name} · 조사 재사용({age}분 전) · 시세 분석가가 분석합니다.',
                         note=f'{name} · 조사 재사용: 기업·뉴스 조사({age}분 전)를 다시 쓰고 시세 분석·반대 검토·최종 판단만 새로 합니다 '
                              f'(AI 호출 {reuse.SAVED_CALLS}회 절약).')
            technical = research(reuse.TECHNICAL, dict(copy.deepcopy(context), assignment=assignments[reuse.TECHNICAL]))
            reports = [fundamental, technical, news]
            fresh_reports = [technical]
        context['reports'].extend(reports)
        save_reports(fresh_reports, 'critic')
        critic = research('critic', context)
        context['reports'].append(critic)
        save_reports([critic], 'director')
        decision = research('director', context)
        context['reports'].append(decision)
        save_reports([decision], None)
        usage_after = self.window_usage()[1]
        fresh = self.quote_for_trade(symbol)
        with self.store.edit() as s:
            if not s['running'] or s['generation'] != gen:
                return
            run = next(x for x in s['runs'] if x['id'] == run_id)
            run['status'] = 'completed'
            if evidence_note:
                run['evidence'] = evidence_note
            tracked = s.setdefault('pacing', {})
            tracked['samples'], tracked['cost_pct'] = pacing.learn(tracked.get('samples'), usage_before, usage_after)
            s['scheduler_status'] = '조사·반대 검토·최종 판단 완료 · 청산 규칙은 별도로 감시합니다.'
            scorecard = record_decision(
                s, run_id=run_id, symbol=symbol, market=SYMBOLS[symbol]['market'], decision=decision, quote=fresh,
                candidates={c[0]: mid(c[2]) for c in market_pool if c[0] != symbol} if selection else {},
                selected_by=run.get('selected_by', 'server'), now=time.time(),
                rules=(verdicts.get(symbol) or {}).get('signals') if month else signals(candles),
                horizon='month' if month else 'intraday', reused=found is not None,
                trigger_side=self.trigger_side(verdicts.get(symbol)) if month else None, evidence=evidence_note,
                cost_bps=self.trade_cost_bps(symbol, fresh))
            self.plan_entry(s, symbol, decision, fresh, run, gen, time.time())
            if decision['stance'] == 'HOLD' or s['revision'] != rev:
                event(s, SYMBOLS[symbol]['name']+' · 관망 또는 계좌 변경으로 체결하지 않습니다.')
                return
            try:
                proposal, sizing = self.place_desk_order(s, symbol, decision, fresh, gen=gen, rev=rev, run_id=run_id,
                                                         reference_quote=quote, run=run, reused=found is not None)
                if scorecard:
                    scorecard['action'] = proposal['status']
                event(s, f'{SYMBOLS[symbol]["name"]} 목표 비중·손절 위험으로 {sizing["quantity"]}주 산정 · '+('자동 체결' if s['execution_mode'] == 'auto' else '승인 대기'))
            except (RiskError, ProviderError, ValueError) as exc:
                run['blocked'] = str(exc)
                if scorecard:
                    scorecard['action'] = 'blocked'
                event(s, '위험 검토에서 체결을 보류했습니다: '+str(exc), 'warning')
