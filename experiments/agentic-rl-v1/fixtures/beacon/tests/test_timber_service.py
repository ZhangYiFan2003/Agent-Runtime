from beacon.timber_service import timber_pipeline


def test_timber_pipeline_happy_path() -> None:
    assert timber_pipeline(" Payload ") == "beacon:timber:payload"
