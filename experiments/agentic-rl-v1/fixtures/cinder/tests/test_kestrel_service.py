from cinder.kestrel_service import kestrel_pipeline


def test_kestrel_pipeline_happy_path() -> None:
    assert kestrel_pipeline(" Payload ") == "cinder:kestrel:payload"
