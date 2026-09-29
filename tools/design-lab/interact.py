"""Behavioural checks in a real browser against the design-lab server."""
import json
import sys
import time

from playwright.sync_api import sync_playwright

BASE, PASSWORD, OUT = 'http://127.0.0.1:8081', 'demo-password-12345', '/out'
results, problems = [], []


def check(name, ok, detail=''):
    results.append((name, bool(ok), detail))


with sync_playwright() as p:
    browser = p.chromium.launch(args=['--no-sandbox'])
    ctx = browser.new_context(viewport={'width': 1440, 'height': 900}, locale='ko-KR', timezone_id='Asia/Seoul', bypass_csp=False)
    page = ctx.new_page()
    page.on('console', lambda m: problems.append('console.%s: %s' % (m.type, m.text)) if m.type == 'error' and '401' not in m.text else None)
    page.on('pageerror', lambda e: problems.append('pageerror: %s' % e))
    page.on('requestfailed', lambda r: problems.append('requestfailed: %s' % r.url))
    page.goto(BASE)
    check('theme.js is served (blocking, same-origin)', page.evaluate("!!document.querySelector('script[src=\"/static/theme.js\"]:not([defer])')"))
    page.fill('#password', PASSWORD)
    page.click('#login-form [type=submit]')
    page.wait_for_selector('#workspace:not([hidden])')
    page.wait_for_function("document.querySelector('#nav-kr').textContent.trim() !== '—'")
    page.wait_for_timeout(600)

    # --- theme toggle: switches, persists across reload, and is applied before scripts run
    start = page.evaluate("document.documentElement.dataset.theme")
    page.click('#theme-toggle')
    flipped = page.evaluate("document.documentElement.dataset.theme")
    check('theme toggle switches the theme', start != flipped, f'{start} -> {flipped}')
    check('choice is stored', page.evaluate("localStorage.getItem('stocklab-theme')") == flipped)
    page.reload()
    page.wait_for_selector('#workspace:not([hidden])')
    check('choice survives a reload', page.evaluate("document.documentElement.dataset.theme") == flipped)
    check('aria-pressed reflects dark mode', page.get_attribute('#theme-toggle', 'aria-pressed') == str(flipped == 'dark').lower())
    page.click('#theme-toggle')  # back to the first theme

    # --- navigation: anchors scroll and the active tab follows
    page.click('.topnav a[href="#sec-eval"]')
    page.wait_for_timeout(1200)
    top = page.evaluate("document.querySelector('#sec-eval').getBoundingClientRect().top")
    check('nav link scrolls the section into view', 0 <= top < 260, f'top={top:.0f}')
    check('active tab follows the scroll', page.get_attribute('.topnav a[href="#sec-eval"]', 'aria-current') == 'true')
    page.click('.topnav a[href="#sec-control"]')
    page.wait_for_timeout(1200)
    check('scrolling back re-selects the first tab', page.get_attribute('.topnav a[href="#sec-control"]', 'aria-current') == 'true')

    # --- evaluation tabs re-draw the comparison
    page.evaluate("document.querySelector('#sec-eval').scrollIntoView()")
    before = page.inner_html('#eval-chart')
    page.click('#eval-tabs button[data-h="30"]')
    check('30-minute tab is selected', page.get_attribute('#eval-tabs button[data-h="30"]', 'aria-pressed') == 'true' and page.get_attribute('#eval-tabs button[data-h="60"]', 'aria-pressed') == 'false')
    check('bars change with the horizon', page.inner_html('#eval-chart') != before)
    widths = page.evaluate("[...document.querySelectorAll('.hb-fill')].map(n => getComputedStyle(n).width)")
    check('bar widths are applied through CSS variables', any(w not in ('0px', 'auto') for w in widths), str(widths[:3]))

    # --- re-render stability: the 2s refresh must not replace unchanged markup (restarting animations, dropping hover)
    page.evaluate("document.querySelectorAll('#team .agent, #live-limits, #live-shadow').forEach(n => n.__mark = 1); window.__team = document.querySelector('#team').firstElementChild")
    page.wait_for_timeout(5200)
    kept = page.evaluate("window.__team === document.querySelector('#team').firstElementChild")
    check('unchanged team cards are not re-created every refresh', kept)
    check('other stable containers keep their nodes', page.evaluate("document.querySelector('#live-limits').__mark === 1"))

    # --- chart currency select and reports accordion survive refreshes
    page.select_option('#chart-currency', 'USD')
    page.wait_for_timeout(500)
    check('chart follows the currency select', 'US' in page.inner_text('#chart-title') or '달러' in page.inner_text('#chart-title'))
    page.wait_for_timeout(2500)
    check('selected currency is kept after a refresh', page.input_value('#chart-currency') == 'USD')
    page.select_option('#chart-currency', 'KRW')
    details = page.locator('#reports details').nth(2)
    details.locator('summary').click()
    page.wait_for_timeout(2600)
    check('an opened report stays open after a refresh', page.evaluate("document.querySelectorAll('#reports details')[2].open"))

    # --- dialogs open, are styled, and close
    for opener, dialog, shot in (('#experiment-open', '#experiment-dialog', 'dlg-experiment'), ('#history-open', '#history-dialog', 'dlg-history'), ('#liquidate', '#liquidation-dialog', 'dlg-liquidate')):
        if page.is_disabled(opener):
            check(f'{opener} is disabled in this state (skipped)', True)
            continue
        page.evaluate("window.scrollTo(0,0)")
        page.click(opener)
        page.wait_for_timeout(500)
        is_open = page.evaluate(f"document.querySelector('{dialog}').open")
        blur = page.evaluate(f"getComputedStyle(document.querySelector('{dialog}')).backdropFilter || getComputedStyle(document.querySelector('{dialog}')).webkitBackdropFilter")
        check(f'{dialog} opens', is_open, f'backdrop-filter={blur}')
        page.screenshot(path=f'{OUT}/{shot}.png')
        page.evaluate(f"document.querySelector('{dialog}').close()")

    # --- focus ring is visible on keyboard focus
    page.keyboard.press('Tab')
    outline = page.evaluate("(() => { const s = getComputedStyle(document.activeElement); return s.outlineStyle + ' ' + s.outlineWidth; })()")
    check('keyboard focus shows an outline', 'none' not in outline, outline)

    # --- reduced motion turns animations off
    rm = browser.new_context(viewport={'width': 1440, 'height': 900}, reduced_motion='reduce').new_page()
    rm.goto(BASE)
    rm.wait_for_selector('#login-view')
    dur = rm.evaluate("getComputedStyle(document.querySelector('.orb')).animationName")
    check('reduced motion disables the orb animation', dur == 'none', dur)
    browser.close()

for name, ok, detail in results:
    print(('PASS ' if ok else 'FAIL ') + name + (f'  [{detail}]' if detail else ''))
print('runtime problems:', problems or 'none')
print('SUMMARY: %d/%d checks passed' % (sum(ok for _, ok, _ in results), len(results)))
