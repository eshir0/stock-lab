"""The page, its script and its stylesheet must keep agreeing with each other and with the CSP.

app.js drives the page by element id and by the class names it writes into templates. A redesign that drops an
id, or a template class nobody styles any more, silently breaks the screen; an inline style or script is blocked
by the strict Content-Security-Policy. These checks catch both without needing a browser.
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1]/'app'/'static'
HTML = (STATIC/'index.html').read_text(encoding='utf-8')
JS = (STATIC/'app.js').read_text(encoding='utf-8')
CSS = (STATIC/'style.css').read_text(encoding='utf-8')

# Hooks that are deliberately unstyled (structure/semantics only, or styled through SVG attributes).
UNSTYLED = {'approvals', 'brand-name', 'eq-area', 'market-panel', 'positions-panel', 'sp-area', 'tile-usd'}
# ids the script builds from a prefix (for example $('performance-' + id) with id in kr/us).
DYNAMIC_IDS = {prefix+suffix for prefix in ('performance-', 'nav-', 'cash-', 'return-', 'valuation-', 'focus-') for suffix in ('kr', 'us')} | {'ai-ring-pct'}


def html_ids():
    return re.findall(r'\bid="([^"]+)"', HTML)


def referenced_ids():
    found = set(re.findall(r"\$\('([^']+)'\)", JS)) | set(re.findall(r"getElementById\('([^']+)'\)", JS))
    found |= set(re.findall(r"(?:setHtml|setRing|drawSpark)\('([^']+)'", JS))
    return {i for i in found if not i.endswith('-')} | DYNAMIC_IDS


def class_tokens(source):
    tokens = set()
    for value in re.findall(r'class=\\?"([^"\\]*)\\?"', source):
        if '${' in value:
            # Conditional classes: only the quoted literals inside the expression are class names.
            for literal in re.findall(r"'([^']*)'", value):
                tokens.update(literal.split())
        else:
            tokens.update(value.split())
    return {t for t in tokens if re.fullmatch(r'[a-z][a-z0-9_-]*', t)}


def test_every_id_the_script_uses_exists_exactly_once_in_the_page():
    ids = html_ids()
    assert not {i for i in ids if ids.count(i) > 1}, 'duplicate ids'
    assert not sorted(referenced_ids()-set(ids)), 'ids used by app.js but missing from index.html'


def test_every_class_written_by_the_page_and_script_has_a_style_rule():
    styled = set(re.findall(r'\.([A-Za-z_][\w-]*)', CSS))
    used = class_tokens(HTML) | class_tokens(JS)
    assert not sorted(used-styled-UNSTYLED), 'classes with no CSS rule'


def test_navigation_anchors_point_at_real_sections():
    ids = set(html_ids())
    anchors = re.findall(r'href="#([^"]+)"', HTML)
    assert anchors and all(a in ids for a in anchors)


def test_the_page_respects_the_strict_csp():
    # script-src 'self' and style-src 'self': no inline scripts, inline style attributes or <style> blocks.
    assert not re.search(r'<script(?![^>]*\bsrc=)[^>]*>', HTML)
    assert not re.search(r'\sstyle=', HTML) and '<style' not in HTML
    assert 'style=' not in JS, 'templates must not emit inline style attributes'
    assert not re.search(r'@import|url\(\s*["\']?https?:', CSS), 'the stylesheet may not load external resources'
    for reference in re.findall(r'(?:src|href)="(/static/[^"#?]+)"', HTML):
        assert (STATIC/reference.removeprefix('/static/')).is_file(), reference


def test_the_theme_is_applied_before_first_paint_and_both_themes_exist():
    assert HTML.index('/static/theme.js') < HTML.index('/static/app.js')
    assert re.search(r'<script src="/static/theme\.js"></script>', HTML), 'theme.js must be blocking (no defer)'
    assert ':root[data-theme="dark"]' in CSS and 'color-scheme:dark' in CSS and 'color-scheme:light' in CSS
    theme = (STATIC/'theme.js').read_text(encoding='utf-8')
    assert "'stocklab-theme'" in theme and 'prefers-color-scheme' in theme and 'try' in theme


def test_reduced_motion_is_respected():
    assert '@media(prefers-reduced-motion:reduce)' in CSS.replace(' ', '')
