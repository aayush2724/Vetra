"""The streaming engine is fed by several threads and by collars that resend
their buffers, so it must count every minute once and never race itself."""
from __future__ import annotations

import threading

from vetra_ml.config import SAMPLES_PER_WINDOW, WINDOW_STRIDE_MINUTES


def _rows(herd, animal_id, n=None):
    animal = herd[herd.animal_id == animal_id].sort_values("timestamp").reset_index(drop=True)
    rows = animal.assign(timestamp=animal.timestamp.astype(str)).to_dict("records")
    return rows if n is None else rows[:n]


def test_a_retransmitted_buffer_is_counted_once(herd, stub_engine):
    """A collar that flushes the same hour twice must not produce extra windows."""
    rows = _rows(herd, herd.animal_id.iloc[0], 120)
    first = stub_engine.ingest_many(rows)
    again = stub_engine.ingest_many(rows[60:])
    assert len(first) == 1 + (120 - SAMPLES_PER_WINDOW) // WINDOW_STRIDE_MINUTES
    assert again == []
    assert stub_engine.ignored_samples == 60


def test_out_of_order_readings_are_ignored(herd, stub_engine):
    rows = _rows(herd, herd.animal_id.iloc[0], 91)
    stub_engine.ingest_many(rows[:90])
    assert stub_engine.ingest(rows[10]) is None
    assert stub_engine.ignored_samples == 1
    # A genuinely newer reading is still accepted.
    stub_engine.ingest(rows[90])
    assert stub_engine.ignored_samples == 1


def test_concurrent_animals_do_not_lose_samples(herd, stub_engine):
    """Several HTTP handler threads and the MQTT thread share one engine."""
    animals = list(herd.animal_id.unique())[:4]
    per_animal = {a: _rows(herd, a, 300) for a in animals}
    results = {a: [] for a in animals}

    def feed(animal_id):
        results[animal_id] = stub_engine.ingest_many(per_animal[animal_id])

    threads = [threading.Thread(target=feed, args=(a,)) for a in animals]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    expected = 1 + (300 - SAMPLES_PER_WINDOW) // WINDOW_STRIDE_MINUTES
    assert all(len(results[a]) == expected for a in animals)
    assert stub_engine.tracked_animals == len(animals)
    assert stub_engine.ignored_samples == 0
