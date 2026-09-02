"""Shared fixtures. Adds the two source roots to the path so tests import the
same modules the running system does, without an install step."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT / "ml", ROOT / "edge"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))


@pytest.fixture
def store(tmp_path):
    from store import EdgeStore

    s = EdgeStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture(scope="session")
def herd():
    """A small deterministic herd, generated once and shared across tests."""
    from vetra_ml.synth.generator import generate_dataset

    return generate_dataset(n_animals=8, days=2, seed=99)
