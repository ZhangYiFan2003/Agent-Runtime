from cinder.willow_service import willow_pipeline


def test_willow_pipeline_happy_path() -> None:
    assert willow_pipeline(" Payload ") == "cinder:willow:payload"
