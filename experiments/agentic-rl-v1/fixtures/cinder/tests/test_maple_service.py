from cinder.maple_service import maple_pipeline


def test_maple_pipeline_happy_path() -> None:
    assert maple_pipeline(" Payload ") == "cinder:maple:payload"
