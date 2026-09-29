"""Measure real rendered text contrast: hide all text, screenshot the background, sample under each text box."""
import base64
import json
import re
import sys

from playwright.sync_api import sync_playwright

from shot import BASE, PASSWORD, TRANSFORMS

COLLECT = r"""() => {
  const out = [], walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode, text = node.nodeValue.trim();
    if (!text) continue;
    const el = node.parentElement;
    if (!el || el.closest('script,style,noscript,.sprite,[hidden],dialog:not([open])')) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || +cs.opacity === 0) continue;
    const range = document.createRange(); range.selectNodeContents(node);
    const rects = [...range.getClientRects()].filter(r => r.width > 3 && r.height > 3);
    if (!rects.length) continue;
    const svg = el instanceof SVGElement;
    out.push({text: text.slice(0, 34), color: svg ? cs.fill : cs.color, size: parseFloat(cs.fontSize) || 12, weight: +cs.fontWeight || 400,
      cls: (svg ? el.getAttribute('class') : el.className) || el.tagName.toLowerCase(), grad: cs.backgroundClip === 'text' || cs.webkitBackgroundClip === 'text',
      rects: rects.map(r => [r.left + scrollX, r.top + scrollY, r.width, r.height])});
  }
  return out;
}"""

SAMPLER = r"""async ([b64, jobs]) => {
  const img = new Image(); img.src = 'data:image/png;base64,' + b64; await img.decode();
  const c = document.createElement('canvas'); c.width = img.width; c.height = img.height;
  const g = c.getContext('2d', {willReadFrequently: true}); g.drawImage(img, 0, 0);
  return jobs.map(rects => {
    const px = [];
    for (const [x, y, w, h] of rects) for (let i = 0; i < 6; i++) for (let j = 0; j < 3; j++) {
      const sx = Math.min(img.width - 1, Math.max(0, Math.round(x + (i + .5) / 6 * w))), sy = Math.min(img.height - 1, Math.max(0, Math.round(y + (j + .5) / 3 * h)));
      const d = g.getImageData(sx, sy, 1, 1).data; px.push([d[0], d[1], d[2]]);
    }
    return px;
  });
}"""


def lin(v):
    v /= 255
    return v / 12.92 if v <= .03928 else ((v + .055) / 1.055) ** 2.4


def lum(c):
    return .2126 * lin(c[0]) + .7152 * lin(c[1]) + .0722 * lin(c[2])


def ratio(a, b):
    la, lb = lum(a), lum(b)
    return (max(la, lb) + .05) / (min(la, lb) + .05)


def parse(color):
    m = re.findall(r'[\d.]+', color)
    r, g, b = (float(x) for x in m[:3])
    return (r, g, b, float(m[3]) if len(m) > 3 else 1.0)


def audit(theme, width, height, states=('base',)):
    with sync_playwright() as p:
        browser = p.chromium.launch(args=['--no-sandbox'])
        ctx = browser.new_context(viewport={'width': width, 'height': height}, locale='ko-KR', timezone_id='Asia/Seoul', bypass_csp=True)
        ctx.add_init_script("try{localStorage.setItem('stocklab-theme','%s')}catch(e){}" % theme)
        page = ctx.new_page()

        def handler(route):
            response = route.fetch()
            if response.status != 200:
                return route.fulfill(response=response)
            data = response.json()
            for n in states:
                data = TRANSFORMS[n](data) or data
            route.fulfill(response=response, json=data)
        page.route('**/api/state', handler)
        page.goto(BASE)
        page.fill('#password', PASSWORD)
        page.click('#login-form [type=submit]')
        page.wait_for_selector('#workspace:not([hidden])')
        page.wait_for_function("document.querySelector('#nav-kr').textContent.trim() !== '—'")
        page.add_style_tag(content='.topbar{position:absolute!important}*,*::before,*::after{animation:none!important;transition:none!important}')
        page.wait_for_timeout(800)
        items = page.evaluate(COLLECT)
        page.add_style_tag(content='*{color:transparent!important;-webkit-text-fill-color:transparent!important;text-shadow:none!important}svg text{fill:transparent!important}')
        page.wait_for_timeout(300)
        png = page.screenshot(full_page=True)
        probe = ctx.new_page()
        probe.goto('about:blank')
        samples = probe.evaluate(SAMPLER, [base64.b64encode(png).decode(), [i['rects'] for i in items]])
        browser.close()
    failures = []
    for item, pixels in zip(items, samples):
        if item['grad'] or not pixels:
            continue
        r, g, b, a = parse(item['color'])
        ratios = []
        for bg in pixels:
            eff = (r * a + bg[0] * (1 - a), g * a + bg[1] * (1 - a), b * a + bg[2] * (1 - a))
            ratios.append(ratio(eff, bg))
        ratios.sort()
        typical = ratios[len(ratios) // 2]
        large = item['size'] >= 24 or (item['size'] >= 18.66 and item['weight'] >= 700)
        need = 3.0 if large else 4.5
        if typical < need:
            failures.append((round(typical, 2), need, item['cls'][:38], item['text'], item['size']))
    return len(items), failures


if __name__ == '__main__':
    for theme, width, height, states in (('light', 1440, 900, ('base',)), ('dark', 1440, 900, ('base',)), ('light', 390, 844, ('base',)), ('dark', 390, 844, ('base',))):
        total, bad = audit(theme, width, height, states)
        print('== %s %dpx: %d text nodes, %d below WCAG AA' % (theme, width, total, len(bad)))
        seen = set()
        for ratio_, need, cls, text, size in sorted(bad)[:40]:
            key = (cls, round(ratio_, 1))
            if key in seen:
                continue
            seen.add(key)
            print('   %.2f (need %.1f)  .%s  "%s"  %spx' % (ratio_, need, cls, text, size))
