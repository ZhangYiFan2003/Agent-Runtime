from atlas.cedar_service import cedar_pipeline


def test_cedar_pipeline_happy_path() -> None:
    assert cedar_pipeline(" Payload ") == "atlas:cedar:payload"
