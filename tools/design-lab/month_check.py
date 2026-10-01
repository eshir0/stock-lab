"""DOM-level checks of the month-horizon screen and the experiment form (no screenshots)."""
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
    page.goto(BASE)
    page.fill('#password', PASSWORD)
    page.click('#login-form [type=submit]')
    page.wait_for_selector('#workspace:not([hidden])')
    page.wait_for_timeout(2500)
    text = lambda sel: page.inner_text(sel)
    st = page.evaluate("fetch('/api/state').then(r => r.json())")
    check('the seeded experiment is a month plan', st['strategy_settings'].get('horizon') == 'month', str(st['strategy_settings']))
    check('strategy label says 1개월 스윙', text('#strategy-label') == '전문가팀 · 1개월 스윙', text('#strategy-label'))
    desc = text('#strategy-description')
    check('description shows the holding limit in days and the rule-signal note', '최대 보유 30일' in desc and '규칙 신호가 있을 때만 AI 분석' in desc, desc)
    check('the monitor line names the trailing stop', '추적 손절' in text('#monitor-warning'), text('#monitor-warning'))
    gate = text('#gate-status')
    check('the rule-signal line is visible with names, reasons and the skipped count',
          page.locator('#gate-status').is_visible() and '삼성전자 규칙 신호(모멘텀·돌파)' in gate and 'SK하이닉스 최근 분석함' in gate and 'Apple 신호 없음' in gate and '7회' in gate, gate)
    row = text('#positions')
    check('the held position shows the raised stop with a trailing tag', '추적 중' in row and '삼성전자' in row, row[:120])
    tabs = page.locator('#eval-tabs button')
    labels = [tabs.nth(i).inner_text() for i in range(tabs.count())]
    check('evaluation tabs follow the horizons in use', labels == ['30분 뒤', '60분 뒤', '다음 거래일', '5거래일 뒤(1주)', '21거래일 뒤(1달)'], str(labels))
    check('exactly one tab is selected', page.locator('#eval-tabs button[aria-pressed="true"]').count() == 1)
    before = text('#eval-chart')
    tabs.nth(4).click()
    page.wait_for_timeout(300)
    check('clicking a tab switches the chart and the pressed state', tabs.nth(4).get_attribute('aria-pressed') == 'true' and text('#eval-chart') != before or 'AI 판단' in text('#eval-chart'), text('#eval-chart')[:80])
    tabs = page.locator('#eval-tabs button')
    check('the tab survives the 2-second refresh', tabs.nth(4).get_attribute('aria-pressed') == 'true')
    page.wait_for_timeout(4500)
    check('... and is still selected afterwards', page.locator('#eval-tabs button').nth(4).get_attribute('aria-pressed') == 'true')
    summary = text('#eval-summary')
    check('the summary table lists every horizon and the rule comparison spans them', all(k in summary for k in ('다음 거래일', '5거래일 뒤(1주)', '21거래일 뒤(1달)', '30분 뒤')) and '골든크로스' in summary, summary[:100])
    head = [page.locator('#eval-recent-head th').nth(i).inner_text() for i in range(page.locator('#eval-recent-head th').count())]
    check('the recent-decisions header has one column per horizon', head[-5:] == ['30분', '60분', '1일', '5일', '21일'], str(head))
    rows = page.locator('#eval-recent tr')
    check('decision rows have a cell for every horizon column', rows.count() > 5 and all(rows.nth(i).locator('td').count() == len(head) for i in range(min(rows.count(), 12))), f'{rows.count()} rows, {len(head)} columns')
    check('a month decision shows a dash under the minute horizons', any('month-' not in '' and rows.nth(i).locator('td').nth(6).inner_text().strip() == '—' for i in range(rows.count())))
    # the desk's reports carry the month numbers
    plan = page.evaluate("document.querySelector('.trade-plan') ? document.querySelector('.trade-plan').innerText : ''")
    reports = page.evaluate("document.querySelector('#reports') ? document.querySelector('#reports').innerText : ''")
    check('the finished analysis shows its holding limit in days', re.search(r'보유 상한\s*\n?\s*\d+일', plan + reports) is not None, (plan + reports)[:200])
    # experiment form
    page.click('#experiment-open')
    page.wait_for_timeout(300)
    check('the form defaults to a month plan in days', page.input_value('#horizon') == 'month' and page.input_value('#max-holding') == '30' and text('#max-holding-label') == '최대 보유 기간 · 일')
    check('the horizon note explains the trailing stop', '추적 손절' in text('#horizon-description'))
    page.select_option('#horizon', 'intraday')
    check('switching to a same-session plan changes the unit and bounds', page.input_value('#max-holding') == '120' and text('#max-holding-label') == '최대 보유 시간 · 분'
          and page.get_attribute('#max-holding', 'min') == '15' and page.get_attribute('#max-holding', 'max') == '240')
    page.select_option('#horizon', 'month')
    check('... and back', page.input_value('#max-holding') == '30' and page.get_attribute('#max-holding', 'max') == '30')
    page.fill('#max-holding', '45')
    check('an out-of-range holding is flagged by the form itself', page.evaluate("document.getElementById('max-holding').validity.rangeOverflow"))
    page.fill('#max-holding', '10')
    sent = {}
    def capture(route, request):
        if request.url.endswith('/api/experiments') and request.method == 'POST':
            sent.update(json.loads(request.post_data)); route.fulfill(status=409, content_type='application/json', body='{"detail":"test"}')
        else:
            route.continue_()
    page.route('**/api/experiments', capture)
    page.click('#experiment-create')
    page.wait_for_timeout(600)
    check('the request carries horizon=month and the days converted to minutes', sent.get('horizon') == 'month' and sent.get('max_holding_minutes') == 14400, str(sent))
    page.unroute('**/api/experiments')
    page.click('#experiment-cancel')
    # phone width: nothing spills sideways
    page.set_viewport_size({'width': 390, 'height': 844})
    page.wait_for_timeout(800)
    overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    check('no horizontal page scroll at phone width', overflow <= 1, f'overflow {overflow}px')
    check('no script errors or CSP violations', not problems, '; '.join(problems[:4]))
    browser.close()

bad = [r for r in results if not r[1]]
for name, ok, detail in results:
    print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else '  -> ' + detail))
print(f'{len(results)-len(bad)}/{len(results)} passed')
sys.exit(1 if bad else 0)
