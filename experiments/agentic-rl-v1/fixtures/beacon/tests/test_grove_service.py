from beacon.grove_service import grove_pipeline


def test_grove_pipeline_happy_path() -> None:
    assert grove_pipeline(" Payload ") == "beacon:grove:payload"
