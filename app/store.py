import copy
import threading
import time
import uuid
from contextlib import contextmanager

from sqlalchemy import Column, Float, Integer, JSON, MetaData, String, Table, create_engine, select, text, update

from .performance import performance_summary, record_performance


def initial(mode, seed_krw=10000000.0, seed_usd=10000.0, name='첫 실험'):
    now = time.time()
    state = {'mode': mode, 'running': False, 'resume': False, 'liquidating': False, 'generation': 0, 'revision': 0,
            'execution_mode': 'manual', 'max_order_ratio': .10,
            'strategy_mode': 'legacy', 'strategy_settings': {}, 'risk_days': {},
            'experiment_id': str(uuid.uuid4()), 'experiment_name': name, 'started_at': now,
            'cash': {'KRW': seed_krw, 'USD': seed_usd}, 'initial': {'KRW': seed_krw, 'USD': seed_usd},
            'positions': {}, 'quotes': {}, 'proposals': [], 'trades': [], 'events': [], 'runs': [],
            'daily_ai': {}, 'last_error': '', 'next_run': 0, 'cycle': 0, 'history': [], 'cursor': 0,
            'focus': {}, 'focus_history': []}
    record_performance(state, now=now, force=True)
    return state


def ensure_defaults(state):
    """Add experiment metadata without resetting existing balances or activity."""
    now = time.time()
    state.setdefault('execution_mode', 'manual')
    state.setdefault('max_order_ratio', .10)
    state.setdefault('strategy_mode', 'legacy')
    state.setdefault('strategy_settings', {})
    state.setdefault('risk_days', {})
    state.setdefault('focus', {})
    state.setdefault('focus_history', [])
    state.setdefault('experiment_id', str(uuid.uuid4()))
    state.setdefault('experiment_name', '첫 실험')
    state.setdefault('started_at', min([item['time'] for item in state.get('history', [])] or [now]))
    if 'performance' not in state:
        # Older ledgers kept only 1,000 samples. Their all-time drawdown cannot be
        # reconstructed honestly, so measurement starts at migration.
        state['performance'] = {'measurement_started_at': now}
        record_performance(state, now=now)
    return state


