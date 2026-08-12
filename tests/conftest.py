"""Shared pytest configuration."""

import logging

# Salt changes the root logger during import. Keep unit-test output predictable.
logging.root.setLevel(logging.WARNING)
