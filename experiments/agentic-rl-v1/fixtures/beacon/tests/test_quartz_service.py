from beacon.quartz_service import quartz_pipeline


def test_quartz_pipeline_happy_path() -> None:
    assert quartz_pipeline(" Payload ") == "beacon:quartz:payload"