class Store:
    """One-account JSON ledger. PG row lock serializes cash, fills and control state atomically."""
    def __init__(self, url, mode):
        self.mode = mode
        self.sqlite = url.startswith('sqlite')
        self.engine = create_engine(url, pool_pre_ping=True,
                                    connect_args={'check_same_thread': False, 'timeout': 30} if self.sqlite else {})
        self.mutex = threading.RLock()
        self.metadata = MetaData()
        self.table = Table('stocklab_state', self.metadata,
                           Column('namespace', String(20), primary_key=True),
                           Column('schema_version', Integer, nullable=False), Column('payload', JSON, nullable=False))
        self.experiments = Table('stocklab_experiments', self.metadata,
                                 Column('id', String(36), primary_key=True),
                                 Column('mode', String(20), nullable=False),
                                 Column('created', Float, nullable=False),
                                 Column('label', String(100), nullable=False),
                                 Column('payload', JSON, nullable=False))
        self.metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            if not conn.execute(select(self.table).where(self.table.c.namespace == mode)).first():
                conn.execute(self.table.insert().values(namespace=mode, schema_version=2, payload=initial(mode)))
        self.leader = None
        self.leader_pid = None
        self.leader_mutex = threading.Lock()
        with self.edit():
            pass  # Persist migration once so reads return stable experiment IDs.

    def claim_process(self):
        if not self.sqlite:
            self.leader = self.engine.connect()
            if not self.leader.execute(text('SELECT pg_try_advisory_lock(734029148)')).scalar():
                self.leader.close()
                self.leader = None
                raise RuntimeError('Only one Stock Lab process may run against this database')
            self.leader_pid = self.leader.execute(text('SELECT pg_backend_pid()')).scalar_one()
            self.leader.commit()

    def check_owner(self):
        if self.leader is not None:
            with self.leader_mutex:
                pid = self.leader.execute(text('SELECT pg_backend_pid()')).scalar_one()
                self.leader.commit()
                if pid != self.leader_pid:
                    raise RuntimeError('Database execution lock was lost; restart the app before trading')

    def release(self):
        if self.leader is not None:
            self.leader.execute(text('SELECT pg_advisory_unlock(734029148)'))
            self.leader.close()
            self.leader = None
        self.engine.dispose()

    @contextmanager
    def _transaction(self):
        self.check_owner()
        with self.mutex, self.engine.connect() as conn:
            if self.sqlite:
                conn.exec_driver_sql('BEGIN IMMEDIATE')
            else:
                conn.begin()
            try:
                row = conn.execute(select(self.table.c.payload).where(
                    self.table.c.namespace == self.mode).with_for_update()).one()
                state = ensure_defaults(copy.deepcopy(row[0]))
                yield conn, state
                state['events'] = state['events'][-200:]
                state['runs'] = state['runs'][-40:]
                state['history'] = state['history'][-10000:]
                state['proposals'] = state['proposals'][-300:]
                conn.execute(update(self.table).where(self.table.c.namespace == self.mode).values(schema_version=2, payload=state))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @contextmanager
    def edit(self):
        with self._transaction() as (_, state):
            yield state

    def reset_experiment(self, seed_krw, seed_usd, name, max_order_ratio=.30, strategy_mode='legacy', strategy_settings=None):
        with self._transaction() as (conn, state):
            if state['running'] or state['liquidating']:
                raise ValueError('진행 중인 실행을 먼저 중지하세요.')
            now = time.time()
            archived = copy.deepcopy(state)
            archived['ended_at'] = now
            record_performance(archived, now=now, force=True)
            conn.execute(self.experiments.insert().values(
                id=state['experiment_id'], mode=self.mode, created=now,
                label=state['experiment_name'], payload=archived))
            new = initial(self.mode, seed_krw, seed_usd, name)
            new['max_order_ratio'] = max_order_ratio
            new['strategy_mode'] = strategy_mode
            new['strategy_settings'] = copy.deepcopy(strategy_settings or {})
            new['daily_ai'] = copy.deepcopy(state['daily_ai'])
            new['quotes'] = copy.deepcopy(state['quotes'])
            new['generation'] = state['generation']+1
            new['revision'] = state['revision']+1
            event(new, '이전 실험을 보관하고 새 가상 시드머니로 시작했습니다. 중지 상태입니다.')
            state.clear()
            state.update(new)
            return copy.deepcopy(state)

    def archive_list(self):
        self.check_owner()
        with self.engine.connect() as conn:
            rows = conn.execute(select(self.experiments).where(
                self.experiments.c.mode == self.mode).order_by(self.experiments.c.created.desc()).limit(100)).mappings().all()
        return [{'id': row['id'], 'experiment_id': row['id'], 'name': row['label'],
                 'experiment_name': row['label'], 'mode': row['mode'],
                 'started_at': row['payload']['started_at'], 'ended_at': row['created'],
                 'execution_mode': row['payload']['execution_mode'],
                 'trades_count': len(row['payload']['trades']),
                 'performance': performance_summary(row['payload'], now=row['created'])} for row in rows]

    def archive_read(self, experiment_id):
        self.check_owner()
        with self.engine.connect() as conn:
            payload = conn.execute(select(self.experiments.c.payload).where(
                self.experiments.c.id == experiment_id,
                self.experiments.c.mode == self.mode)).scalar_one_or_none()
        return copy.deepcopy(payload) if payload is not None else None

    def read(self):
        self.check_owner()
        with self.engine.connect() as conn:
            return copy.deepcopy(conn.execute(select(self.table.c.payload).where(
                self.table.c.namespace == self.mode)).scalar_one())


def event(s, message, level='info'):
    s['events'].append({'time': time.time(), 'message': message, 'level': level})
