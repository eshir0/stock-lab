"""DOM-level checks of the conditional-entry panel (no screenshots)."""
import sys
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
    page.on('response', lambda r: problems.append(f'http {r.status} {r.url}') if r.status >= 400 and not (r.status == 401 and r.url.endswith('/api/state')) else None)
    page.on('console', lambda m: problems.append('console.' + m.type + ': ' + m.text[:160]) if m.type in ('error', 'warning') and 'status of 401' not in m.text else None)
    page.goto(BASE); page.fill('#password', PASSWORD); page.click('#login-form [type=submit]')
    page.wait_for_selector('#workspace:not([hidden])'); page.wait_for_timeout(2500)
    text = lambda sel: page.inner_text(sel)
    st = page.evaluate("fetch('/api/state').then(r => r.json())")
    waiting = [w for w in st['watches'] if w['status'] == 'waiting']
    check('the state carries two waiting plans and the latest finished ones', len(waiting) == 2 and len(st['watches']) == 5, str(len(st['watches'])))
    check('the panel is in the decisions card with a count of 2', page.locator('#proposal-list .watch-list h3 .pill').inner_text().strip() == '2')
    cards = page.locator('#proposal-list .watch-card')
    check('two cards are rendered', cards.count() == 2, str(cards.count()))
    first, second = cards.nth(0).inner_text(), cards.nth(1).inner_text()
    check('the first card is the Korean breakout with its kind, level, invalidation and time left',
          'SK하이닉스' in first and '돌파 매수' in first and '이상' in first and '₩' in first and '무효 가격' in first and '남은 시간' in first and '분' in first, first[:200])
    check('its note about thin volume is shown', '거래량이 받쳐주지 않아' in first and '2회 연속' in first, first[-120:])
    check('the second card is the US pullback in dollars with "이하"', 'Apple' in second and '눌림 매수' in second and '$' in second and '이하' in second, second[:200])
    gaps = page.evaluate("[...document.querySelectorAll('.watch-card dl > div')].filter(d => d.querySelector('dt').innerText === '조건까지').map(d => d.querySelector('dd').innerText)")
    check('the distance to the condition is signed (+ for a breakout still to come, - for a pullback)', len(gaps) == 2 and gaps[0].startswith('+') and gaps[1].startswith('-'), str(gaps))
    done = text('#proposal-list .watch-done')
    check('the finished plans are listed with how they ended', '체결' in done and '기한 만료' in done and '무효 가격 이탈' in done and '2.6053주' in done, done[:200])
    check('the auto note explains conditional entries', '조건 진입' in text('#proposal-list .auto-note'))
    check('the trade made by a plan is tagged in the trades table and the recent list', '조건 진입' in text('#trades') and '조건 진입' in text('#proposal-list .auto-trades'), text('#trades')[:120])
    director = page.locator('#reports details[data-role=director]').inner_text()
    check('the director report shows the plan it left', '조건 진입 계획' in director and '돌파 매수' in director and '무효 가격' in director and '50분 유효' in director, director[-300:])
    check('the run notice repeats the registered plan', '조건 진입 등록' in text('#reports'), text('#reports')[-160:])
    check('the team line says the research was reused and how many calls it saved', '조사 23분 전 자료 재사용(AI 3회 절약)' in text('#team-context'), text('#team-context'))
    cards_text = page.evaluate("[...document.querySelectorAll('#team .agent')].map(a => a.innerText.replace(/\\s+/g, ' '))")
    check('the three reused roles show "재사용 · 23분 전" and the others "검토 완료"', sum('재사용 · 23분 전' in c for c in cards_text) == 3 and sum('검토 완료' in c for c in cards_text) >= 1, str(cards_text))
    summaries = page.evaluate("[...document.querySelectorAll('#reports details > summary')].map(s => s.innerText)")
    check('the report headers of the reused research say so', sum('재사용 23분 전' in x for x in summaries) == 3, str(summaries))
    check('the evaluation table can show a plan as the selector', page.evaluate("typeof watchKinds === 'object'"))
    clamp = page.evaluate("getComputedStyle(document.querySelector('.watch-why')).webkitLineClamp")
    check('the AI explanation is clamped to three lines', clamp == '3', str(clamp))
    cols = page.evaluate("getComputedStyle(document.querySelector('.watch-card dl')).gridTemplateColumns.split(' ').length")
    check('desktop: the facts sit in three columns', cols == 3, str(cols))
    page.set_viewport_size({'width': 390, 'height': 844}); page.wait_for_timeout(800)
    cols = page.evaluate("getComputedStyle(document.querySelector('.watch-card dl')).gridTemplateColumns.split(' ').length")
    check('phone: two columns', cols == 2, str(cols))
    check('phone: no horizontal page scroll', page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth") <= 1)
    over = page.evaluate("[...document.querySelectorAll('.watch-card, .watch-card *')].filter(e => e.getBoundingClientRect().right > document.documentElement.clientWidth + 1).length")
    check('phone: nothing in a card pokes out of the screen', over == 0, str(over))
    page.evaluate("document.documentElement.dataset.theme = 'dark'"); page.wait_for_timeout(300)
    check('dark theme renders the same cards', page.locator('#proposal-list .watch-card').count() == 2)
    check('no script errors or CSP violations', not problems, '; '.join(problems[:4]))
    browser.close()

bad = [r for r in results if not r[1]]
for name, ok, detail in results:
    print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else '  -> ' + detail))
print(f'{len(results)-len(bad)}/{len(results)} passed')
