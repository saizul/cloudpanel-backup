from datetime import datetime, timezone

from cloudpanel_backup.sigv4 import EMPTY_SHA256, canonical_query, canonical_request, sign, signing_key, uri_encode

# The "GET Object" example from the AWS documentation "Signature Calculations for the Authorization Header:
# Transferring Payload in a Single Chunk (AWS Signature Version 4)".
AWS_ACCESS = "AKIAIOSFODNN7EXAMPLE"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
MOMENT = datetime(2013, 5, 24, tzinfo=timezone.utc)


def test_aws_documented_get_object_example():
    headers = sign(
        "GET",
        "https://examplebucket.s3.amazonaws.com/test.txt",
        {},
        {"Range": "bytes=0-9"},
        EMPTY_SHA256,
        AWS_ACCESS,
        AWS_SECRET,
        "us-east-1",
        now=MOMENT,
    )
    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )
    assert headers["x-amz-date"] == "20130524T000000Z"


def test_aws_documented_list_objects_example():
    headers = sign(
        "GET",
        "https://examplebucket.s3.amazonaws.com/",
        {"max-keys": "2", "prefix": "J"},
        {},
        EMPTY_SHA256,
        AWS_ACCESS,
        AWS_SECRET,
        "us-east-1",
        now=MOMENT,
    )
    assert headers["Authorization"].endswith(
        "Signature=34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"
    )


def test_canonical_query_sorts_and_encodes():
    assert canonical_query({"prefix": "a b/c", "list-type": "2", "uploads": ""}) == (
        "list-type=2&prefix=a%20b%2Fc&uploads="
    )


def test_uri_encode_keeps_unreserved():
    assert uri_encode("a-b_c.d~e f+g") == "a-b_c.d~e%20f%2Bg"
    assert uri_encode("dir/file name", keep_slash=True) == "dir/file%20name"


def test_canonical_request_normalizes_headers():
    request, signed = canonical_request("put", "/b/k", {}, {"X-Amz-Meta-A": "  a   b ", "Host": "h"}, "abc")
    assert signed == "host;x-amz-meta-a"
    assert "x-amz-meta-a:a b\n" in request
    assert request.startswith("PUT\n/b/k\n\n")


def test_signing_key_is_stable():
    assert signing_key("secret", "20260101", "auto", "s3") == signing_key("secret", "20260101", "auto", "s3")
    assert signing_key("secret", "20260101", "auto", "s3") != signing_key("secret", "20260102", "auto", "s3")
