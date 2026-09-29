"""The live-trading master lock and its settings.

This build contains NO live order adapter. ``LIVE_TRADING_BUILD_ENABLED`` is a code constant, not a
setting: the environment can *ask* for live trading (the request is shown as ignored) but can never turn
it on. Enabling it takes a reviewed code change that ships a real broker adapter, updates the guard tests
in tests/test_no_live_trading.py and flips this constant.
"""
import os
from dataclasses import dataclass, field

from .limits import LiveLimits

LIVE_TRADING_BUILD_ENABLED = False

REASON_OFF = '실거래 꺼짐(기본). 모의투자만 동작합니다.'
REASON_LOCKED = '.env에 LIVE_TRADING이 켜져 있지만 이 빌드에는 실거래 주문 어댑터가 없어 무시합니다. 모의투자만 동작합니다.'
REASON_ON = '실거래 켜짐'


def flag(value, default=False):
    if value is None or str(value).strip() == '':
        return default
    return str(value).strip().lower() in ('1', 'true', 'on', 'yes', 'y')


@dataclass(frozen=True)
class LiveConfig:
    requested: bool = False      # the environment asked for live trading
    enabled: bool = False        # effective: requested AND this build allows it (never true today)
    allow_buy: bool = False      # separate permission for buying
    allow_sell: bool = False     # separate permission for selling
    shadow: bool = True          # record the live orders the AI would have sent (never sent)
    limits: LiveLimits = field(default_factory=LiveLimits.default)
    reason: str = REASON_OFF

    @classmethod
    def from_env(cls, env=None, build_enabled=None):
        env = os.environ if env is None else env
        build = LIVE_TRADING_BUILD_ENABLED if build_enabled is None else build_enabled
        requested = flag(env.get('LIVE_TRADING'))
        enabled = bool(requested and build)
        return cls(requested=requested, enabled=enabled,
                   allow_buy=flag(env.get('LIVE_ALLOW_BUY')), allow_sell=flag(env.get('LIVE_ALLOW_SELL')),
                   shadow=flag(env.get('SHADOW_MODE'), True), limits=LiveLimits.from_env(env),
                   reason=REASON_ON if enabled else REASON_LOCKED if requested else REASON_OFF)

    def public(self):
        return {'requested': self.requested, 'enabled': self.enabled, 'locked': not self.enabled,
                'allow_buy': self.allow_buy, 'allow_sell': self.allow_sell, 'shadow': self.shadow,
                'reason': self.reason, 'limits': self.limits.public()}
