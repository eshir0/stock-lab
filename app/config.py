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
    interval_seconds: int = max(60, int(os.getenv('ANALYSIS_INTERVAL_SECONDS', '1200')))
    # Day trading looks more often than a month plan: every 10 minutes, and every 5 while a position is open or a proposal
    # waits (a quick second look pays most then). Set both to the same value for a fixed interval.
    # Daily focus list: names per market the intraday desk may buy, and whether the AI reads the news for it.
    focus_per_market: int = max(1, min(6, int(os.getenv('FOCUS_PER_MARKET', '3'))))
    focus_ai: bool = os.getenv('FOCUS_AI', 'on').strip().lower() not in ('off', 'false', '0', 'no')
    # Fractional US shares (four decimals, at least about a dollar per order). A simulation assumption; Korean orders stay whole shares.
    fractional_us: bool = os.getenv('FRACTIONAL_US', 'on').strip().lower() not in ('off', 'false', '0', 'no')
    # Spread the AI window's budget over the session (see pacing.py): the interval above is the minimum, pacing only stretches it.
    quota_pacing: bool = os.getenv('QUOTA_PACING', 'on').strip().lower() not in ('off', 'false', '0', 'no')
    pace_max_seconds: int = max(60, int(os.getenv('PACE_MAX_SECONDS', '4500')))
    # Conditional entries: the director may leave a price plan with a HOLD, which the server watches without any AI call and trades
    # through the ordinary risk rules when it comes true (see entry.py). Off keeps the plans from being stored at all.
    conditional_entry: bool = os.getenv('CONDITIONAL_ENTRY', 'on').strip().lower() not in ('off', 'false', '0', 'no')
    # Research reuse: a second analysis of the same name within this many seconds reuses the first one's planner, company and news
    # research and only reads the tape, the objections and the decision again (3 AI calls instead of 6). 0 turns it off. The research
    # is looked up afresh when the price has moved this many percent since it was made (see reuse.py).
    # A buy is refused when its take-profit is narrower than this many times the round-trip cost (fees, slippage, Korean sell tax and the
  # quoted spread): a target that small is eaten by the costs. 0 turns the rule off (see risk.size_order).
    min_take_cost_ratio: float = max(0.0, float(os.getenv('MIN_TAKE_COST_RATIO', '3')))
    # Model tiering: these roles ask the bridge for its lighter model (CLAUDE_MODEL_LIGHT / CODEX_MODEL_LIGHT, when set); the
    # selector, the critic, the director and the morning brief keep the main one. Research summarises sources while the decision
    # keeps the strongest model, and an analysis costs a smaller share of the subscription. Empty runs every role on the main model.
    ai_light_roles: tuple = tuple(r.strip() for r in os.getenv('AI_LIGHT_ROLES', 'planner,fundamental,technical,news').split(',')
                                  if r.strip())
    # The bridge's models, read here only so the verification fingerprint notices a change of model.
    claude_model: str = os.getenv('CLAUDE_MODEL', '')
    claude_model_light: str = os.getenv('CLAUDE_MODEL_LIGHT', '')
    codex_model: str = os.getenv('CODEX_MODEL', '')
    # Phone notifications (notify.py): an ntfy topic URL or any endpoint taking a plain-text POST. Empty sends nothing.
    notify_url: str = os.getenv('NOTIFY_URL', '')
    # Folder of daily evidence packs from the history archive (tools/data/evidence.py), mounted read-only. Empty = none.
    evidence_dir: str = os.getenv('EVIDENCE_DIR', '')
    research_reuse_seconds: int = max(0, int(os.getenv('RESEARCH_REUSE_SECONDS', '3600')))
    research_reuse_move_pct: float = max(.1, float(os.getenv('RESEARCH_REUSE_MOVE_PCT', '1.5')))
    proposal_seconds: int = 180
    quote_age: int = 30
    # Costs follow the real 2026 schedule (Toss Securities): domestic commission 0.015% a side, US commission 0.1% a side (an
    # order of $10 or less is free), and Korea's securities transaction tax of 0.20% on selling a listed STOCK (ETFs are exempt,
    # see risk.trade_fee). Slippage is an assumption on top of crossing the quoted spread.
    fee_kr: float = float(os.getenv('FEE_KR_BPS', '1.5'))
    fee_us: float = float(os.getenv('FEE_US_BPS', '10'))
    sell_tax_kr: float = float(os.getenv('SELL_TAX_KR_BPS', '20'))
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
        if not math.isfinite(self.min_take_cost_ratio) or not 0 <= self.min_take_cost_ratio <= 20:
            raise ValueError('MIN_TAKE_COST_RATIO must be between 0 and 20')


INSTRUMENTS = [
    {'symbol': '005930', 'name': '삼성전자', 'market': 'KR', 'currency': 'KRW', 'demo_base': 70000},
    {'symbol': '000660', 'name': 'SK하이닉스', 'market': 'KR', 'currency': 'KRW', 'demo_base': 180000},
    {'symbol': 'AAPL', 'name': 'Apple', 'market': 'US', 'currency': 'USD', 'demo_base': 200},
    {'symbol': 'MSFT', 'name': 'Microsoft', 'market': 'US', 'currency': 'USD', 'demo_base': 400},
]
SYMBOLS = {i['symbol']: i for i in INSTRUMENTS}
ROLES = [('fundamental', '기업 분석가'), ('technical', '차트 분석가'), ('news', '뉴스 분석가'),
         ('critic', '반대 검토자'), ('director', '디렉터')]
