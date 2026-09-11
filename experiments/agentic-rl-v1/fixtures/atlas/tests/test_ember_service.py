from atlas.ember_service import ember_pipeline


def test_ember_pipeline_happy_path() -> None:
    assert ember_pipeline(" Payload ") == "atlas:ember:payload"
