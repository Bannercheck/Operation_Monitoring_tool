"""Tests never start the built-in host agent (a child process) nor talk to real servers."""
import os

os.environ.setdefault("WATCHOVER_SELFMON", "0")
