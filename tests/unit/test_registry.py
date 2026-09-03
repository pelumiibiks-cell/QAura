import joblib
import pytest
from sklearn.tree import DecisionTreeClassifier

from qaura.mltest.registry import ModelFormat, ModelLoadError, detect_format, load_model


@pytest.fixture
def sklearn_model_path(tmp_path):
    clf = DecisionTreeClassifier(random_state=0)
    X = [[0, 0], [1, 1], [2, 2], [3, 3]]
    y = [0, 0, 1, 1]
    clf.fit(X, y)
    path = tmp_path / "model.joblib"
    joblib.dump(clf, path)
    return path


def test_detect_format_sklearn_extensions():
    assert detect_format("model.pkl") == ModelFormat.SKLEARN_PICKLE
    assert detect_format("model.joblib") == ModelFormat.SKLEARN_PICKLE


def test_detect_format_onnx():
    assert detect_format("model.onnx") == ModelFormat.ONNX


def test_detect_format_torch():
    assert detect_format("model.pt") == ModelFormat.TORCH
    assert detect_format("model.pth") == ModelFormat.TORCH


def test_detect_format_unrecognized_extension_raises():
    with pytest.raises(ModelLoadError, match="unrecognized"):
        detect_format("model.xyz")


def test_detect_format_huggingface_directory(tmp_path):
    hf_dir = tmp_path / "my-model"
    hf_dir.mkdir()
    (hf_dir / "config.json").write_text("{}", encoding="utf-8")
    assert detect_format(hf_dir) == ModelFormat.HUGGINGFACE


def test_detect_format_directory_without_hf_markers_raises(tmp_path):
    plain_dir = tmp_path / "not-a-model"
    plain_dir.mkdir()
    with pytest.raises(ModelLoadError, match="doesn't look like"):
        detect_format(plain_dir)


def test_load_model_raises_for_nonexistent_path():
    with pytest.raises(ModelLoadError, match="does not exist"):
        load_model("/nonexistent/path/model.pkl")


def test_load_model_real_sklearn_classifier_predicts(sklearn_model_path):
    loaded = load_model(sklearn_model_path)
    assert loaded.format == ModelFormat.SKLEARN_PICKLE
    predictions = loaded.predict([[0, 0], [3, 3]])
    assert list(predictions) == [0, 1]


def test_load_model_sklearn_raw_object_is_the_real_estimator(sklearn_model_path):
    loaded = load_model(sklearn_model_path)
    assert isinstance(loaded.raw, DecisionTreeClassifier)


def test_load_model_onnx_gives_clear_error_when_onnxruntime_missing():
    # onnxruntime is genuinely not installed in this project's dependencies (by
    # design — see registry.py's module docstring), so this exercises the real
    # ImportError path, not a mocked one.
    import importlib
    if importlib.util.find_spec("onnxruntime") is not None:
        pytest.skip("onnxruntime happens to be installed in this environment")

    from pathlib import Path
    fake_path = Path("nonexistent.onnx")
    # detect_format alone doesn't need the file to exist; load_model does existence
    # checking first, so construct a temp file to get past that and reach the loader.
    fake_path.write_bytes(b"not a real onnx file")
    try:
        with pytest.raises(ModelLoadError, match="onnxruntime"):
            load_model(fake_path)
    finally:
        fake_path.unlink()


def test_load_model_torch_gives_clear_error_when_torch_missing():
    import importlib
    if importlib.util.find_spec("torch") is not None:
        pytest.skip("torch happens to be installed in this environment")

    from pathlib import Path
    fake_path = Path("nonexistent.pt")
    fake_path.write_bytes(b"not a real torch file")
    try:
        with pytest.raises(ModelLoadError, match="torch"):
            load_model(fake_path)
    finally:
        fake_path.unlink()
