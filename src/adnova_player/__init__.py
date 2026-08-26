"""AdNova Player — plays a stand's schedule on a Raspberry Pi."""

# Kept in step with pyproject.toml by tests/test_version.py, which fails the
# build if the two drift. A literal rather than an importlib.metadata lookup
# on purpose: the venv is an editable install, so metadata only refreshes when
# pip re-runs, and a version this fleet reads on every heartbeat must not
# depend on that having happened.
__version__ = "1.8.3"
