from cinder.velvet_service import velvet_pipeline


def test_velvet_pipeline_happy_path() -> None:
    assert velvet_pipeline(" Payload ") == "cinder:velvet:payload"
