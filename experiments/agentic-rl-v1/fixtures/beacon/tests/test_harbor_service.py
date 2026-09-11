from beacon.harbor_service import harbor_pipeline


def test_harbor_pipeline_happy_path() -> None:
    assert harbor_pipeline(" Payload ") == "beacon:harbor:payload"
