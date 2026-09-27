from __future__ import annotations

import os
from uuid import uuid4

import pytest

from axiom.artifacts import S3ArtifactBlobStore


@pytest.mark.minio
def test_real_minio_upload_download_and_dedupe() -> None:
    if os.environ.get("AXIOM_TEST_MINIO") != "1":
        pytest.skip("AXIOM_TEST_MINIO is not enabled")
    minio = pytest.importorskip("minio")
    bucket = f"axiom-test-{uuid4().hex}"
    client = minio.Minio(
        "127.0.0.1:9000",
        access_key="axiom-test",
        secret_key="axiom-test-secret",
        secure=False,
    )
    client.make_bucket(bucket)
    try:
        store = S3ArtifactBlobStore(
            endpoint="http://127.0.0.1:9000",
            bucket=bucket,
            access_key="axiom-test",
            secret_key="axiom-test-secret",
            secure=False,
            client=client,
        )
        first = store.put_bytes(b"minio-artifact", max_bytes=1024)
        second = store.put_bytes(b"minio-artifact", max_bytes=1024)
        assert not first.reused
        assert second.reused
        assert b"".join(store.iter_bytes(first.blob.storage_key)) == b"minio-artifact"
    finally:
        for item in client.list_objects(bucket, recursive=True):
            client.remove_object(bucket, item.object_name)
        client.remove_bucket(bucket)
