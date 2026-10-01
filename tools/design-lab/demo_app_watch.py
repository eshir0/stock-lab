"""demo_app with the seeded conditional entries kept alive across the app's own start-up (a start-up cancels waiting plans)."""
from demo_app import application

engine = application.state.engine
seeded = engine.store.read()['watches']
real_boot = engine.boot


def boot():
    real_boot()
    with engine.store.edit() as s:
        s['watches'] = seeded


engine.boot = boot
