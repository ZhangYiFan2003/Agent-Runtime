from beacon.umber_service import umber_pipeline


def test_umber_pipeline_happy_path() -> None:
    assert umber_pipeline(" Payload ") == "beacon:umber:payload"
