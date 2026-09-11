from atlas.birch_service import birch_pipeline


def test_birch_pipeline_happy_path() -> None:
    assert birch_pipeline(" Payload ") == "atlas:birch:payload"
