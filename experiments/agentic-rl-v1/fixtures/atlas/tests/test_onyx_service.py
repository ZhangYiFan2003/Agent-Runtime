from atlas.onyx_service import onyx_pipeline


def test_onyx_pipeline_happy_path() -> None:
    assert onyx_pipeline(" Payload ") == "atlas:onyx:payload"
