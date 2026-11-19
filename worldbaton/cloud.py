"""S3-compatible storage (Backblaze B2, Cloudflare R2, Amazon S3, MinIO...) with only the standard library:
AWS Signature Version 4 over HTTPS. Used for the group's cloud copy of the world (see core.CloudCopy)."""
import datetime
import hashlib
import hmac
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

CHUNK = 1 << 20
EMPTY = hashlib.sha256(b"").hexdigest()
MAX_PUT = 5 * 10**9  # S3's limit for one upload
FRIENDLY = {
    "InvalidAccessKeyId": "The key ID is wrong.",
    "SignatureDoesNotMatch": "The secret key is wrong.",
    "NoSuchBucket": "There's no bucket with that name at that address.",
    "AccessDenied": "That key isn't allowed to use this bucket. Give it read and write access.",
    "InvalidBucketName": "That bucket name isn't valid.",
}


class CloudError(Exception):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


def _q(text: str, safe="-_.~") -> str:
    return urllib.parse.quote(text, safe=safe)


def sign(method, host, path, query: dict, headers: dict, payload_hash, key_id, secret, region, amz_date) -> str:
    """The Authorization header for one request (AWS Signature Version 4). headers: any extra headers to sign."""
    signed_headers = {**{k.lower(): str(v).strip() for k, v in headers.items()}, "host": host,
                      "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
    names = ";".join(sorted(signed_headers))
    canonical = "\n".join([
        method, _q(path, safe="/-_.~"),
        "&".join(f"{_q(k)}={_q(str(v))}" for k, v in sorted(query.items())),
        "".join(f"{k}:{signed_headers[k]}\n" for k in sorted(signed_headers)), names, payload_hash])
    scope = f"{amz_date[:8]}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = ("AWS4" + secret).encode()
    for part in (amz_date[:8], region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return f"AWS4-HMAC-SHA256 Credential={key_id}/{scope},SignedHeaders={names},Signature={signature}"


class _Reader:
    """A file being uploaded, reporting how far it got."""
    def __init__(self, f, size, progress):
        self.f, self.size, self.done, self.progress = f, size, 0, progress

    def read(self, n=-1):
        data = self.f.read(n)
        self.done += len(data)
        if self.progress and self.size:
            self.progress(self.done / self.size)
        return data


def _tag(el) -> str:
    return el.tag.rsplit("}", 1)[-1]


class S3:
    def __init__(self, endpoint: str, bucket: str, key_id: str, secret: str):
        url = endpoint.strip().rstrip("/")
        if "://" not in url:
            url = "https://" + url
        parts = urllib.parse.urlsplit(url)
        if not parts.hostname:
            raise ValueError("Paste the S3 endpoint, like s3.us-west-004.backblazeb2.com.")
        if parts.scheme != "https" and parts.hostname not in ("localhost", "127.0.0.1"):
            raise ValueError("The endpoint must use https://, so your keys and world are sent encrypted.")
        if not (bucket.strip() and key_id.strip() and secret.strip()):
            raise ValueError("Fill in the bucket name, the key ID and the secret key.")
        default_port = {"https": 443, "http": 80}[parts.scheme] if parts.scheme in ("https", "http") else None
        self.host = parts.hostname + (f":{parts.port}" if parts.port and parts.port != default_port else "")
        self.base = f"{parts.scheme}://{self.host}"
        m = re.search(r"s3[.-]([a-z0-9-]+)\.(?:backblazeb2|amazonaws|wasabisys)\.com$", parts.hostname)
        self.region = m.group(1) if m else "us-east-1"  # Cloudflare R2 and MinIO take us-east-1 as their default
        self.bucket, self.key_id, self.secret = bucket.strip(), key_id.strip(), secret.strip()

    def _call(self, method, key="", query=None, body=None, size=0, payload=EMPTY, timeout=30):
        path, query = f"/{self.bucket}/{key}" if key else f"/{self.bucket}", query or {}
        amz_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        headers = {"x-amz-date": amz_date, "x-amz-content-sha256": payload, "User-Agent": "WorldBaton",
                   "Authorization": sign(method, self.host, path, query, {}, payload, self.key_id, self.secret,
                                         self.region, amz_date)}
        if method == "PUT":
            headers["Content-Length"] = str(size)
        url = self.base + _q(path, safe="/-_.~") + ("?" + "&".join(f"{_q(k)}={_q(str(v))}" for k, v in
                                                                   sorted(query.items())) if query else "")
        try:
            return urllib.request.urlopen(urllib.request.Request(url, data=body, method=method, headers=headers),
                                          timeout=timeout)
        except urllib.error.HTTPError as e:
            code = message = ""
            try:
                root = ET.fromstring(e.read())
                code = next((el.text for el in root.iter() if _tag(el) == "Code"), "") or ""
                message = next((el.text for el in root.iter() if _tag(el) == "Message"), "") or ""
            except ET.ParseError:
                pass
            finally:
                e.close()
            raise CloudError(FRIENDLY.get(code) or f"The storage said {e.code} {code or e.reason}"
                             + (f": {message}" if message else ""), e.code)
        except (urllib.error.URLError, OSError) as e:
            raise CloudError(f"Couldn't reach {self.host}: {getattr(e, 'reason', e)}")

    def get(self, key) -> bytes | None:
        """An object's contents, or None if there isn't one."""
        try:
            with self._call("GET", key) as r:
                return r.read()
        except CloudError as e:
            if e.status == 404:
                return None
            raise

    def put(self, key, data: bytes) -> None:
        self._call("PUT", key, body=data, size=len(data), payload=hashlib.sha256(data).hexdigest()).close()

    def delete(self, key) -> None:
        self._call("DELETE", key).close()

    def put_file(self, key, path: Path, progress=None) -> None:
        size, h = Path(path).stat().st_size, hashlib.sha256()
        if size > MAX_PUT:
            # shortcut: one upload per file caps a world zip at 5 GB; multipart uploads lift that if a group needs it.
            raise CloudError(f"The world zip is {size / 1e9:.1f} GB. Cloud copies over 5 GB aren't supported yet.")
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(CHUNK), b""):
                h.update(chunk)
        with open(path, "rb") as f:
            self._call("PUT", key, body=_Reader(f, size, progress), size=size, payload=h.hexdigest(), timeout=120).close()

    def get_file(self, key, dest: Path, progress=None) -> None:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        with self._call("GET", key, timeout=120) as r, open(part, "wb") as f:
            size, done = int(r.headers.get("Content-Length") or 0), 0
            for chunk in iter(lambda: r.read(CHUNK), b""):
                f.write(chunk)
                done += len(chunk)
                if progress and size:
                    progress(done / size)
        os.replace(part, dest)

    def list(self, prefix="") -> dict[str, tuple[int, float]]:
        """{key: (size, last modified)} for every object whose key starts with prefix."""
        out, token = {}, None
        while True:
            query = {"list-type": "2", "prefix": prefix, **({"continuation-token": token} if token else {})}
            with self._call("GET", query=query) as r:
                root = ET.fromstring(r.read())
            for item in (el for el in root if _tag(el) == "Contents"):
                fields = {_tag(el): el.text or "" for el in item}
                modified = datetime.datetime.fromisoformat(fields.get("LastModified", "").replace("Z", "+00:00"))
                out[fields["Key"]] = (int(fields.get("Size") or 0), modified.timestamp())
            token = next((el.text for el in root if _tag(el) == "NextContinuationToken"), None)
            if not token:
                return out

    def check(self, key) -> None:
        """Write, read and delete a small object: raises CloudError if any of it isn't allowed."""
        self.put(key, b"WorldBaton check")
        if self.get(key) != b"WorldBaton check":
            raise CloudError("The storage didn't give back what was saved.")
        self.list(key)
        self.delete(key)
