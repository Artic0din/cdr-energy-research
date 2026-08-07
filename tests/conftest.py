"""Pytest configuration for the catalogue-pipeline tests.

Puts ``scripts/`` on ``sys.path`` so ``build_catalogue`` (and the
``cdr_full_sweep_v2`` helpers it imports) resolve the same way they do when the
script is run directly as ``python scripts/build_catalogue.py`` (which adds the
script's own directory to ``sys.path[0]``).
"""
from __future__ import annotations

import os
import sys

_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
