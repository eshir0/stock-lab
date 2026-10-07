import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Config
from .engine import Engine, RuleError
from .providers import ProviderError
from .search_display import SEARCH_ENTRY_HEADER, register_search_display
from .store import Store

STATIC = Path(__file__).parent/'static'


SESSION_SECONDS = 28800             # eight hours


def create_app(config=None, background=True, test=False):
    c = config or Config()
    c.validate()
    public_origin = os.getenv('APP_PUBLIC_ORIGIN', '').strip().rstrip('/')
    public_url = urlsplit(public_origin)
    if public_origin:
        if (public_url.scheme != 'https' or not public_url.hostname
                or public_url.username is not None or public_url.password is not None
                or public_url.path or public_url.query or public_url.fragment
                or any(ch.isspace() for ch in public_origin) or '*' in public_origin):
            raise ValueError('APP_PUBLIC_ORIGIN must be an HTTPS origin such as https://stock.eshiro.net')
        # Reading port also validates malformed/non-numeric values.
        port = public_url.port
        authority = public_url.hostname.lower()
        if ':' in authority:
            authority = '['+authority+']'
        if port is not None and port != 443:
            authority += ':'+str(port)
        public_origin = 'https://'+authority
        public_url = urlsplit(public_origin)
    store = Store(c.database_url, c.mode)
    engine = Engine(c, store)
    failures = {}

    async def monitor():
        while True:
            try:
                await asyncio.to_thread(engine.refresh)
                await asyncio.to_thread(engine.check_splits)
                await asyncio.to_thread(engine.process_desk_exits)
                await asyncio.to_thread(engine.process_liquidation)
                await asyncio.to_thread(engine.process_entry_watches)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(c.poll_seconds)

    async def intel_loop():
        while True:
            try:
                await asyncio.to_thread(engine.refresh_intel)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(15)

    async def focus_loop():
        while True:
            try:
                await asyncio.to_thread(engine.refresh_focus)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            try:
                await asyncio.to_thread(engine.score_days)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            for job in (engine.scan_pool, engine.register_splits, engine.refresh_benchmark, engine.credit_dividends, engine.settle_fx, engine.lock_verdict, engine.check_providers,
                        engine.notify):
                try:
                    await asyncio.to_thread(job)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
            await asyncio.sleep(30)

    async def work():
        while True:
            try:
                await asyncio.to_thread(engine.cycle)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(1)

    @asynccontextmanager
    async def lifespan(app):
        store.claim_process()
        engine.boot()
        tasks = [asyncio.create_task(monitor()), asyncio.create_task(work()),
                 asyncio.create_task(intel_loop()), asyncio.create_task(focus_loop())] if background else []
        yield
        engine.shutdown()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Outstanding synchronous HTTP calls can finish, but their generation cannot write proposals.
        store.release()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine, app.state.store = engine, store
    register_search_display(app, store)
    allowed = ['localhost', '127.0.0.1', os.getenv('APP_HOST', '192.168.1.117')]
    if public_origin:
        allowed.append(public_url.hostname)
    if test:
        allowed.append('testserver')
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed)

    def browser_origin(request):
        host = request.headers.get('host', '').lower()
        if public_origin and host == public_url.netloc:
            return public_origin
        # The public origin is configured by the owner; forwarded headers are not trusted.
        return request.url.scheme+'://'+host

    def on_public_origin(request):
        """The browser says it is on our configured public origin. A reverse proxy or tunnel may rewrite Host (to its
        upstream address, or add the default port), but it cannot change what the browser sends in Origin, and a page on
        another site cannot claim to be this origin."""
        return bool(public_origin) and request.headers.get('origin') == public_origin

    def origin_ok(request):
        origin = request.headers.get('origin')
        return not origin or origin == browser_origin(request) or on_public_origin(request)

    gate_log = logging.getLogger('uvicorn.error')
    gate_seen = {}

    # Tunnels allowed to say who connected (Cloudflare's CF-Connecting-IP). Every other peer is counted as itself.
    trusted_proxies = {ip.strip() for ip in os.getenv('TRUSTED_PROXY_IPS', '').split(',') if ip.strip()}

    def client_ip(request):
        """The address the login throttle counts. Behind a trusted tunnel every visitor arrives from the tunnel's address, so
        one bucket would let a stranger's failed guesses lock the owner out too; the tunnel's CF-Connecting-IP is used
        instead. Any other peer cannot claim an address."""
        peer = request.client.host if request.client else '-'
        if peer in trusted_proxies:
            try:
                return str(ipaddress.ip_address(request.headers.get('cf-connecting-ip', '').strip()))
            except ValueError:
                pass
        return peer

    # Tokens are stateless; logging out moves this line, so every token issued before it stops working (one owner, one key).
    auth = {'revoked_before': float((store.read().get('auth') or {}).get('revoked_before', 0))}

    def note_rejection(request, reason):
        """Say why a write was refused, once per distinct cause every five minutes. Headers only: no body, no cookies."""
        now = time.time()
        key = (reason, request.headers.get('host', '')[:80], request.headers.get('origin', '')[:80])
        if now-gate_seen.get(key, 0) < 300:
            return
        if len(gate_seen) > 200:
            gate_seen.clear()
        gate_seen[key] = now
        gate_log.warning('write refused (%s) %s %s host=%r origin=%r peer=%s client=%s', reason, request.method, request.url.path,
                         key[1], key[2], request.client.host if request.client else '-', client_ip(request))

    def signed(value):
        digest = hmac.new(c.session_secret.encode(), value.encode(), hashlib.sha256).hexdigest()
        return value+'.'+digest

    def authenticated(request):
        cookie = request.cookies.get('stocklab_session', '')
        try:
            value, sig = cookie.rsplit('.', 1)
            parts = value.split(':')
            expires = float(parts[0])
            issued = float(parts[1]) if len(parts) == 3 else expires-SESSION_SECONDS     # tokens from before revocation existed
            return hmac.compare_digest(signed(value), cookie) and expires > time.time() and issued > auth['revoked_before']
        except (ValueError, TypeError):
            return False

    def refused(detail, status):
        return secure(JSONResponse({'detail': detail}, status_code=status))

    @app.middleware('http')
    async def access(request: Request, call_next):
        if request.url.path.startswith('/api/'):
            if request.method != 'GET':
                if request.headers.get('x-stocklab-action') != '1':
                    note_rejection(request, 'missing-action-header')
                    return refused('허용되지 않은 요청입니다.', 403)
                if not origin_ok(request):
                    note_rejection(request, 'origin-mismatch')
                    return refused('허용되지 않은 요청입니다.', 403)
            if request.url.path != '/api/login' and not authenticated(request):
                return refused('로그인이 필요합니다.', 401)
        return secure(await call_next(request))

    def secure(response):
        """The same security headers on every response, the gate's own refusals included."""
        search_entry = response.headers.get(SEARCH_ENTRY_HEADER) == '1'
        if SEARCH_ENTRY_HEADER in response.headers:
            del response.headers[SEARCH_ENTRY_HEADER]
        response.headers['X-Content-Type-Options'] = 'nosniff'
        if not search_entry:
            response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Cache-Control'] = 'no-store'
        if not search_entry:
            response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.exception_handler(RuleError)
    async def invalid(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=409)

    @app.exception_handler(ProviderError)
    async def provider_error(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=503)

    class Login(BaseModel):
        password: str = Field(max_length=256)

    @app.post('/api/login')
    async def login(data: Login, request: Request, response: Response):
        remote = client_ip(request)
        attempts = failures.get(remote, [])
        attempts = [t for t in attempts if t > time.time()-300]
        if len(attempts) >= 10:
            raise HTTPException(429, '로그인 시도가 많습니다. 5분 뒤 다시 시도하세요.')
        if not secrets.compare_digest(data.password.encode(), c.password.encode()):
            if len(failures) > 1024:
                failures.clear()
            failures[remote] = attempts+[time.time()]
            raise HTTPException(401, '비밀번호가 올바르지 않습니다.')
        failures.pop(remote, None)
        now = time.time()
        token = signed(f'{int(now+SESSION_SECONDS)}:{now:.3f}:{secrets.token_urlsafe(24)}')
        response.set_cookie('stocklab_session', token, httponly=True, samesite='strict',
                            secure=browser_origin(request).startswith('https://') or on_public_origin(request),
                            max_age=SESSION_SECONDS)
        return {'ok': True}

    @app.post('/api/logout')
    def logout(response: Response):
        # Every session ends, not just this browser's cookie: a copied token stops working too.
        auth['revoked_before'] = time.time()
        with store.edit() as s:
            s['auth'] = {'revoked_before': auth['revoked_before']}
        response.delete_cookie('stocklab_session')
        return {'ok': True}

    @app.get('/health')
    def health():
        store.read()
        return {'status': 'ok'}

    @app.get('/api/state')
    def state():
        return engine.public_state()

    @app.get('/api/history/{symbol}')
    def history_chart(symbol: str):
        """A name's long history for the dashboard's charts (weekly 5 years, monthly all), from the archive's evidence pack."""
        pack = engine.evidence.get(symbol) if symbol.isalnum() and len(symbol) <= 12 else None
        if not pack:
            return {'symbol': symbol, 'series': None, 'long_term': None}
        return {'symbol': symbol, 'series': pack.get('series'), 'long_term': pack.get('long_term'), 'as_of': pack.get('as_of_bar')}

    @app.post('/api/start')
    def start():
        if c.mode == 'toss':
            if not (c.toss_id and c.toss_secret):
                raise RuleError('실제 시세 모드에는 토스 설정이 필요합니다.')
            if not c.ai_configured:
                # Say what is wrong with Gemini when it is the provider the owner asked for; otherwise point at the bridge.
                if 'gemini' in [name.strip().lower() for name in c.providers.split(',')]:
                    try:
                        c.validate_ai()
                    except ValueError as exc:
                        raise RuleError(str(exc)) from None
                raise RuleError('사용할 수 있는 AI가 없습니다. 서버 .env의 AI_BRIDGE_URL·AI_BRIDGE_TOKEN(Claude/Codex)을 확인하세요.')
        engine.start()
        return {'ok': True}

    @app.post('/api/stop')
    def stop():
        engine.stop()
        return {'ok': True}

    class Execution(BaseModel):
        mode: Literal['manual', 'auto']

    @app.post('/api/settings/execution')
    def execution(data: Execution):
        engine.set_execution(data.mode)
        return {'ok': True}

    class Experiment(BaseModel):
        seed_krw: float = Field(ge=0, le=1e12, allow_inf_nan=False, strict=True)
        seed_usd: float = Field(ge=0, le=1e9, allow_inf_nan=False, strict=True)
        name: str = Field(default='', max_length=80)
        max_order_pct: float = Field(default=30, ge=1, le=100, allow_inf_nan=False, strict=True)
        max_position_pct: float | None = Field(default=None, ge=10, le=100, allow_inf_nan=False, strict=True)
        strategy_mode: Literal['legacy', 'intraday'] = 'legacy'
        include_leveraged_etfs: bool = Field(default=False, strict=True)
        universe_mode: Literal['daily_focus', 'fixed'] = 'daily_focus'
        horizon: Literal['month', 'intraday'] = 'month'
        risk_per_trade_pct: float = Field(default=.5, ge=.1, le=2, allow_inf_nan=False, strict=True)
        daily_loss_limit_pct: float = Field(default=2, ge=1, le=10, allow_inf_nan=False, strict=True)
        max_holding_minutes: int | None = Field(default=None, ge=15, le=43200, strict=True)
        exit_mode: Literal['target', 'trail'] = 'trail'
        signal_filter: Literal['all', 'research'] = 'research'
        evidence: Literal['on', 'off'] = 'on'
        scan: Literal['focus', 'pool'] = 'pool'
        exit_profile: Literal['ai', 'market'] = 'market'
        confirmation: str

    @app.post('/api/experiments')
    def new_experiment(data: Experiment):
        if data.confirmation != '새 실험 시작':
            raise RuleError('이전 실험 보관과 새 실험 시작 확인이 필요합니다.')
        if data.strategy_mode == 'intraday' and data.horizon != 'month':
            raise RuleError('당일 단타는 제거되었습니다. 1개월 스윙으로 시작하세요.')
        result = engine.new_experiment(data.seed_krw, data.seed_usd, data.name, data.max_order_pct,
                                      strategy_mode=data.strategy_mode, strategy_settings={
                                          'include_leveraged_etfs': data.include_leveraged_etfs,
                                          'universe_mode': data.universe_mode, 'horizon': data.horizon,
                                          'exit_mode': data.exit_mode, 'signal_filter': data.signal_filter,
                                          'evidence': data.evidence, 'scan': data.scan, 'exit_profile': data.exit_profile,
                                          'max_position_pct': (data.max_position_pct if data.max_position_pct is not None
                                                               else 30),
                                          'risk_per_trade_pct': data.risk_per_trade_pct,
                                          'daily_loss_limit_pct': data.daily_loss_limit_pct,
                                          **({'max_holding_minutes': data.max_holding_minutes}
                                             if data.max_holding_minutes is not None else {})})
        return {'ok': True, 'experiment_id': result['experiment_id']}

    @app.get('/api/experiments')
    def experiments():
        return {'experiments': store.archive_list()}

    @app.get('/api/experiments/{experiment_id}/export')
    def export_experiment(experiment_id: str):
        data = store.archive_read(experiment_id)
        if data is None:
            raise HTTPException(404, '보관된 실험을 찾을 수 없습니다.')
        data.pop('daily_ai', None)
        return Response(json.dumps(data, ensure_ascii=False, indent=2), media_type='application/json',
                        headers={'Content-Disposition': 'attachment; filename=stocklab-experiment-'+data['experiment_id']+'.json'})

    class Cycle(BaseModel):
        symbol: str = Field(max_length=16)

    @app.post('/api/analyze')
    def analyze(data: Cycle):
        engine.request_cycle(data.symbol)
        return {'ok': True}

    @app.post('/api/proposals/{proposal_id}/approve')
    def approve(proposal_id: str):
        return engine.approve(proposal_id)

    @app.post('/api/proposals/{proposal_id}/reject')
    def reject(proposal_id: str):
        engine.reject(proposal_id)
        return {'ok': True}

    class Liquidation(BaseModel):
        confirmation: str

    @app.post('/api/liquidate')
    def liquidate(data: Liquidation):
        if data.confirmation != '전량 모의매도':
            raise RuleError('전량 모의매도 확인이 필요합니다.')
        engine.liquidate()
        return {'ok': True}

    @app.get('/api/export')
    def export():
        data = store.read()
        data.pop('daily_ai', None)
        return Response(json.dumps(data, ensure_ascii=False, indent=2), media_type='application/json',
                        headers={'Content-Disposition': 'attachment; filename=stocklab-ledger.json'})

    @app.get('/')
    def index():
        return FileResponse(STATIC/'index.html')

    app.mount('/static', StaticFiles(directory=STATIC), name='static')
    return app
