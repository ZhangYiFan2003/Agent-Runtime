from cinder.nova_service import nova_pipeline


def test_nova_pipeline_happy_path() -> None:
    assert nova_pipeline(" Payload ") == "cinder:nova:payload"
