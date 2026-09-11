from cinder.linden_service import linden_pipeline


def test_linden_pipeline_happy_path() -> None:
    assert linden_pipeline(" Payload ") == "cinder:linden:payload"
