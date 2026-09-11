from atlas.dune_service import dune_pipeline


def test_dune_pipeline_happy_path() -> None:
    assert dune_pipeline(" Payload ") == "atlas:dune:payload"
