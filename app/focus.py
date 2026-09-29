"""Engine side of the daily focus list: when it is built, how it is published, and how it limits what may be bought.

Safety properties (all covered by tests/test_focus_engine.py):
- The list is only ever a subset of the closed catalogue in `instruments.py`; nothing external can add a symbol.
- A failure at any step keeps the fixed lineup, so trading is never blocked by the list and never runs on a stale one.
- New buys are limited to today's list. Names already held are never added to; they are kept or sold (see
  `universe.rotation_decision`) and the normal stop, target, holding-time and closing rules keep protecting them.
- Building the list only reads market data and asks the AI for a briefing. It cannot place or size an order.
"""
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import universe
from .agents import STOPPED
from .desk import DESK_CALLS
from .instruments import CATALOGUE, INSTRUMENTS, SYMBOLS
from .providers import ProviderError, RateLimited
from .store import event

MARKETS = ('KR', 'US')
ZONES = {'KR': 'Asia/Seoul', 'US': 'America/New_York'}
LABELS = {'KR': '국내', 'US': '미국'}
HISTORY_LIMIT = 40           # about twenty sessions of both markets
RETRY_SECONDS = 600          # wait between failed builds
AI_RETRY_SECONDS = 600
MAX_BUILD_FAILURES = 3       # after this many failures the session falls back to the fixed lineup
MIN_MEASURED = 5             # fewer measurable names than this means the data feed is unusable
METRIC_KEYS = ('last', 'sma20', 'ret_1m_pct', 'ret_5d_pct', 'ext_20d_pct', 'atr_pct')


def universe_mode(state):
    return (state.get('strategy_settings') or {}).get('universe_mode', 'daily_focus')


class FocusData(Exception):
    """The market data needed to build today's list is not usable right now."""


