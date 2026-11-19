"""A tiny S3 server for tests: keeps objects in memory and, like a real one, refuses requests whose signature
doesn't match what it received."""
import datetime
import hashlib
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

from worldbaton.cloud import sign

KEY_ID, SECRET, BUCKET = "test-key", "test-secret", "worlds"


class FakeS3:
    def __init__(self):
        self.objects: dict[str, tuple[bytes, float]] = {}
        self.requests: list[str] = []
        store = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, body=b"", content_type="application/xml"):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _error(self, status, code):
                self._reply(status, f"<Error><Code>{code}</Code><Message>{code}</Message></Error>".encode())

            def _handle(self):
                url = urllib.parse.urlsplit(self.path)
                path, query = urllib.parse.unquote(url.path), dict(urllib.parse.parse_qsl(url.query, keep_blank_values=True))
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)) if self.command == "PUT" else b""
                store.requests.append(f"{self.command} {path}")
                auth = self.headers.get("Authorization", "")
                m = re.match(r"AWS4-HMAC-SHA256 Credential=([^/]+)/(\d{8})/([^/]+)/s3/aws4_request,"
                             r"SignedHeaders=([^,]+),Signature=([0-9a-f]{64})$", auth)
                if not m or m.group(1) != KEY_ID:
                    return self._error(403, "InvalidAccessKeyId")
                payload = self.headers.get("x-amz-content-sha256", "")
                extra = {h: self.headers[h] for h in m.group(4).split(";")
                         if h not in ("host", "x-amz-content-sha256", "x-amz-date")}
                expected = sign(self.command, self.headers["Host"], path, query, extra, payload, KEY_ID, SECRET,
                                m.group(3), self.headers.get("x-amz-date", ""))
                if expected != auth or (self.command == "PUT" and hashlib.sha256(body).hexdigest() != payload):
                    return self._error(403, "SignatureDoesNotMatch")
                bucket, _, key = path.lstrip("/").partition("/")
                if bucket != BUCKET:
                    return self._error(404, "NoSuchBucket")
                if self.command == "PUT":
                    store.objects[key] = (body, datetime.datetime.now(datetime.timezone.utc).timestamp())
                    return self._reply(200)
                if self.command == "DELETE":
                    store.objects.pop(key, None)
                    return self._reply(204)
                if key:
                    if key not in store.objects:
                        return self._error(404, "NoSuchKey")
                    return self._reply(200, store.objects[key][0], "application/octet-stream")
                # ListObjectsV2, two keys per page so continuation is exercised
                keys = sorted(k for k in store.objects if k.startswith(query.get("prefix", "")))
                start = int(query.get("continuation-token", 0))
                page, more = keys[start:start + 2], start + 2 < len(keys)
                items = "".join(
                    f"<Contents><Key>{escape(k)}</Key><Size>{len(store.objects[k][0])}</Size><LastModified>"
                    f"{datetime.datetime.fromtimestamp(store.objects[k][1], datetime.timezone.utc):%Y-%m-%dT%H:%M:%S.000Z}"
                    f"</LastModified></Contents>" for k in page)
                token = f"<NextContinuationToken>{start + 2}</NextContinuationToken>" if more else ""
                self._reply(200, f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">{items}{token}'
                                 f"</ListBucketResult>".encode())

            do_GET = do_PUT = do_DELETE = do_HEAD = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.endpoint = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def settings(self, **changes) -> dict:
        """What a group would save in Settings to use this server."""
        return {"endpoint": self.endpoint, "bucket": BUCKET, "key_id": KEY_ID, "secret": SECRET, **changes}

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
