"""Order lifecycle: the states an order can be in and which moves between them are legal.

Two rules matter most. UNKNOWN is a real state, never guessed away: only evidence from the broker or an
operator's explicit decision can leave it. And nothing new is sent while any order is UNKNOWN.
"""
PREVIEWED = 'PREVIEWED'
SUBMITTING = 'SUBMITTING'
ACCEPTED = 'ACCEPTED'
PARTIALLY_FILLED = 'PARTIALLY_FILLED'
FILLED = 'FILLED'
CANCEL_PENDING = 'CANCEL_PENDING'
CANCELED = 'CANCELED'
REJECTED = 'REJECTED'
UNKNOWN = 'UNKNOWN'
NOT_PLACED = 'NOT_PLACED'

TERMINAL = frozenset({FILLED, CANCELED, REJECTED, NOT_PLACED})
ACTIVE = frozenset({SUBMITTING, ACCEPTED, PARTIALLY_FILLED, CANCEL_PENDING})
# States that may still change a position or hold a claim on limits.
LIVE_EXPOSURE = ACTIVE | {UNKNOWN}

TRANSITIONS = {
    SUBMITTING: {ACCEPTED, REJECTED, UNKNOWN},
    UNKNOWN: {ACCEPTED, PARTIALLY_FILLED, FILLED, CANCELED, REJECTED, NOT_PLACED, UNKNOWN},
    ACCEPTED: {PARTIALLY_FILLED, FILLED, CANCEL_PENDING, CANCELED, REJECTED},
    PARTIALLY_FILLED: {PARTIALLY_FILLED, FILLED, CANCEL_PENDING, CANCELED},
    CANCEL_PENDING: {ACCEPTED, PARTIALLY_FILLED, FILLED, CANCELED},
}

# Broker order-status strings. CANCELED / REJECTED may still carry a partial fill: read the filled quantity.
_BROKER_STATES = {'PENDING': ACCEPTED, 'PENDING_REPLACE': ACCEPTED, 'PARTIAL_FILLED': PARTIALLY_FILLED,
                  'FILLED': FILLED, 'PENDING_CANCEL': CANCEL_PENDING, 'CANCELED': CANCELED, 'REJECTED': REJECTED}


def can_transition(old, new):
    return new in TRANSITIONS.get(old, ())


def map_broker_status(raw):
    """Anything unrecognised (a new broker status, a replace/cancel-reject record) stays UNKNOWN: never guess."""
    return _BROKER_STATES.get(raw, UNKNOWN)
