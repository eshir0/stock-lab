"""DOM-level checks of the daily focus card and the experiment form (no screenshots)."""
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
    page.on('pageerror', lambda e: problems.append('pageerror: ' + str(e)[:120]))
    page.on('console', lambda m: problems.append('console.' + m.type + ': ' + m.text[:120]) if m.type in ('error', 'warning') else None)
    page.goto(BASE)
    page.fill('#password', PASSWORD)
    page.click('#login-form [type=submit]')
    page.wait_for_selector('#workspace:not([hidden])')
    page.wait_for_timeout(2500)

    text = lambda sel: page.inner_text(sel)
    check('focus card exists with its nav link', page.locator('#sec-focus').count() == 1 and page.locator('.topnav a[href="#sec-focus"]').count() == 1)
    check('mode pill shows the daily list', '일일 집중' in text('#focus-mode'), text('#focus-mode'))
    for market in ('kr', 'us'):
        picks = page.locator(f'#focus-{market} .focus-pick')
        check(f'{market.upper()}: picks rendered', 1 <= picks.count() <= 3, f'{picks.count()} picks')
        check(f'{market.upper()}: every pick shows score, 1-month/5-day/extension numbers and a source chip',
              all(all(k in picks.nth(i).inner_text() for k in ('최근 1개월', '20일선 대비', '점')) and
                  ('AI 선정' in picks.nth(i).inner_text() or '데이터 선정' in picks.nth(i).inner_text()) for i in range(picks.count())))
        check(f'{market.upper()}: status pill says where the list came from', re.search('AI 뉴스 검토|일봉 데이터만|계산 실패|개장 90분', text(f'#focus-{market} .focus-status')) is not None, text(f'#focus-{market} .focus-status'))
    fills = page.evaluate("[...document.querySelectorAll('.focus-bar-fill')].map(n => getComputedStyle(n).width)")
    check('score bars are drawn through the CSS variable', fills and any(w not in ('0px', 'auto') for w in fills), str(fills[:3]))
    ai_chip = page.locator('.focus-chip.ai').count()
    check('AI-picked names carry the AI chip and a priced-in-risk chip', ai_chip >= 1 and page.locator('.focus-chip.risk-low, .focus-chip.risk-medium').count() >= 1, f'ai chips {ai_chip}')
    page.click('#focus-excluded >> xpath=ancestor::details/summary')
    check('the excluded list opens', page.evaluate("document.querySelector('#focus-excluded').closest('details').open"))
    excluded = text('#focus-excluded')
    check('excluded names are listed with reasons (downtrend and chasing guards visible)',
          page.locator('.focus-excluded-list li').count() >= 2 and '20일선 아래' in excluded and '추격 금지' in excluded, excluded[:160])
    held = text('#focus-held')
    check('a held name that left the list is explained (keep vs sell)', '목록에서 빠진 보유 종목' in held and ('유지' in held or '개장 후 매도' in held), held[:90])
    check('the evaluation line states the sample size and the caveat', '표본' in text('#focus-eval') and ('우연' in text('#focus-eval') or '판단할 수 없습니다' in text('#focus-eval')), text('#focus-eval')[:80])
    st = page.evaluate("fetch('/api/state').then(r => r.json())")
    expected = {p['symbol'] for m in ('KR', 'US') for p in st['focus'][m]['picks']} | set(st['positions'])
    shown = {i['symbol'] for i in st['instruments']}
    rows = page.locator('#watchlist .watchrow').count()
    check('the watchlist is exactly today\'s picks plus anything held (not the whole pool)', shown == expected and rows == len(expected),
          f'rows {rows}, picks+held {sorted(expected)}, shown {sorted(shown)}')
    # 2-second re-render must not recreate identical focus markup
    page.evaluate("window.__col = document.querySelector('#focus-us').firstElementChild")
    page.wait_for_timeout(4500)
    check('unchanged focus markup is not re-created on refresh', page.evaluate("window.__col === document.querySelector('#focus-us').firstElementChild"))
    # navigation
    page.click('.topnav a[href="#sec-focus"]')
    page.wait_for_timeout(900)
    check('nav link scrolls to the card and marks it current', page.evaluate("document.querySelector('.topnav a[href=\"#sec-focus\"]').getAttribute('aria-current') === 'true'"))
    # experiment form
    page.evaluate("window.scrollTo(0,0)")
    page.evaluate("document.querySelector('#stop') && !document.querySelector('#stop').disabled && document.querySelector('#stop').click()")
    page.wait_for_timeout(1200)
    page.click('#experiment-open')
    check('the form offers the universe mode, defaulting to the daily list', page.input_value('#universe-mode') == 'daily_focus' and page.locator('#universe-mode option').count() == 2)
    page.select_option('#universe-mode', 'fixed')
    page.fill('#experiment-input-name', '고정 종목 확인')
    page.click('#experiment-create')
    page.wait_for_timeout(2500)
    check('a fixed-universe experiment shows the fixed lineup and says so', '고정 종목' in text('#focus-mode') and '고정 종목 사용 중' in text('#focus-kr .focus-status'), text('#focus-mode'))
    check('the strategy summary names the universe mode', '고정 종목' in text('#strategy-description'), text('#strategy-description')[:100])
    page.evaluate("window.scrollTo(0,0)")
    page.click('#experiment-open')
    page.select_option('#universe-mode', 'daily_focus')
    page.fill('#experiment-input-name', '일일 집중 확인')
    page.click('#experiment-create')
    page.wait_for_timeout(2500)
    check('switching back gives the daily list again (waiting for the next build)', '일일 집중' in text('#focus-mode') and '개장 90분' in text('#focus-kr .focus-status'), text('#focus-kr .focus-status'))
    # phone width: nothing pokes out of the viewport
    ctx.close()
    for width in (390, 360, 820):
        c2 = browser.new_context(viewport={'width': width, 'height': 800}, locale='ko-KR')
        pg = c2.new_page()
        pg.goto(BASE); pg.fill('#password', PASSWORD); pg.click('#login-form [type=submit]'); pg.wait_for_selector('#workspace:not([hidden])'); pg.wait_for_timeout(1500)
        over = pg.evaluate("""() => { const vw = document.documentElement.clientWidth; const bad = [];
          for (const el of document.querySelectorAll('#sec-focus *')) { if (el.closest('.table-wrap, svg')) continue; const r = el.getBoundingClientRect(); if (r.width > 1 && (r.right > vw + 1 || r.left < -1)) bad.push(el.className || el.tagName); }
          return {doc: document.documentElement.scrollWidth, vw, bad: bad.slice(0, 4)}; }""")
        check(f'{width}px: the focus card fits the screen', over['doc'] <= over['vw'] and not over['bad'], str(over))
        c2.close()
    browser.close()

for name, ok, detail in results:
    print(('PASS ' if ok else 'FAIL ') + name + (f'   [{detail}]' if detail and not ok else ''))
print('runtime problems:', problems or 'none')
print('SUMMARY: %d/%d passed' % (sum(ok for _, ok, _ in results), len(results)))
