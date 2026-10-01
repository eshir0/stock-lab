"""DOM-level checks of a day-trading experiment's focus card (no screenshots)."""
import json, re, sys
sys.path.insert(0, '/lab')
from playwright.sync_api import sync_playwright
from shot import BASE, PASSWORD

results, problems = [], []
def check(name, ok, detail=''):
    results.append((name, bool(ok), detail))

with sync_playwright() as p:
    browser = p.chromium.launch(args=['--no-sandbox'])
    ctx = browser.new_context(viewport={'width': 1440, 'height': 900}, locale='ko-KR')
    page = ctx.new_page()
    page.on('pageerror', lambda e: problems.append('pageerror: ' + str(e)[:160]))
    page.on('response', lambda r: problems.append(f'http {r.status} {r.url}') if r.status >= 400 and not (r.status == 401 and r.url.endswith('/api/state')) and not (r.status == 409 and r.url.endswith('/api/experiments')) else None)
    page.on('console', lambda m: problems.append('console.' + m.type + ': ' + m.text[:160]) if m.type in ('error', 'warning') and 'status of 401' not in m.text and 'status of 409' not in m.text else None)
    page.goto(BASE); page.fill('#password', PASSWORD); page.click('#login-form [type=submit]')
    page.wait_for_selector('#workspace:not([hidden])'); page.wait_for_timeout(2500)
    text = lambda sel: page.inner_text(sel)
    st = page.evaluate("fetch('/api/state').then(r => r.json())")
    check('the seeded experiment is a day-trading one', st['strategy_settings'].get('horizon') == 'intraday' and st['focus_config']['profile'] == 'volatility', str(st['focus_config']))
    check('the strategy card says day trading', text('#strategy-label') == '전문가팀 · 당일 단타', text('#strategy-label'))
    check('the mode pill names the volatility profile', '변동성 우선(단타)' in text('#focus-mode'), text('#focus-mode'))
    intro = text('#focus-intro')
    check('the intro explains the day-trading screen', '단타 실험입니다' in intro and '하루 평균 변동폭' in intro and '방향은 거르지 않습니다' in intro, intro[:80])
    for market in ('kr', 'us'):
        picks = page.locator(f'#focus-{market} .focus-pick')
        n = picks.count()
        check(f'{market.upper()}: picks rendered', 1 <= n <= 3, f'{n} picks')
        joined = ' '.join(picks.nth(i).inner_text() for i in range(n))
        check(f'{market.upper()}: every pick shows range, average move, volume ratio (not the month numbers)',
              n > 0 and all(k in joined for k in ('하루 평균 변동폭', '평균 등락', '어제 거래량')) and '최근 1개월' not in joined and '20일선 대비' not in joined, joined[:160])
    ranges = page.evaluate("[...document.querySelectorAll('.focus-pick .focus-metrics')].map(p => parseFloat(p.querySelector('b').innerText)).filter(v => !Number.isNaN(v))")
    check('the listed names really are the lively ones (every range at least 1.8%)', ranges and min(ranges) >= 1.8, str(ranges[:8]))
    page.click('#focus-excluded >> xpath=ancestor::details/summary')
    excluded = text('#focus-excluded')
    check('quiet names are excluded with the day-trading reason', '단타 기준' in excluded, excluded[:200])
    check('a five-day freefall and a far-too-wild name are excluded with reasons', '급락' in excluded and '너무 큼' in excluded)
    check('downtrend names are NOT excluded for trend reasons', '20일선 아래' not in excluded and '추격 금지' not in excluded, excluded[:200])
    ev = text('#focus-eval')
    check('the evaluation line is about how much the picks moved', '하루 변동폭 기준' in ev and '더 크게 움직인 날' in ev and '수익을 뜻하지 않고' in ev, ev[:160])
    page.click('#experiment-open'); page.wait_for_timeout(300)
    page.select_option('#horizon', 'intraday')
    note = text('#horizon-description')
    check('the new-experiment form tells day traders how names are chosen', '하루 변동폭이 크고 거래가 활발한' in note, note)
    check('the form defaults to the larger virtual accounts', page.input_value('#seed-krw') == '10000000' and page.input_value('#seed-usd') == '10000', page.input_value('#seed-krw'))
    check('a day-trading form proposes 100% per order and per name', page.input_value('#max-order-pct') == '100' and page.input_value('#max-position-pct') == '100', page.input_value('#max-order-pct') + '/' + page.input_value('#max-position-pct'))
    page.select_option('#horizon', 'month')
    check('a month plan proposes 30% and 30%', page.input_value('#max-order-pct') == '30' and page.input_value('#max-position-pct') == '30')
    page.select_option('#horizon', 'intraday')
    sent = {}
    def capture(route, request):
        if request.url.endswith('/api/experiments') and request.method == 'POST':
            import json as _j; sent.update(_j.loads(request.post_data)); route.fulfill(status=409, content_type='application/json', body='{"detail":"test"}')
        else:
            route.continue_()
    page.route('**/api/experiments', capture)
    page.click('#experiment-create'); page.wait_for_timeout(500)
    check('the request carries both limits and the seed money', sent.get('max_position_pct') == 100 and sent.get('max_order_pct') == 100 and sent.get('seed_krw') == 10000000 and sent.get('seed_usd') == 10000, str(sent))
    page.unroute('**/api/experiments')
    page.fill('#max-position-pct', '5')
    check('a position limit under 10% is flagged by the form itself', page.evaluate("document.getElementById('max-position-pct').validity.rangeUnderflow"))
    page.fill('#max-position-pct', '100')
    page.select_option('#horizon', 'month')
    check('... and month plans the trend way', '상승 추세 종목 위주' in text('#horizon-description'))
    check('the universe option mentions both styles', '단타는 변동성 큰 종목' in text('#universe-mode option:first-child') or '단타는 변동성 큰 종목' in page.inner_html('#universe-mode'))
    page.click('#experiment-cancel')
    page.set_viewport_size({'width': 390, 'height': 844}); page.wait_for_timeout(800)
    row = page.inner_text('#positions')
    check('a fractional US holding is shown with its decimals', '0.2727주' in row and 'Apple' in row, row[:120])
    check('the strategy card names the per-name limit', '종목당 최대 100%' in page.inner_text('#strategy-description'), page.inner_text('#strategy-description'))
    check('no horizontal page scroll at phone width', page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth") <= 1)
    check('no script errors or CSP violations', not problems, '; '.join(problems[:4]))
    browser.close()

bad = [r for r in results if not r[1]]
for name, ok, detail in results:
    print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else '  -> ' + detail))
print(f'{len(results)-len(bad)}/{len(results)} passed')
sys.exit(1 if bad else 0)
