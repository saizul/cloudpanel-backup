import json

import pytest

from cloudpanel_backup.manifest import Manifest, ManifestEntry, ManifestError


def sample() -> Manifest:
    return Manifest(
        domain="a.example",
        host="h",
        timestamp="20261114T000000Z",
        tool_version="0.1.0",
        entries=[ManifestEntry("files.tar.gz", "files", 10, "a" * 64, "a" * 64, 10)],
    )


def test_roundtrip():
    m = sample()
    back = Manifest.from_json(m.to_json())
    assert back == m
    assert back.total_size == 10
    assert back.entry("files.tar.gz").kind == "files"
    with pytest.raises(KeyError):
        back.entry("nope")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(tool="other"),
        lambda d: d.update(format_version=99),
        lambda d: d["entries"][0].update(sha256="short"),
        lambda d: d["entries"][0].update(name="../escape"),
        lambda d: d.pop("domain"),
        lambda d: d["entries"][0].update(unknown=1),
    ],
)
def test_invalid_manifests(mutate):
    data = json.loads(sample().to_json())
    mutate(data)
    with pytest.raises(ManifestError):
        Manifest.from_json(json.dumps(data))


def test_not_json():
    with pytest.raises(ManifestError):
        Manifest.from_json("[")
