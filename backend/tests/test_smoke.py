from app.model import Observation


def test_model_importable():
    assert Observation.__name__ == "Observation"
