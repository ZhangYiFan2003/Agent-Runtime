from beacon.juniper_service import juniper_pipeline


def test_juniper_pipeline_happy_path() -> None:
    assert juniper_pipeline(" Payload ") == "beacon:juniper:payload"
