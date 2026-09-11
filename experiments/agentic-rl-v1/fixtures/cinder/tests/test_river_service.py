from cinder.river_service import river_pipeline


def test_river_pipeline_happy_path() -> None:
    assert river_pipeline(" Payload ") == "cinder:river:payload"
