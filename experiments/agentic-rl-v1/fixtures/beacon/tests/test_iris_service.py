from beacon.iris_service import iris_pipeline


def test_iris_pipeline_happy_path() -> None:
    assert iris_pipeline(" Payload ") == "beacon:iris:payload"
