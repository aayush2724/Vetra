"""One place that knows how to load a .tflite file.

The edge device should not need TensorFlow installed — `ai-edge-litert` is a
few megabytes against TensorFlow's several hundred. This module prefers it and
falls back to `tf.lite` only on a development machine where TF happens to be
present, so the same code runs in both places.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


class InterpreterUnavailable(RuntimeError):
    """Neither ai-edge-litert nor TensorFlow could provide an interpreter."""


def load_interpreter(model: Path | bytes) -> Any:
    """Return an allocated TFLite interpreter for a path or an in-memory model."""
    kwargs: dict[str, Any] = (
        {"model_path": str(model)} if isinstance(model, (str, Path)) else {"model_content": model}
    )

    try:
        from ai_edge_litert.interpreter import Interpreter  # type: ignore
    except ImportError:
        try:
            from tensorflow.lite import Interpreter  # type: ignore
        except ImportError as exc:
            raise InterpreterUnavailable(
                "install `ai-edge-litert` (recommended for edge devices) or `tensorflow`"
            ) from exc

    interpreter = Interpreter(**kwargs)
    interpreter.allocate_tensors()
    return interpreter
