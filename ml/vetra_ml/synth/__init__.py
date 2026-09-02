"""Physiology-grounded synthetic telemetry generator.

No public continuous-vitals dataset for Indian farm livestock was available for
this project, so training data is simulated from published veterinary reference
ranges plus documented disease signatures. Every constant used here is sourced
in `docs/physiology-references.md`. Swap `generator.py` for a real-data loader
and the rest of the pipeline is unchanged.
"""
