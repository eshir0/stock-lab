"""Design-lab screenshots. Usage: shot.py shots.json   (spec = list of dicts, see SHOT defaults below)."""
import copy
import json
import sys
import time

from playwright.sync_api import sync_playwright

import os
BASE = os.environ.get('LAB_BASE', 'http://127.0.0.1:8081')
PASSWORD = os.environ.get('LAB_PASSWORD', 'demo-password-12345')
OUT = '/out'


def base(s):
    """Make the demo ledger look like production (Toss mode, real AI chain, 14 of 30 calls used)."""
    s['mode'] = 'toss'
    s['daily_ai'] = {time.strftime('%Y-%m-%d', time.gmtime(s['server_time'])): 14}
    s['config'].update(providers=['claude', 'codex', 'gemini'], daily_limit=30)
    return s


def running(s):
    s['running'] = True
    s['scheduler_status'] = '삼성전자 분석 중'
    run = s['runs'][-1]
    run['status'] = 'running'
    run['active_role'], run['active_roles'] = 'research', ['fundamental', 'technical', 'news']
    run['reports'] = run['reports'][:3]
    run.pop('sizing', None)
    return s


def manual_pending(s):
    s['execution_mode'] = 'manual'
    s['running'] = True
    now = s['server_time']
    s['proposals'] = [{'id': 'p1', 'symbol': 'AAPL', 'side': 'BUY', 'quantity': 3, 'reference_price': 201.3, 'created': now-40,
                       'expires': now+140, 'status': 'pending', 'summary': '장중 거래량이 늘고 단기 추세가 이어지지만, 발표 예정 일정이 있어 손절 폭을 좁게 잡았습니다. 진입 무효화 조건: 200달러 이탈.',
                       'risks': ['실적 발표 직전 변동성', '스프레드 확대 가능'], 'target_weight_pct': 12, 'stop_loss_pct': 1.2, 'take_profit_pct': 2.4, 'max_holding_minutes': 90,
                       'sizing': {'quantity': 3, 'target_quantity': 4, 'risk_quantity': 3, 'max_buy_quantity': 5, 'estimated_stop_risk': 7.2, 'reason': '손절 위험 한도 기준으로 3주로 제한했습니다.'}},
                      {'id': 'p2', 'symbol': '005930', 'side': 'SELL', 'quantity': 4, 'reference_price': 70100, 'created': now-20, 'expires': now+160, 'status': 'pending',
                       'summary': '익절 조건에 도달해 매도를 제안합니다.', 'exit_reason': 'take_profit', 'risks': ['지정한 손절 가격은 체결 가격을 보장하지 않습니다.'], 'sizing': {'quantity': 4, 'reason': '익절 조건'}}]
    return s


def halted(s):
    s['risk_status'] = {'halted': True, 'halted_currencies': ['USD'], 'day_pnl_pct': {'KRW': -0.4, 'USD': -2.13},
                        'reason': 'USD 일일 손실 한도 · 신규 매수 중단, 청산 감시 유지'}
    s['last_error'] = '[Gemini 호출 한도] API 호출 한도에 걸렸습니다. 잠시 기다린 뒤 다시 분석하세요.'
    s['running'] = True
    return s


def empty(s):
    for key in ('trades', 'proposals', 'events', 'runs', 'evaluations'):
        s[key] = []
    s['positions'], s['history'] = {}, s['history'][:1]
    s['evaluation'] = {'decisions': 0, 'counts': {'BUY': 0, 'SELL': 0, 'HOLD': 0}, 'horizons': {h: dict(s['evaluation']['horizons'][h], scored=0) for h in ('30', '60')},
                       'engines': {}, 'pending': 0, 'min_sample': 30, 'enough_sample': False}
    s['live']['shadow'] = {'total': 0, 'would_submit': 0, 'blocked': 0, 'blocked_by': {}, 'by_side': {'BUY': 0, 'SELL': 0}, 'notional': {'KRW': '0', 'USD': '0'}, 'recent': []}
    return s


