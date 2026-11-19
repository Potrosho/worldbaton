"""The S3 client behind the cloud copy: signatures, objects, errors."""
import json
import tempfile
import unittest
from pathlib import Path

from tests.fake_s3 import FakeS3
from worldbaton import core
from worldbaton.cloud import EMPTY, S3, CloudError, sign

AWS_KEY, AWS_SECRET = "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


class Signing(unittest.TestCase):
    def test_aws_examples(self):
        """The worked examples in AWS's Signature Version 4 documentation for S3."""
        host, when = "examplebucket.s3.amazonaws.com", "20130524T000000Z"
        cases = [("/test.txt", {}, {"Range": "bytes=0-9"}, "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"),
                 ("/", {"max-keys": "2", "prefix": "J"}, {}, "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"),
                 ("/", {"lifecycle": ""}, {}, "fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543")]
        for path, query, headers, signature in cases:
            auth = sign("GET", host, path, query, headers, EMPTY, AWS_KEY, AWS_SECRET, "us-east-1", when)
            self.assertTrue(auth.endswith("Signature=" + signature), path)

    def test_endpoints(self):
        b2 = S3("s3.us-west-004.backblazeb2.com", "b", "k", "s")
        self.assertEqual((b2.base, b2.region), ("https://s3.us-west-004.backblazeb2.com", "us-west-004"))
        r2 = S3("https://abc123.r2.cloudflarestorage.com/", "b", "k", "s")
        self.assertEqual((r2.host, r2.region), ("abc123.r2.cloudflarestorage.com", "us-east-1"))
        self.assertEqual(S3("https://s3.eu-west-2.amazonaws.com:443", "b", "k", "s").host, "s3.eu-west-2.amazonaws.com")
        with self.assertRaisesRegex(ValueError, "https"):
            S3("http://s3.example.com", "b", "k", "s")  # keys and worlds never go out unencrypted
        with self.assertRaisesRegex(ValueError, "Fill in"):
            S3("s3.example.com", "b", "", "s")


class Objects(unittest.TestCase):
    def setUp(self):
        self.fake = FakeS3()
        self.s3 = S3(**{k: v for k, v in self.fake.settings().items()})
        self.tmp = Path(tempfile.mkdtemp(prefix="worldbaton-s3-"))

    def tearDown(self):
        self.fake.stop()

    def test_round_trip(self):
        self.assertIsNone(self.s3.get("nothing"))
        self.s3.put("w-baton.json", b'{"version": 3}')
        self.assertEqual(self.s3.get("w-baton.json"), b'{"version": 3}')
        big = self.tmp / "world.zip"
        big.write_bytes(bytes(range(256)) * 20000)  # several chunks
        seen = []
        self.s3.put_file("w-v3.zip", big, seen.append)
        self.assertEqual(seen[-1], 1.0)
        for v in (1, 2, 4):
            self.s3.put(f"w-v{v}.zip", b"x" * v)
        self.s3.put("other-v1.zip", b"not ours")
        listed = self.s3.list("w-v")
        self.assertEqual(sorted(listed), ["w-v1.zip", "w-v2.zip", "w-v3.zip", "w-v4.zip"])  # over two pages
        self.assertEqual(listed["w-v3.zip"][0], big.stat().st_size)
        got = []
        self.s3.get_file("w-v3.zip", self.tmp / "down" / "world.zip", got.append)
        self.assertEqual((self.tmp / "down" / "world.zip").read_bytes(), big.read_bytes())
        self.assertEqual(got[-1], 1.0)
        self.s3.delete("w-v1.zip")
        self.assertNotIn("w-v1.zip", self.s3.list("w-v"))
        self.s3.check("w-check")
        self.assertNotIn("w-check", self.fake.objects)

    def test_errors_in_plain_words(self):
        for changes, message in (({"secret": "wrong"}, "secret key is wrong"), ({"key_id": "nope"}, "key ID is wrong"),
                                 ({"bucket": "typo"}, "no bucket")):
            with self.assertRaisesRegex(CloudError, message):
                S3(**self.fake.settings(**changes)).check("x")
        with self.assertRaisesRegex(CloudError, "Couldn't reach"):
            S3("http://127.0.0.1:9", "b", "k", "s").get("x")

    def test_older_baton_never_replaces_newer(self):
        copy = core.CloudCopy(self.fake.settings(), "w")
        copy.write_baton({"version": 5, "holder": None}, now=True)
        copy.write_baton({"version": 4, "holder": "SAM", "heartbeat": 1}, now=True)  # late heartbeat from before
        self.assertEqual(json.loads(self.s3.get("w-baton.json"))["version"], 5)
        copy.write_baton({"version": 5, "holder": "ALEX", "heartbeat": 2}, now=True)
        self.assertEqual(json.loads(self.s3.get("w-baton.json"))["holder"], "ALEX")


if __name__ == "__main__":
    unittest.main()
