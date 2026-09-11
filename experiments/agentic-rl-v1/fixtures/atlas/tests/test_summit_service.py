from atlas.summit_service import summit_pipeline


def test_summit_pipeline_happy_path() -> None:
    assert summit_pipeline(" Payload ") == "atlas:summit:payload"