class FocusMixin:
    focus_pause = .25          # seconds between daily-candle reads: gentle on the chart rate group
    focus_retry_pause = 3.0    # seconds to wait after a 429 from the chart group before asking again

    # ---- reading the published list (called on every cycle, so no I/O here) ---------------------------------------

    def focus_entry(self, state, market, now=None):
        """The usable list for a market, or None (never built, gave up, or too old) meaning "use the fixed lineup"."""
        entry = (state.get('focus') or {}).get(market)
        if not entry or entry.get('status') not in ('ok', 'quant_only'):
            return None
        now = time.time() if now is None else now
        return entry if now-entry.get('built_at', 0) <= universe.MAX_AGE else None

    def focus_symbols(self, state, market, now=None):
        """Today's picks for a market ([] is a valid "nothing qualifies today"), or None to use the fixed lineup."""
        entry = self.focus_entry(state, market, now)
        return [p['symbol'] for p in entry['picks']] if entry else None

    @staticmethod
    def fixed_symbols(state, market):
        include = (state.get('strategy_settings') or {}).get('include_leveraged_etfs', False)
        return [i['symbol'] for i in INSTRUMENTS if i['market'] == market and (include or not i.get('leveraged_etf'))]

    def buyable_symbols(self, state, now=None):
        """Symbols the desk may open or add to right now."""
        symbols = []
        for market in MARKETS:
            picks = self.focus_symbols(state, market, now)
            symbols += self.fixed_symbols(state, market) if picks is None else picks
        return symbols

    def focus_allows(self, state, symbol):
        if state.get('strategy_mode') != 'intraday' or universe_mode(state) != 'daily_focus':
            return True
        return symbol in self.buyable_symbols(state)

    # ---- when to build --------------------------------------------------------------------------------------------

    @staticmethod
    def market_window(state, market, now):
        """(session date, open, close) of the market's regular session, taken from the newest quote we hold."""
        quotes = [q for symbol, q in (state.get('quotes') or {}).items()
                  if SYMBOLS.get(symbol, {}).get('market') == market and q.get('session_start') and q.get('session_end')
                  and now-q.get('received', 0) < 6*3600]
        if not quotes:
            return None
        newest = max(quotes, key=lambda q: q.get('received', 0))
        start, end = newest['session_start'], newest['session_end']
        return datetime.fromtimestamp(start, ZoneInfo(ZONES[market])).date().isoformat(), start, end

    def focus_ai_wanted(self, state):
        """Only while the analysis is running, and never at the cost of the desk's own daily AI budget."""
        if not self.c.focus_ai or not state.get('running'):
            return False
        if self.c.mode == 'demo':
            return True
        if not self.c.ai_configured:
            return False
        day = datetime.now(timezone.utc).date().isoformat()
        return not (self.c.gemini_only and self.c.ai_daily_calls-state['daily_ai'].get(day, 0) < DESK_CALLS+1)

    def focus_ai_upgrade_due(self, state, entry, now):
        ai = entry.get('ai') or {}
        return (entry.get('status') == 'quant_only' and ai.get('status') in ('none', 'failed', 'skipped')
                and ai.get('attempts', 0) < 2 and now-ai.get('last_attempt', 0) >= AI_RETRY_SECONDS
                and self.focus_ai_wanted(state))

    def focus_retry_due(self, market, session_date, now):
        stamp, failures, date = self.focus_attempts.get(market, (0, 0, session_date))
        return date != session_date or failures == 0 or now-stamp >= RETRY_SECONDS

    def refresh_focus(self):
        """One pass of the focus loop. Cheap when nothing is due; never blocks quotes, exits or trading."""
        state = self.store.read()
        if state.get('strategy_mode') != 'intraday' or universe_mode(state) != 'daily_focus':
            return
        now = time.time()
        for market in MARKETS:
            window = self.market_window(state, market, now)
            if window is None:
                continue
            session_date, start, end = window
            if not start-universe.LEAD_SECONDS <= now < end:
                continue
            entry = (state.get('focus') or {}).get(market)
            same_session = bool(entry) and entry.get('session_date') == session_date
            if same_session and (entry.get('status') == 'fallback' or not self.focus_ai_upgrade_due(state, entry, now)):
                continue
            if not same_session and not self.focus_retry_due(market, session_date, now):
                continue
            try:
                self.build_focus(state, market, session_date, start, end, entry if same_session else None, now)
                self.focus_attempts[market] = (now, 0, session_date)
            except Exception as exc:
                self.focus_failed(state, market, session_date, start, end, now, exc)

    def focus_failed(self, state, market, session_date, start, end, now, exc):
        _, failures, date = self.focus_attempts.get(market, (0, 0, session_date))
        failures = (failures if date == session_date else 0)+1
        self.focus_attempts[market] = (now, failures, session_date)
        reason = str(exc)[:200] if isinstance(exc, (FocusData, ProviderError)) else type(exc).__name__
        give_up = failures >= MAX_BUILD_FAILURES
        with self.store.edit() as s:
            if s['experiment_id'] != state['experiment_id']:
                return
            if give_up:
                s['focus'][market] = {'market': market, 'session_date': session_date, 'built_at': now, 'session_start': start,
                                      'session_end': end, 'status': 'fallback', 'source': 'fixed', 'picks': [], 'excluded': [],
                                      'notes': [f'집중 종목을 계산하지 못해 오늘은 기본 종목을 씁니다: {reason}'],
                                      'ai': {'status': 'none', 'attempts': 0, 'last_attempt': 0}}
            event(s, f'{LABELS[market]} 오늘의 집중 종목 계산에 실패했습니다({failures}/{MAX_BUILD_FAILURES}): {reason}'
                  + (' 오늘은 기본 종목으로 진행합니다.' if give_up else ' 잠시 뒤 다시 시도합니다.'), 'warning')

    # ---- building -------------------------------------------------------------------------------------------------

    def fetch_pool(self, items, now):
        """Daily candles for every name; a name that cannot be read is simply missing today, never guessed."""
        series, metrics = {}, {}
        for item in items:
            symbol, rows, limited = item['symbol'], [], False
            for _ in range(3):
                try:
                    rows, limited = self.provider.candles(symbol, '1d'), False
                    break
                except RateLimited:
                    limited = True                  # a short 429: wait a moment and ask again
                    time.sleep(self.focus_retry_pause)
                except (ProviderError, KeyError, ValueError):
                    rows = []
                    break
            time.sleep(self.focus_pause)
            if limited:
                break                               # still busy after three tries: the remaining names are missing today
            series[symbol] = rows
            metrics[symbol] = universe.daily_metrics(rows, now)
        return series, metrics

    def run_trend_brief(self, state, market, session_date, passed, meta, now):
        """Ask the AI for a news/theme read of the names that passed the data screen. Failure only removes the read."""
        context = {'strategy_mode': 'intraday', 'as_of_utc': datetime.now(timezone.utc).isoformat(), 'date': session_date,
                   'market': market, 'market_name': LABELS[market], 'max_picks': self.c.focus_per_market,
                   'candidates': universe.ai_candidates(passed),
                   'held': sorted(s for s in state.get('positions', {}) if SYMBOLS.get(s, {}).get('market') == market),
                   'guards': {'ext_20d_cap_pct': {k: v['ext'] for k, v in universe.CAPS.items()},
                              'run_5d_cap_pct': {k: v['run5'] for k, v in universe.CAPS.items()}}}
        meta = dict(meta, attempts=meta.get('attempts', 0)+1, last_attempt=now)
        try:
            report = self.agents.run('trend', context, state['generation'])
        except ProviderError as exc:
            if str(exc) == STOPPED:
                return None, dict(meta, attempts=meta.get('attempts', 1)-1, status=meta.get('status', 'none'))
            return None, dict(meta, status='failed', error=str(exc)[:300])
        return report, dict(meta, status='ok', error='', engine=str(report.get('engine', ''))[:80],
                            grounded=bool(report.get('grounded')))

    def build_focus(self, state, market, session_date, start, end, existing, now):
        """Score the market's pool, add the AI's read if it is available, and publish the list."""
        include = state['strategy_settings'].get('include_leveraged_etfs', False)
        held = set(state.get('positions') or {})
        rank_items = [i for i in CATALOGUE if i['market'] == market and (include or not i.get('leveraged_etf'))]
        extra = [i for i in CATALOGUE if i['market'] == market and i['symbol'] in held and i not in rank_items]
        series = None
        if existing is None:
            series, metrics = self.fetch_pool(rank_items+extra, now)
            measured = sum(1 for m in metrics.values() if m)
            if measured < MIN_MEASURED:
                raise FocusData(f'일봉을 읽을 수 있는 종목이 {measured}개뿐입니다.')
            passed, excluded = universe.rank_pool(rank_items, metrics, self.intel.attention(market), self.c.mode != 'toss')
            meta = {'status': 'none', 'attempts': 0, 'last_attempt': 0}
        else:
            passed, excluded = existing['ranked'], existing['excluded']
            metrics = existing['metrics']
            measured = existing.get('measured', 0)
            meta = dict(existing.get('ai') or {'status': 'none', 'attempts': 0, 'last_attempt': 0})
        report = None
        if passed and self.focus_ai_wanted(state):
            report, meta = self.run_trend_brief(state, market, session_date, passed, meta, now)
        elif meta.get('status') == 'none' and not self.c.focus_ai:
            meta['status'] = 'skipped'
        picks, notes = universe.choose(passed, report, self.c.focus_per_market)
        grounded = bool(report and report.get('grounded'))
        avoid = {a['symbol'] for a in (report or {}).get('avoid', [])} if grounded else set()
        entry = {'market': market, 'session_date': session_date, 'built_at': now, 'session_start': start, 'session_end': end,
                 'status': 'ok' if grounded else 'quant_only', 'source': 'ai+data' if grounded else 'data',
                 'per_market': self.c.focus_per_market, 'candidates': len(rank_items), 'measured': measured,
                 'picks': picks, 'ranked': passed, 'excluded': excluded, 'notes': notes,
                 'metrics': {symbol: {k: m[k] for k in METRIC_KEYS} for symbol, m in metrics.items() if m}
                 if existing is None else metrics,
                 'ai': {**meta, **({'market_view': report['market_view'], 'themes': report['themes'], 'avoid': report['avoid'],
                                    'risks': report['risks'][:6], 'evidence': [
                                        {**e, 'claim': e['claim'][:300]} for e in report['evidence'][:6]]}
                                   if report else {k: v for k, v in (existing or {}).get('ai', {}).items()
                                                   if k in ('market_view', 'themes', 'avoid', 'risks', 'evidence')})}}
        self.publish_focus(state, market, entry, avoid, series, rank_items, existing is not None, now)

    # ---- publishing ------------------------------------------------------------------------------------------------

    def publish_focus(self, state, market, entry, avoid, series, rank_items, upgrade, now):
        with self.store.edit() as s:
            if (s['experiment_id'] != state['experiment_id'] or s.get('strategy_mode') != 'intraday'
                    or universe_mode(s) != 'daily_focus'):
                return                              # the account or its settings changed while the list was being built
            s['focus'][market] = entry
            self.apply_rotation(s, market, entry, avoid, now)
            self.record_focus(s, market, entry, series, rank_items, upgrade, now)
            names = ', '.join(p['name'] for p in entry['picks']) or '조건을 통과한 종목 없음'
            event(s, f'{LABELS[market]} 오늘의 집중 종목({"AI 뉴스 검토 + 일봉 데이터" if entry["status"] == "ok" else "일봉 데이터"}): {names}')
            if entry['ai'].get('status') == 'failed':
                event(s, f'{LABELS[market]} AI 뉴스 브리핑을 받지 못해 데이터만으로 골랐습니다: {entry["ai"].get("error", "")[:160]}', 'warning')

    def apply_rotation(self, s, market, entry, avoid, now):
        """Held names that left the list: keep them under the normal exit rules, or sell them after the open."""
        picked = {p['symbol'] for p in entry['picks']}
        for symbol, position in s['positions'].items():
            if SYMBOLS.get(symbol, {}).get('market') != market:
                continue
            if symbol in picked:
                position.pop('rotation', None)
                continue
            quote = s['quotes'].get(symbol) or {}
            decision = universe.rotation_decision(position, entry['metrics'].get(symbol), quote.get('bid') or quote.get('last'),
                                                  avoid=symbol in avoid)
            position['rotation'] = {**decision, 'at': now, 'session_date': entry['session_date']}
            event(s, f'{SYMBOLS[symbol]["name"]}: 오늘의 집중 종목에서 빠졌습니다. '
                  + ('그대로 유지합니다.' if decision['action'] == 'keep' else '개장 후 매도합니다.') + ' '+decision['reason'],
                  'info' if decision['action'] == 'keep' else 'warning')

    def record_focus(self, s, market, entry, series, rank_items, upgrade, now):
        """Store the day's picks with reference prices, and score earlier days now that a newer candle exists."""
        history = s.setdefault('focus_history', [])
        if series is not None:
            for record in history:
                if record['market'] == market and record.get('result') is None and record['session_date'] != entry['session_date']:
                    record['result'] = universe.outcome(record, series)
        picks = [p['symbol'] for p in entry['picks']]
        record = next((r for r in history if r['market'] == market and r['session_date'] == entry['session_date']), None)
        if record is not None:
            record['picks'] = picks                 # an AI upgrade re-picks the same session; the reference prices stay
        elif series is not None:
            metrics = entry['metrics']
            history.append({'market': market, 'session_date': entry['session_date'], 'built_at': now,
                            'ref': {item['symbol']: [self.ref_time(series, item['symbol']), metrics[item['symbol']]['last']]
                                    for item in rank_items if item['symbol'] in metrics},
                            'picks': picks, 'fixed': [i for i in self.fixed_symbols(s, market) if i in metrics],
                            'result': None})
        s['focus_history'] = history[-HISTORY_LIMIT:]

    @staticmethod
    def ref_time(series, symbol):
        rows = [c for c in series.get(symbol) or [] if c.get('completed')]
        return max(c['time'] for c in rows) if rows else 0
