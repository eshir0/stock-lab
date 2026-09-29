"""ASGI entry for the design lab: the real app on the seeded demo DB, plus in-memory market context."""
from app.config import Config
from app.main import create_app
from lab_common import seed_intel

cfg = Config(database_url='sqlite:////tmp/demo.db', mode='demo', password='demo-password-12345', session_secret='x'*40,
             toss_id='', toss_secret='', gemini_key='')
application = create_app(cfg)
seed_intel(application.state.engine)
