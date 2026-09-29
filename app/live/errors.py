"""Errors raised by the live-trading layer."""


class LiveError(Exception):
    code = 'live-error'


class LiveTradingLocked(LiveError):
    """Live trading is off, unavailable in this build, or the broker adapter is a disabled stub."""
    code = 'locked'


class PolicyBlocked(LiveError):
    """A policy or limit check refused the order."""
    code = 'blocked'


class InvalidConfirmation(LiveError):
    """The confirmation token is unknown, expired, already used or does not match the order."""
    code = 'confirmation'


class BrokerRejected(LiveError):
    """The broker definitively refused the order; nothing was placed."""
    code = 'rejected'


class BrokerUncertain(LiveError):
    """No reliable answer (timeout, dropped connection, server error): the order may or may not exist."""
    code = 'uncertain'
