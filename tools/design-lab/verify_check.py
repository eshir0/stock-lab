"""DOM checks of the verification panel, the month-only experiment form and the cost note (no screenshots)."""
import sys
sys.path.insert(0, '/lab')
from playwright.sync_api import sync_playwright
from shot import BASE, PASSWORD

results, problems = [], []
def check(name, ok, detail=''):
    results.append((name, bool(ok), detail))

with sync_playwright() as p:
    browser = p.chromium.launch(args=['--no-sandbox'])
    page = browser.new_context(viewport={'width': 1440, 'height': 900}, locale='ko-KR').new_page()
    page.on('pageerror', lambda e: problems.append('pageerror: ' + str(e)[:160]))
    page.on('response', lambda r: problems.append(f'http {r.status} {r.url}') if r.status >= 400 and not (r.status == 401 and r.url.endswith('/api/state')) else None)
    page.on('console', lambda m: problems.append('console.' + m.type + ': ' + m.text[:160]) if m.type in ('error', 'warning') and 'status of 401' not in m.text else None)
    page.goto(BASE); page.fill('#password', PASSWORD); page.click('#login-form [type=submit]')
    page.wait_for_selector('#workspace:not([hidden])'); page.wait_for_timeout(2500)
    st = page.evaluate("fetch('/api/state').then(r => r.json())")
    v, card = st['verification'], st['scorecard']
    check('the state carries the fixed plan and its verdict', v and v['criteria']['min_days'] == 56 and v['status'] == 'collecting', str(v and v['status']))
    panel = page.inner_text('#verify-panel')
    check('the panel shows the plan and its status', '검증 계획' in panel and '표본을 모으는 중' in panel, panel[:100])
    check('three progress rows', page.locator('#verify-panel .verify-progress').count() == 3)
    widths = page.evaluate("[...document.querySelectorAll('#verify-panel .verify-fill')].map(e => e.getBoundingClientRect().width / e.parentElement.getBoundingClientRect().width)")
    check('the day bar is filled about 20/56', widths and .3 < widths[0] < .42, str(widths))
    check('every check is listed with a verdict', page.locator('#verify-panel .verify-check').count() == len(v['checks']) >= 4, str(len(v['checks'])))
    check('the report card is computed from the ledger', card['closed'] >= 1 and '승률' in panel and '거래당 기대값' in panel, str(card['closed']))
    check('the index comparison names KODEX 200 and SPDR', 'KODEX 200' in panel and 'SPDR' in panel, panel[-300:])
    check('the AI-vs-rule line is shown', 'AI 판단 vs 규칙대로' in panel)
    check('the cost note gives the real schedule', all(k in page.inner_text('#cost-note') for k in ('거래세', 'ETF 면제', '10달러', '지정가')), page.inner_text('#cost-note')[:120])
    page.click('#experiment-open'); page.wait_for_timeout(300)
    options = page.evaluate("[...document.querySelectorAll('#horizon option')].map(o => o.value)")
    check('the new-experiment form offers the month plan only', options == ['month'], str(options))
    check('the form no longer mentions day trading', '단타' not in page.inner_html('#experiment-dialog'))
    check('defaults are 30% per order and per name', page.input_value('#max-order-pct') == '30' and page.input_value('#max-position-pct') == '30')
    page.click('#experiment-cancel')
    check('no day-trading wording on the page', '단타' not in page.inner_text('body'), [l for l in page.inner_text('body').split('\n') if '단타' in l][:3])
    page.set_viewport_size({'width': 390, 'height': 844}); page.wait_for_timeout(800)
    check('phone: no horizontal page scroll', page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth") <= 1)
    over = page.evaluate("[...document.querySelectorAll('#verify-panel *')].filter(e => !e.closest('.table-wrap') && e.getBoundingClientRect().right > document.documentElement.clientWidth + 1).length")
    check('phone: nothing in the panel pokes out', over == 0, str(over))
    page.evaluate("document.documentElement.dataset.theme = 'dark'"); page.wait_for_timeout(300)
    check('dark theme renders the panel', page.locator('#verify-panel .verify-check').count() >= 4)
    check('no script errors or CSP violations', not problems, '; '.join(problems[:4]))
    browser.close()
bad = [r for r in results if not r[1]]
for name, ok, detail in results:
    print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else '  -> ' + str(detail)))
print(f'{len(results)-len(bad)}/{len(results)} passed')
