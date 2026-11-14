"""The boto3 backend with a stand-in client (boto3 itself is not needed for the tests)."""

from pathlib import Path

from cloudpanel_backup.storage.boto import Boto3Storage


class FakeBody:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self) -> bytes:
        return self.data


class FakePaginator:
    def __init__(self, client: "FakeClient") -> None:
        self.client = client

    def paginate(self, Bucket, Prefix):
        keys = sorted(k for k in self.client.objects if k.startswith(Prefix))
        yield {"Contents": [{"Key": k, "Size": len(self.client.objects[k])} for k in keys[:1]]}
        yield {"Contents": [{"Key": k, "Size": len(self.client.objects[k])} for k in keys[1:]]}


class FakeClient:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.extra: list = []

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        self.extra.append(ExtraArgs)
        self.objects[key] = Path(filename).read_bytes()

    def put_object(self, Bucket, Key, Body, **kw):
        self.objects[Key] = Body

    def download_file(self, bucket, key, filename):
        if key not in self.objects:
            raise RuntimeError("404")
        Path(filename).write_bytes(self.objects[key])

    def get_object(self, Bucket, Key):
        return {"Body": FakeBody(self.objects[Key])}

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return FakePaginator(self)

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise RuntimeError("404")
        return {"ContentLength": len(self.objects[Key])}


def test_boto3_backend(tmp_path: Path):
    from test_storage import exercise

    client = FakeClient()
    storage = Boto3Storage(client, "bkt", storage_class="STANDARD_IA")
    exercise(storage, tmp_path)
    assert client.extra[0] == {"StorageClass": "STANDARD_IA"}
    assert storage.describe() == "s3://bkt (boto3)"
