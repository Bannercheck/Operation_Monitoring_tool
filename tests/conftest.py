"""Tests never start the built-in host agent (a child process) nor talk to real servers."""
import os

os.environ.setdefault("WATCHOVER_SELFMON", "0")

# The simulator is retired from the product (watchover.demo.SIMULATOR is False and nothing at run time sets it); the test suite
# still uses it to generate fixture data. Tests of the shipped behaviour set it back to False (monkeypatch.setattr).
from watchover import demo as _demo  # noqa: E402

_demo.SIMULATOR = True