def flat(s):
    """A brand-new experiment: the equity history is a perfectly flat line."""
    now = s['server_time']
    s['history'] = [{'time': now-120+60*i, 'KRW': 1000000.0, 'USD': 1000.0, 'fresh': {'KRW': True, 'USD': True}} for i in range(3)]
    return s


TRANSFORMS = {'flat': flat, 'base': base, 'running': running, 'pending': manual_pending, 'halted': halted, 'empty': empty, 'raw': lambda s: s}


def capture(p, shot, report):
    name = shot['name']
    ctx = p.chromium.launch(args=['--no-sandbox']).new_context(
        viewport={'width': shot.get('width', 1440), 'height': shot.get('height', 900)}, device_scale_factor=shot.get('scale', 1),
        locale='ko-KR', timezone_id='Asia/Seoul', color_scheme=shot.get('color_scheme', 'light'), bypass_csp=not shot.get('csp'))
    if shot.get('theme'):
        ctx.add_init_script("try{localStorage.setItem('stocklab-theme','%s')}catch(e){}" % shot['theme'])
    page = ctx.new_page()
    issues = report.setdefault(name, [])
    page.on('console', lambda m: issues.append('console.%s: %s' % (m.type, m.text)) if m.type in ('error', 'warning') else None)
    page.on('pageerror', lambda e: issues.append('pageerror: %s' % e))
    page.on('requestfailed', lambda r: issues.append('requestfailed: %s' % r.url))
    page.on('response', lambda r: issues.append('http %s: %s' % (r.status, r.url)) if r.status >= 400 and r.url.startswith(BASE) and '/api/state' not in r.url else None)
    names = shot.get('states', ['base'])

    def handler(route):
        response = route.fetch()
        if response.status != 200:
            return route.fulfill(response=response)
        data = response.json()
        for n in names:
            data = TRANSFORMS[n](data) or data
        route.fulfill(response=response, json=data)
    page.route('**/api/state', handler)
    page.goto(BASE)
    if not shot.get('login_only'):
        page.fill('#password', PASSWORD)
        page.click('#login-form [type=submit]')
        page.wait_for_selector('#workspace:not([hidden])', timeout=15000)
        page.wait_for_function("document.querySelector('#nav-kr') && document.querySelector('#nav-kr').textContent.trim() !== '—'", timeout=15000)
    page.evaluate('document.fonts.ready')
    for step in shot.get('do', []):
        page.evaluate(step)
    page.wait_for_timeout(shot.get('wait', 700))
    if shot.get('scroll'):
        page.evaluate("document.querySelector(%s).scrollIntoView({block:'start'})" % json.dumps(shot['scroll']))
        page.wait_for_timeout(300)
    ext = 'jpg' if shot.get('jpeg') else 'png'
    path = '%s/%s.%s' % (OUT, name, ext)
    opts = {'type': 'jpeg', 'quality': 88} if shot.get('jpeg') else {}
    if shot.get('selector'):
        page.add_style_tag(content='.topbar{position:static!important}')
        page.locator(shot['selector']).first.screenshot(path=path, **opts)
    elif shot.get('clip'):
        page.screenshot(path=path, full_page=True, clip=shot['clip'], **opts)
    else:
        page.screenshot(path=path, full_page=shot.get('full', False), **opts)
    ctx.browser.close()


if __name__ == '__main__':
    shots = json.load(open(sys.argv[1]))
    report = {}
    with sync_playwright() as p:
        for shot in shots:
            try:
                capture(p, shot, report)
            except Exception as exc:
                report.setdefault(shot['name'], []).append('FAILED: %r' % exc)
    json.dump(report, open(OUT+'/report.json', 'w'), ensure_ascii=False, indent=1)
    for name, issues in report.items():
        print(name, '->', 'OK' if not issues else issues[:6])
