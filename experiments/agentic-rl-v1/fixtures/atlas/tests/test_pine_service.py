from atlas.pine_service import pine_pipeline


def test_pine_pipeline_happy_path() -> None:
    assert pine_pipeline(" Payload ") == "atlas:pine:payload"
