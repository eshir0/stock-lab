"""Optional phone notifications: one short line for each fill and each warning in the operating log, sent to NOTIFY_URL when it
is set (an ntfy topic such as https://ntfy.sh/<a-long-random-topic>, or any endpoint that accepts a plain-text POST).

Only what the ledger has committed is sent, read after the fact, so a dry-run or rolled-back fill never produces a
message. The first look after a start only remembers where the ledger is, so a restart does not resend history. Off when
unset; a delivery failure is ignored and never touches trading. Paper trading only, like everything else.
"""
import httpx

from .instruments import SYMBOLS

MAX_LINES = 20


def _price(value, currency):
    return f'{value:,.0f}원' if currency == 'KRW' else f'${value:,.2f}'


def trade_line(t):
    name = SYMBOLS.get(t.get('symbol'), {}).get('name', t.get('symbol'))
    currency = t.get('currency')
    line = f"{name} {t.get('quantity')}주 모의 {'매수' if t.get('side') == 'BUY' else '매도'} {_price(t.get('price', 0), currency)}"
    if t.get('exit_reason'):
        line += ' · '+t['exit_reason']
    elif t.get('entry_watch'):
        line += ' · 조건 진입'
    if t.get('side') == 'SELL':
        realized = t.get('realized') or 0
        line += ' · 실현 '+(f'{realized:+,.0f}원' if currency == 'KRW' else f'{realized:+,.2f}달러')
    return line


class Notifier:
    def __init__(self, url):
        self.url = (url or '').strip()
        self.trades, self.warnings = None, None

    @property
    def enabled(self):
        return self.url.startswith(('https://', 'http://'))

    def lines(self, state):
        """What is new since the last look: fills, then warnings."""
        trades = [t for t in state.get('trades') or [] if t.get('id')]
        warnings = [(e.get('time'), e.get('message')) for e in state.get('events') or [] if e.get('level') == 'warning']
        first = self.trades is None
        out = [] if first else ([trade_line(t) for t in trades if t['id'] not in self.trades]
                                +['⚠ '+str(message)[:300] for key in warnings if key not in self.warnings for message in [key[1]]])
        self.trades, self.warnings = {t['id'] for t in trades}, set(warnings)
        return out

    def poll(self, state, post=None):
        lines = self.lines(state)
        if lines and self.enabled:
            try:
                (post or httpx.post)(self.url, content='\n'.join(lines[:MAX_LINES]).encode('utf-8'),
                                     headers={'Title': 'Stock Lab'}, timeout=10)
            except httpx.HTTPError:
                pass
        return lines
