import math
import os
import re
from dataclasses import dataclass, field


def gemini_model_id(value):
    """Normalize a configured REST model name and reject invalid path segments."""
    model = value.strip().removeprefix('models/') if isinstance(value, str) else ''
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', model):
        raise ValueError('GEMINI_MODEL 값이 올바른 모델 ID 형식이 아닙니다. 예: gemini-2.5-flash')
    return model


from .live.lock import LiveConfig


@dataclass
class Config:
    database_url: str = os.getenv('DATABASE_URL', 'sqlite:///stocklab.db')
    mode: str = os.getenv('MARKET_MODE', 'demo')
    password: str = os.getenv('APP_PASSWORD', '')
    session_secret: str = os.getenv('SESSION_SECRET', '')
    toss_id: str = os.getenv('TOSS_CLIENT_ID', '')
    toss_secret: str = os.getenv('TOSS_CLIENT_SECRET', '')
    gemini_key: str = os.getenv('GEMINI_API_KEY', '')
    # Default model ID; verify access, quotas and billing for the configured project.
    model: str = os.getenv('GEMINI_MODEL', '') or 'gemini-2.5-flash'
    # Gemini 3.x thinking level (low/medium/high); empty keeps the model default.
    gemini_thinking: str = os.getenv('GEMINI_THINKING', '').strip().lower()
    # Free-tier Gemini 3.x keys get 429 for Google Search; off skips search roles without spending a call.
    gemini_search: bool = os.getenv('GEMINI_SEARCH', 'on').strip().lower() not in ('off', 'false', '0', 'no')
    # Host CLI bridge (claude -p / codex exec); Gemini is only the last fallback.
    bridge_url: str = os.getenv('AI_BRIDGE_URL', '')
    bridge_token: str = os.getenv('AI_BRIDGE_TOKEN', '')
    providers: str = os.getenv('AI_PROVIDERS', 'claude,codex')
    # Percent of a subscription window (5-hour or weekly) at which a NEW analysis cycle moves to the next AI.
    ai_switch_pct: float = max(10.0, min(99.0, float(os.getenv('AI_SWITCH_AT_PCT', '80'))))
    # Live-trading readiness: locked in this build; the environment can request it but never enable it.
    live: LiveConfig = field(default_factory=LiveConfig.from_env)
    ai_daily_calls: int = int(os.getenv('AI_DAILY_CALL_LIMIT', '30'))
    poll_seconds: int = max(5, int(os.getenv('QUOTE_POLL_SECONDS', '10')))
    # Parallel Toss order-book reads (each symbol needs its own call); clamped to 1-8.
    toss_parallel: int = max(1, min(8, int(os.getenv('TOSS_PARALLEL', '4'))))
    interval_seconds: int = max(60, int(os.getenv('ANALYSIS_INTERVAL_SECONDS', '900')))
    # Daily focus list: names per market the intraday desk may buy, and whether the AI reads the news for it.
    focus_per_market: int = max(1, min(6, int(os.getenv('FOCUS_PER_MARKET', '3'))))
    focus_ai: bool = os.getenv('FOCUS_AI', 'on').strip().lower() not in ('off', 'false', '0', 'no')
    proposal_seconds: int = 180
    quote_age: int = 30
    fee_kr: float = float(os.getenv('FEE_KR_BPS', '15'))
    fee_us: float = float(os.getenv('FEE_US_BPS', '15'))
    sell_tax_kr: float = float(os.getenv('SELL_TAX_KR_BPS', '0'))
    slippage_bps: float = float(os.getenv('SLIPPAGE_BPS', '5'))

    def validate_ai(self):
        if not isinstance(self.gemini_key, str) or not self.gemini_key.strip():
            raise ValueError('서버 .env에 GEMINI_API_KEY를 설정하세요.')
        return gemini_model_id(self.model)

    @property
    def bridge_configured(self):
        return bool(self.bridge_url.strip() and len(self.bridge_token.strip()) >= 32)

    @property
    def provider_order(self):
        """Configured order, keeping only providers that can actually be called."""
        order = []
        for name in (x.strip().lower() for x in self.providers.split(',')):
            if name in ('claude', 'codex') and self.bridge_configured or name == 'gemini' and self.gemini_configured:
                if name not in order:
                    order.append(name)
        return order

    @property
    def gemini_configured(self):
        try:
            self.validate_ai()
            return True
        except ValueError:
            return False

    @property
    def ai_configured(self):
        return bool(self.provider_order)

    @property
    def gemini_only(self):
        """The daily call limit protects the Gemini quota; it gates cycles only when nothing else runs."""
        return self.provider_order == ['gemini']

    def validate(self):
        if self.mode not in ('demo', 'toss'):
            raise ValueError('MARKET_MODE must be demo or toss')
        if len(self.password) < 8 or len(self.session_secret) < 32:
            raise ValueError('APP_PASSWORD (8+) and SESSION_SECRET (32+) must be configured')
        if any(not math.isfinite(x) or x < 0 or x > 1000 for x in [self.fee_kr, self.fee_us, self.sell_tax_kr, self.slippage_bps]):
            raise ValueError('Simulation cost parameters must be between 0 and 1000 bps')


INSTRUMENTS = [
    {'symbol': '005930', 'name': '삼성전자', 'market': 'KR', 'currency': 'KRW', 'demo_base': 70000},
    {'symbol': '000660', 'name': 'SK하이닉스', 'market': 'KR', 'currency': 'KRW', 'demo_base': 180000},
    {'symbol': 'AAPL', 'name': 'Apple', 'market': 'US', 'currency': 'USD', 'demo_base': 200},
    {'symbol': 'MSFT', 'name': 'Microsoft', 'market': 'US', 'currency': 'USD', 'demo_base': 400},
]
SYMBOLS = {i['symbol']: i for i in INSTRUMENTS}
ROLES = [('fundamental', '기업 분석가'), ('technical', '차트 분석가'), ('news', '뉴스 분석가'),
         ('critic', '반대 검토자'), ('director', '디렉터')]
