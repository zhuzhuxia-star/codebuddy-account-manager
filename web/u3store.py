# -*- coding: utf-8 -*-
"""Altare U3 对象存储 · 极简 S3 客户端（纯标准库，SigV4，path-style）。

凭据文件（INI 风格 KEY=VALUE，权限应为 600）：
  U3_ACCESS_KEY_ID / U3_SECRET_ACCESS_KEY / U3_ENDPOINT / U3_REGION
同一把密钥可用于 us / eu / asia 三个区域端点，桶按区域隔离。
注意：U3 端点在 Cloudflare 之后，默认 Python UA 会被 1010 拦截，
请求必须带一个正常 User-Agent（不参与签名）。
"""
from __future__ import annotations

import hashlib
import hmac
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

UA = "aws-cli/2.17.0 Python/3.12.3"
NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


def load_client(env_path: str | Path) -> "U3Client | None":
    try:
        env = {}
        for line in Path(env_path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
        return U3Client(env["U3_ACCESS_KEY_ID"], env["U3_SECRET_ACCESS_KEY"],
                        env.get("U3_ENDPOINT", "https://u3-us.altare.global"),
                        env.get("U3_REGION", "us"))
    except Exception:
        return None


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


class U3Error(Exception):
    pass


class U3Client:
    def __init__(self, access_key: str, secret_key: str, endpoint: str, region: str):
        self.ak = access_key
        self.sk = secret_key
        self.endpoint = endpoint.rstrip("/")
        self.region = region
        self.host = self.endpoint.split("://", 1)[1]

    def _key_chain(self, datestamp: str) -> bytes:
        k = _sign(("AWS4" + self.sk).encode("utf-8"), datestamp)
        k = _sign(k, self.region)
        k = _sign(k, "s3")
        return _sign(k, "aws4_request")

    @staticmethod
    def _path(bucket: str, key: str = "") -> str:
        p = "/" + urllib.parse.quote(bucket, safe="")
        if key:
            p += "/" + urllib.parse.quote(key, safe="/")
        return p

    def request(self, method: str, bucket: str, key: str = "", body: bytes = b"",
                headers_extra: dict | None = None) -> tuple[int, bytes]:
        path = self._path(bucket, key)
        now = datetime.now(timezone.utc)
        amzdate = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body).hexdigest()
        headers = {"host": self.host, "x-amz-date": amzdate,
                   "x-amz-content-sha256": payload_hash}
        if headers_extra:
            headers.update(headers_extra)
        signed = ";".join(sorted(headers))
        canon_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
        canon = "\n".join([method, path, "", canon_headers, signed, payload_hash])
        scope = f"{datestamp}/{self.region}/s3/aws4_request"
        to_sign = "\n".join(["AWS4-HMAC-SHA256", amzdate, scope,
                             hashlib.sha256(canon.encode()).hexdigest()])
        sig = hmac.new(self._key_chain(datestamp), to_sign.encode("utf-8"),
                       hashlib.sha256).hexdigest()
        headers["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={self.ak}/{scope}, "
                                    f"SignedHeaders={signed}, Signature={sig}")
        req = urllib.request.Request(
            self.endpoint + path,
            data=body if method in ("PUT", "POST") else None,
            headers=headers, method=method)
        req.add_header("User-Agent", UA)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            data = e.read()
            raise U3Error(f"HTTP {e.code}: {data[:300].decode('utf-8', 'replace')}") from e

    def list_objects(self, bucket: str) -> list[dict]:
        out: list[dict] = []
        token = ""
        while True:
            path = self._path(bucket)
            q = "list-type=2"
            if token:
                q += "&continuation-token=" + urllib.parse.quote(token, safe="")
            # 带查询串的请求需单独签名，这里直接内联一次 GET
            status, data = self._get_query(path, q)
            root = ET.fromstring(data)
            for c in root.findall(f"{NS}Contents"):
                out.append({
                    "key": c.findtext(f"{NS}Key") or "",
                    "size": int(c.findtext(f"{NS}Size") or 0),
                    "modified": c.findtext(f"{NS}LastModified") or "",
                })
            if (root.findtext(f"{NS}IsTruncated") or "") == "true":
                token = root.findtext(f"{NS}NextContinuationToken") or ""
            else:
                break
        out.sort(key=lambda o: o["key"])
        return out

    def _get_query(self, path: str, query: str) -> tuple[int, bytes]:
        now = datetime.now(timezone.utc)
        amzdate = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(b"").hexdigest()
        headers = {"host": self.host, "x-amz-date": amzdate,
                   "x-amz-content-sha256": payload_hash}
        signed = ";".join(sorted(headers))
        canon_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
        canon = "\n".join(["GET", path, query, canon_headers, signed, payload_hash])
        scope = f"{datestamp}/{self.region}/s3/aws4_request"
        to_sign = "\n".join(["AWS4-HMAC-SHA256", amzdate, scope,
                             hashlib.sha256(canon.encode()).hexdigest()])
        sig = hmac.new(self._key_chain(datestamp), to_sign.encode("utf-8"),
                       hashlib.sha256).hexdigest()
        headers["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={self.ak}/{scope}, "
                                    f"SignedHeaders={signed}, Signature={sig}")
        req = urllib.request.Request(self.endpoint + path + "?" + query,
                                     headers=headers, method="GET")
        req.add_header("User-Agent", UA)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            data = e.read()
            raise U3Error(f"HTTP {e.code}: {data[:300].decode('utf-8', 'replace')}") from e

    def put_object(self, bucket: str, key: str, body: bytes, content_type: str = "") -> None:
        extra = {"content-type": content_type} if content_type else None
        self.request("PUT", bucket, key, body, extra)

    def delete_object(self, bucket: str, key: str) -> None:
        self.request("DELETE", bucket, key)

    def presign_get(self, bucket: str, key: str, expires: int = 3600) -> str:
        now = datetime.now(timezone.utc)
        amzdate = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        scope = f"{datestamp}/{self.region}/s3/aws4_request"
        path = self._path(bucket, key)
        params = {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": f"{self.ak}/{scope}",
            "X-Amz-Date": amzdate,
            "X-Amz-Expires": str(expires),
            "X-Amz-SignedHeaders": "host",
        }
        query = "&".join(f"{urllib.parse.quote(k, safe='')}="
                         f"{urllib.parse.quote(v, safe='')}"
                         for k, v in sorted(params.items()))
        canon = "\n".join(["GET", path, query, f"host:{self.host}\n", "host",
                           "UNSIGNED-PAYLOAD"])
        to_sign = "\n".join(["AWS4-HMAC-SHA256", amzdate, scope,
                             hashlib.sha256(canon.encode()).hexdigest()])
        sig = hmac.new(self._key_chain(datestamp), to_sign.encode("utf-8"),
                       hashlib.sha256).hexdigest()
        return f"{self.endpoint}{path}?{query}&X-Amz-Signature={sig}"

    # ---- 带查询串的签名请求（分片上传 / 流式下载用） ----
    def request_q(self, method: str, bucket: str, key: str, query: str,
                  body: bytes = b"", headers_extra: dict | None = None,
                  stream: bool = False, headers_unsigned: dict | None = None):
        path = self._path(bucket, key)
        now = datetime.now(timezone.utc)
        amzdate = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body).hexdigest()
        headers = {"host": self.host, "x-amz-date": amzdate,
                   "x-amz-content-sha256": payload_hash}
        if headers_extra:
            headers.update(headers_extra)
        signed = ";".join(sorted(headers))
        canon_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
        canon = "\n".join([method, path, query, canon_headers, signed, payload_hash])
        scope = f"{datestamp}/{self.region}/s3/aws4_request"
        to_sign = "\n".join(["AWS4-HMAC-SHA256", amzdate, scope,
                             hashlib.sha256(canon.encode()).hexdigest()])
        sig = hmac.new(self._key_chain(datestamp), to_sign.encode("utf-8"),
                       hashlib.sha256).hexdigest()
        headers["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={self.ak}/{scope}, "
                                    f"SignedHeaders={signed}, Signature={sig}")
        req = urllib.request.Request(
            self.endpoint + path + ("?" + query if query else ""),
            data=body if method in ("PUT", "POST") else None,
            headers=headers, method=method)
        req.add_header("User-Agent", UA)
        for k, v in (headers_unsigned or {}).items():
            req.add_header(k, v)
        try:
            resp = urllib.request.urlopen(req, timeout=300)
            if stream:
                return resp.status, resp.headers, resp
            with resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            data = e.read()
            raise U3Error(f"HTTP {e.code}: {data[:300].decode('utf-8', 'replace')}") from e

    def open_get(self, bucket: str, key: str, range_header: str | None = None):
        """打开一个已签名的 GET 响应流，返回 (status, headers, resp)，调用方负责读完/关闭。

        range_header（如 "bytes=0-1023"）以**不签名**方式转发：Garage 拒绝
        签名头里的 range（实测报 signed header `range` is not present），
        而 SigV4 允许附加未签名头，Range 照常生效。媒体在线播放/断点续传
        靠它拿到 206 分段响应。
        """
        unsigned = {"Range": range_header} if range_header else None
        return self.request_q("GET", bucket, key, "", stream=True,
                              headers_unsigned=unsigned)

    def open_head(self, bucket: str, key: str):
        """HEAD 取对象元信息，返回 headers（播放器探测时用）。"""
        _st, headers, resp = self.request_q("HEAD", bucket, key, "", stream=True)
        resp.close()
        return headers

    # ---- 分片上传 ----
    def mp_create(self, bucket: str, key: str, content_type: str = "") -> str:
        extra = {"content-type": content_type} if content_type else None
        _st, _hd, data = self.request_q("POST", bucket, key, "uploads=", b"", extra)
        root = ET.fromstring(data)
        uid = root.findtext(f"{NS}UploadId") or ""
        if not uid:
            raise U3Error("CreateMultipartUpload 未返回 UploadId")
        return uid

    def mp_upload_part(self, bucket: str, key: str, upload_id: str,
                       part_no: int, body: bytes) -> str:
        q = (f"partNumber={part_no}&uploadId="
             f"{urllib.parse.quote(upload_id, safe='')}")
        _st, headers, _data = self.request_q("PUT", bucket, key, q, body)
        etag = headers.get("ETag") or headers.get("etag") or ""
        if not etag:
            raise U3Error(f"UploadPart {part_no} 未返回 ETag")
        return etag

    def mp_complete(self, bucket: str, key: str, upload_id: str,
                    parts: list[tuple[int, str]]) -> None:
        xml = "<CompleteMultipartUpload>" + "".join(
            f"<Part><PartNumber>{n}</PartNumber><ETag>{e}</ETag></Part>"
            for n, e in parts) + "</CompleteMultipartUpload>"
        q = f"uploadId={urllib.parse.quote(upload_id, safe='')}"
        self.request_q("POST", bucket, key, q, xml.encode())

    def mp_abort(self, bucket: str, key: str, upload_id: str) -> None:
        q = f"uploadId={urllib.parse.quote(upload_id, safe='')}"
        try:
            self.request_q("DELETE", bucket, key, q)
        except Exception:  # noqa: BLE001  放弃失败无所谓，残片由平台清理
            pass
