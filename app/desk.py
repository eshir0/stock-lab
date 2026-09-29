"""Bounded research workflow and independent, paper-only intraday exit monitoring."""
import copy
import math
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .agents import DESK_ROLES, market_context
from .config import INSTRUMENTS as BASE_INSTRUMENTS
from .instruments import INSTRUMENTS, SYMBOLS
from .performance import performance_summary
from .providers import ProviderError
from .risk import RiskError, size_order
from .store import event


class DeskMixin:
    def active_instruments(self, state):
        if state.get('strategy_mode') != 'intraday':
            return BASE_INSTRUMENTS
        include = state['strategy_settings'].get('include_leveraged_etfs', False)
        return [i for i in INSTRUMENTS if include or not i.get('leveraged_etf')]

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
                    or time.time() >= pos.get('expires_at', 0) or time.time() >= q['session_end']-120):
                raise ProviderError('청산 조건에 도달한 보유 종목에는 추가 매수하지 않습니다.')

    def apply_desk_plan(self, state, symbol, proposal, sizing):
        trade = state['trades'][-1]
        trade['strategy_mode'] = 'intraday'
        trade['exit_reason'] = proposal.get('exit_reason', '')
        trade['sizing'] = copy.deepcopy(sizing)
        if proposal['side'] != 'BUY':
            return
        pos = state['positions'][symbol]
        # Never extend the lifetime or loosen the stop on an existing position.
        pos.update(strategy_mode='intraday', entry_thesis=proposal['summary'],
                   stop_price=max(pos.get('stop_price', 0), sizing['stop_price']),
                   take_profit_price=min(pos.get('take_profit_price', math.inf), sizing['take_profit_price']),
                   expires_at=min(pos.get('expires_at', math.inf), sizing['expires_at']))

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

    def process_desk_exits(self):
        snapshot = self.store.read()
        if snapshot.get('strategy_mode') != 'intraday' or not snapshot['running'] or snapshot['liquidating']:
            return
        gen = snapshot['generation']
        for symbol in list(snapshot['positions']):
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
                        reason = '손절 조건'
                    elif q['bid'] >= pos['take_profit_price']:
                        reason = '익절 조건'
                    elif now >= pos['expires_at']:
                        reason = '최대 보유시간'
                    elif now >= q['session_end']-120:
                        reason = '장 마감 전 청산'
                    if not reason:
                        continue
                    qty = min(pos['quantity'], int(q['bid_size']), 10000)
                    if qty <= 0:
                        continue
                    if any(p['status'] == 'pending' and p['symbol'] == symbol and p.get('exit_reason') for p in s['proposals']):
                        continue
                    proposal = {'id': str(uuid.uuid4()), 'symbol': symbol, 'side': 'SELL', 'quantity': qty,
                                'reference_price': q['bid'], 'created': now, 'expires': now+self.c.proposal_seconds,
                                'generation': gen, 'revision': s['revision'], 'mode': self.c.mode,
                                'status': 'pending', 'strategy_mode': 'intraday', 'exit_reason': reason,
                                'summary': reason+'에 따른 모의매도', 'risks': ['지정한 손절 가격은 체결 가격을 보장하지 않습니다.'],
                                'sizing': {'quantity': qty, 'reason': reason}}
                    for other in s['proposals']:
                        if other['status'] == 'pending' and other['symbol'] == symbol:
                            other['status'] = 'invalidated'
                    if s['execution_mode'] == 'auto':
                        self.fill(s, symbol, 'SELL', qty, q, proposal['id'])
                        self.apply_desk_plan(s, symbol, proposal, proposal['sizing'])
                        proposal.update(status='filled', execution_mode='auto')
                        for other in s['proposals']:
                            if other['status'] == 'pending':
                                other['status'] = 'invalidated'
                        event(s, f'{SYMBOLS[symbol]["name"]} {reason}: {qty}주 자동 모의매도')
                    else:
                        event(s, f'{SYMBOLS[symbol]["name"]} {reason}: 모의매도 승인을 기다립니다.')
                    s['proposals'].append(proposal)
            except (ValueError, ProviderError, KeyError):
                # Closed sessions and missing books never become synthetic fills.
                continue

    def desk_cycle(self, snapshot, run_id):
        gen, now = snapshot['generation'], time.time()
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        if self.c.mode != 'demo' and self.c.gemini_only and self.c.ai_daily_calls-snapshot['daily_ai'].get(day, 0) < len(DESK_ROLES):
            self.wait_for_cycle(gen, 'AI 호출 한도 대기 · 기획·전문가·검토·최종결정 6회 호출이 필요합니다.',
                                (int(now)//86400+1)*86400+1)
            return
        symbols = [i['symbol'] for i in self.active_instruments(snapshot)]
        selected, message = None, '정규장·최신 호가·거래 가능한 수량을 기다립니다.'
        for offset in range(len(symbols)):
            index = (snapshot['cursor']+offset) % len(symbols)
            symbol = symbols[index]
            if snapshot['initial'][SYMBOLS[symbol]['currency']] <= 0:
                continue
            if not self.current(gen):
                return
            try:
                q = self.quote_for_trade(symbol)
                limits = self.order_constraints(snapshot, symbol, q)
                if SYMBOLS[symbol]['currency'] in snapshot.get('risk_status', {}).get('halted_currencies', []):
                    continue
                if q['session_end']-now < 300 or not (limits['max_buy_quantity'] or limits['max_sell_quantity']):
                    continue
                candles = self.provider.candles(symbol, interval='1m')
                candles = [x for x in candles if x.get('completed') and q['session_start'] <= x['time'] < now]
                if len(candles) < 20 or now-candles[-1]['time'] > 180:
                    message = '당일 완료된 1분봉 20개와 최신 봉을 기다립니다. AI를 호출하지 않습니다.'
                    continue
                if any(b['time']-a['time'] > 180 for a, b in zip(candles[-20:], candles[-19:])):
                    message = '분봉에 큰 공백이 있어 단기 분석을 보류합니다.'
                    continue
                selected = symbol, index, q, candles
                break
            except (ValueError, ProviderError, KeyError) as exc:
                message = str(exc)
        if selected is None:
            self.wait_for_cycle(gen, message)
            return
        symbol, index, quote, candles = selected
        with self.store.edit() as s:
            if not s['running'] or s['generation'] != gen:
                return
            s['cursor'], s['next_run'] = index+1, time.time()+self.c.interval_seconds
            s['quotes'][symbol] = quote
            self.update_desk_risk(s)
            rev = s['revision']
            s['scheduler_status'] = SYMBOLS[symbol]['name']+' · 디렉터가 조사 업무를 배정합니다.'
            s['runs'].append({'id': run_id, 'symbol': symbol, 'time': time.time(), 'status': 'running',
                              'reports': [], 'execution_mode': s['execution_mode'], 'strategy_mode': 'intraday'})
            account = copy.deepcopy(s)
        context = market_context(symbol, quote, candles, account)
        context.update(strategy_mode='intraday', candle_interval='1m', intraday_candles=candles, strategy_settings=account['strategy_settings'],
                       instrument=SYMBOLS[symbol],
                       portfolio={'cash': account['cash'], 'positions': account['positions'], 'risk_status': account.get('risk_status', {})},
                       universe=[{'symbol': i['symbol'], 'name': i['name'], 'quote': account['quotes'].get(i['symbol'])} for i in self.active_instruments(account)])
        context['constraints'].update(self.order_constraints(account, symbol, quote))
        role_names = dict(DESK_ROLES)

        def research(role, specific_context):
            if not self.current(gen):
                raise ProviderError('중지된 분석입니다.')
            result = self.agents.run(role, specific_context, gen)
            result.update(role=role, name=role_names[role], time=time.time())
            return result

        def save_reports(reports, active):
            with self.store.edit() as s:
                if not s['running'] or s['generation'] != gen:
                    raise ProviderError('중지된 분석입니다.')
                run = next(x for x in s['runs'] if x['id'] == run_id)
                run['reports'].extend(reports)
                run['active_role'] = active
                run['active_roles'] = ['fundamental', 'technical', 'news'] if active == 'research' else ([active] if active else [])

        save_reports([], 'planner')
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
        context['reports'].extend(reports)
        save_reports(reports, 'critic')
        critic = research('critic', context)
        context['reports'].append(critic)
        save_reports([critic], 'director')
        decision = research('director', context)
        context['reports'].append(decision)
        save_reports([decision], None)
        fresh = self.quote_for_trade(symbol)
        with self.store.edit() as s:
            if not s['running'] or s['generation'] != gen:
                return
            run = next(x for x in s['runs'] if x['id'] == run_id)
            run['status'] = 'completed'
            s['scheduler_status'] = '조사·반대 검토·최종 판단 완료 · 청산 규칙은 별도로 감시합니다.'
            if decision['stance'] == 'HOLD' or s['revision'] != rev:
                event(s, SYMBOLS[symbol]['name']+' · 관망 또는 계좌 변경으로 체결하지 않습니다.')
                return
            side = decision['stance']
            try:
                s['quotes'][symbol] = fresh
                self.update_desk_risk(s)
                if side == 'BUY':
                    self.desk_buy_allowed(s, symbol)
                reference = quote['ask'] if side == 'BUY' else quote['bid']
                latest = fresh['ask'] if side == 'BUY' else fresh['bid']
                if abs(latest/reference-1) > .005:
                    raise RiskError('조사 중 가격이 0.5% 이상 움직여 다음 분석을 기다립니다.')
                sizing = size_order(s, symbol, fresh, decision, self.order_constraints(s, symbol, fresh), self.c)
                run['sizing'] = sizing
                if sizing['quantity'] <= 0:
                    raise RiskError(sizing['reason'])
                proposal = {'id': str(uuid.uuid4()), 'symbol': symbol, 'side': side, 'quantity': sizing['quantity'],
                            'reference_price': latest, 'created': time.time(), 'expires': time.time()+self.c.proposal_seconds,
                            'generation': gen, 'revision': rev, 'mode': self.c.mode, 'status': 'pending', 'run_id': run_id,
                            'summary': decision['summary'], 'risks': decision['risks'], 'sizing': sizing, 'strategy_mode': 'intraday'}
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
                event(s, f'{SYMBOLS[symbol]["name"]} 목표 비중·손절 위험으로 {sizing["quantity"]}주 산정 · '+('자동 체결' if s['execution_mode'] == 'auto' else '승인 대기'))
            except (RiskError, ProviderError, ValueError) as exc:
                run['blocked'] = str(exc)
                event(s, '위험 검토에서 체결을 보류했습니다: '+str(exc), 'warning')
