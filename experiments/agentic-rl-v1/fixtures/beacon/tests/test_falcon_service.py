from beacon.falcon_service import falcon_pipeline


def test_falcon_pipeline_happy_path() -> None:
    assert falcon_pipeline(" Payload ") == "beacon:falcon:payload"
