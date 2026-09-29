"""Display provider-supplied Search entry points in an isolated document."""

from fastapi import HTTPException
from fastapi.responses import HTMLResponse


SEARCH_ENTRY_HEADER = 'X-StockLab-Search-Entry'
SEARCH_ENTRY_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'self'; "
    'sandbox allow-popups allow-popups-to-escape-sandbox'
)
_ROLES = frozenset(('planner', 'fundamental', 'technical', 'news', 'critic', 'director'))
_MAX_HTML_CHARS = 100_000


def register_search_display(app, store):
    """Register under /api so the application's session middleware applies.

    The middleware must remove SEARCH_ENTRY_HEADER and preserve this route's
    CSP and SAMEORIGIN framing policy. The CSP sandbox also protects direct
    navigation to this URL; the iframe sandbox is a second layer.
    """
    @app.get('/api/search-entry/{run_id}/{role}', response_class=HTMLResponse)
    def search_entry(run_id: str, role: str):
        missing = HTTPException(404, '검색 표시 자료를 찾을 수 없습니다.')
        if not 1 <= len(run_id) <= 64 or role not in _ROLES:
            raise missing
        runs = store.read().get('runs', [])
        if not isinstance(runs, list):
            raise missing
        # Only the current ledger's retained runs and known analyst reports.
        for run in reversed(runs[-40:]):
            if not isinstance(run, dict) or run.get('id') != run_id:
                continue
            reports = run.get('reports', [])
            if not isinstance(reports, list):
                raise missing
            for report in reversed(reports[-6:]):
                if not isinstance(report, dict) or report.get('role') != role:
                    continue
                html = report.get('search_entry_point')
                if (not isinstance(html, str) or not html.strip()
                        or len(html) > _MAX_HTML_CHARS):
                    raise missing
                # Preserve Google's returned HTML and styles byte-for-byte.
                # It is untrusted: scripts, forms, same-origin privileges and
                # top navigation remain disabled by both sandbox policies.
                return HTMLResponse(html, headers={
                    SEARCH_ENTRY_HEADER: '1',
                    'Content-Security-Policy': SEARCH_ENTRY_CSP,
                    'X-Frame-Options': 'SAMEORIGIN',
                    'X-Content-Type-Options': 'nosniff',
                    'Referrer-Policy': 'no-referrer',
                    'Cache-Control': 'no-store',
                })
            raise missing
        raise missing
