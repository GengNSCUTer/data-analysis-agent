# Synthetic Fixtures

`sales_daily.csv` is a tiny, deterministic fixture for local smoke tests. It is not
third-party data and does not represent a real business. The Vanna Phase 1 launcher
currently creates the equivalent SQLite table at runtime; later iterations may load
this CSV instead so that the fixture has one canonical source.

The fixture is intentionally small enough to commit. It must not be used to claim
business accuracy, model accuracy, or production performance.
