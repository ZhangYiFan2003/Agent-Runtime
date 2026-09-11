from atlas.amber_service import amber_pipeline


def test_amber_pipeline_happy_path() -> None:
    assert amber_pipeline(" Payload ") == "atlas:amber:payload"
