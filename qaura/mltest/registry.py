"""Model loaders for the ML testing suites (Phase 6). scikit-learn/joblib are fully
supported and exercised in tests — they're already project dependencies (pyproject.toml's
`ml` extra). torch, onnx, and Hugging Face transformers are supported via LAZY, OPTIONAL
imports rather than hard dependencies: those are multi-gigabyte installs, and requiring
them for every QAura install just to test a scikit-learn classifier would be a poor
default. If a user actually needs one, `load_model()` gives a clear "pip install X to
use this" error rather than an ImportError at module import time or a silent failure.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

_log = logging.getLogger(__name__)


class ModelFormat(str, Enum):
    SKLEARN_PICKLE = "sklearn_pickle"   # .pkl / .joblib via joblib.load
    ONNX = "onnx"
    TORCH = "torch"
    HUGGINGFACE = "huggingface"          # a saved_pretrained() directory


class ModelLoadError(RuntimeError):
    pass


@dataclass
class LoadedModel:
    format: ModelFormat
    path: str
    predict: Callable[[Any], Any]        # unified callable: raw input -> raw output
    raw: Any                             # the underlying model object, for suite-specific use


_EXTENSION_TO_FORMAT = {
    ".pkl": ModelFormat.SKLEARN_PICKLE,
    ".joblib": ModelFormat.SKLEARN_PICKLE,
    ".onnx": ModelFormat.ONNX,
    ".pt": ModelFormat.TORCH,
    ".pth": ModelFormat.TORCH,
}


def detect_format(path: str | Path) -> ModelFormat:
    p = Path(path)
    if p.is_dir():
        # A Hugging Face save_pretrained() directory has one of these; a bare
        # sniff is enough here, we're not validating the whole directory layout.
        if any((p / name).exists() for name in ("config.json", "pytorch_model.bin", "model.safetensors")):
            return ModelFormat.HUGGINGFACE
        raise ModelLoadError(f"{path}: directory given but doesn't look like a Hugging Face model save")
    fmt = _EXTENSION_TO_FORMAT.get(p.suffix.lower())
    if fmt is None:
        raise ModelLoadError(f"{path}: unrecognized model file extension {p.suffix!r}")
    return fmt


def _load_sklearn(path: Path) -> LoadedModel:
    try:
        import joblib
    except ImportError as e:
        raise ModelLoadError("scikit-learn support needs the 'ml' extra: pip install -e \".[ml]\"") from e

    _log.warning(
        "Loading %s via joblib.load() — this deserializes an arbitrary Python "
        "pickle and can execute code embedded in the file. Only run `qaura ml test` "
        "against model artifacts you trust.",
        path,
    )
    model = joblib.load(path)
    if hasattr(model, "predict"):
        predict = model.predict
    elif callable(model):
        predict = model
    else:
        raise ModelLoadError(f"{path}: loaded object has no .predict() and isn't callable")
    return LoadedModel(format=ModelFormat.SKLEARN_PICKLE, path=str(path), predict=predict, raw=model)


def _load_onnx(path: Path) -> LoadedModel:
    try:
        import onnxruntime as ort
    except ImportError as e:
        raise ModelLoadError("ONNX support needs onnxruntime: pip install onnxruntime") from e

    session = ort.InferenceSession(str(path))
    input_name = session.get_inputs()[0].name
    output_names = [o.name for o in session.get_outputs()]

    def predict(x):
        result = session.run(output_names, {input_name: x})
        return result[0] if len(result) == 1 else result

    return LoadedModel(format=ModelFormat.ONNX, path=str(path), predict=predict, raw=session)


def _load_torch(path: Path) -> LoadedModel:
    try:
        import torch
    except ImportError as e:
        raise ModelLoadError("PyTorch support needs torch: pip install torch") from e

    _log.warning(
        "Loading %s via torch.load(weights_only=False) — this explicitly opts out "
        "of PyTorch's default pickle-deserialization protection and can execute "
        "code embedded in the file. Only run `qaura ml test` against model "
        "artifacts you trust.",
        path,
    )
    model = torch.load(path, map_location="cpu", weights_only=False)
    if hasattr(model, "eval"):
        model.eval()

    def predict(x):
        with torch.no_grad():
            inp = x if isinstance(x, torch.Tensor) else torch.as_tensor(x)
            return model(inp)

    return LoadedModel(format=ModelFormat.TORCH, path=str(path), predict=predict, raw=model)


def _load_huggingface(path: Path) -> LoadedModel:
    try:
        from transformers import AutoModel, AutoTokenizer
    except ImportError as e:
        raise ModelLoadError("Hugging Face support needs transformers: pip install transformers") from e

    tokenizer = AutoTokenizer.from_pretrained(str(path))
    model = AutoModel.from_pretrained(str(path))
    model.eval()

    def predict(text: str):
        inputs = tokenizer(text, return_tensors="pt")
        return model(**inputs)

    return LoadedModel(format=ModelFormat.HUGGINGFACE, path=str(path), predict=predict, raw=(model, tokenizer))


_LOADERS: dict[ModelFormat, Callable[[Path], LoadedModel]] = {
    ModelFormat.SKLEARN_PICKLE: _load_sklearn,
    ModelFormat.ONNX: _load_onnx,
    ModelFormat.TORCH: _load_torch,
    ModelFormat.HUGGINGFACE: _load_huggingface,
}


def load_model(path: str | Path, format: ModelFormat | None = None) -> LoadedModel:
    path = Path(path)
    if not path.exists():
        raise ModelLoadError(f"{path}: does not exist")
    fmt = format or detect_format(path)
    return _LOADERS[fmt](path)
