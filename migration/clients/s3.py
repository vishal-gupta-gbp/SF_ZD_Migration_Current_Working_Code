"""AWS S3 Client for high-performance concurrent file downloads."""
from __future__ import annotations

import hashlib
import mimetypes
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import unquote, urlparse

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import BotoCoreError, ClientError

S3_VIRTUAL_HOST_RE = re.compile(r"^([^.]+)\.s3[.-]([^.]+)\.amazonaws\.com$", re.I)
S3_PATH_STYLE_RE = re.compile(r"^s3[.-]([^.]+)\.amazonaws\.com$", re.I)


def parse_s3_url(url: str) -> Tuple[str, str]:
    """Parse standard, virtual-hosted, or path-style S3 URLs into (bucket, key)."""
    raw = str(url or "").strip()
    if not raw:
        return "", ""

    if raw.startswith("s3://"):
        part = raw[5:]
        bucket, _, key = part.partition("/")
        return bucket, unquote(key)

    parsed = urlparse(raw)
    host = parsed.netloc or ""
    path = parsed.path.lstrip("/")

    vh_match = S3_VIRTUAL_HOST_RE.match(host)
    if vh_match:
        return vh_match.group(1), unquote(path)

    ps_match = S3_PATH_STYLE_RE.match(host)
    if ps_match and "/" in path:
        bucket, _, key = path.partition("/")
        return bucket, unquote(key)

    if host.endswith(".amazonaws.com") and ".s3." in host:
        bucket = host.split(".s3.", 1)[0]
        return bucket, unquote(path)

    return "", ""


def construct_migration_s3_url(raw_url: str, target_base_url: str) -> str:
    """Normalize source S3 URL against target S3 base URL."""
    raw = str(raw_url or "").strip()
    if not raw or not target_base_url:
        return raw

    _, key = parse_s3_url(raw)
    if not key:
        return raw

    clean_base = target_base_url.rstrip("/") + "/"
    # Clean up leading bucket names or common prefixes if present in key
    clean_key = key.lstrip("/")
    return f"{clean_base}{clean_key}"


class S3Client:
    def __init__(self, access_key_id: str, secret_access_key: str, region: str = "us-east-1"):
        if not access_key_id or not secret_access_key:
            raise RuntimeError("AWS credentials are required for S3 access.")

        self.region = region or "us-east-1"
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.client = boto3.client(
            "s3",
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=self.region,
        )

    def download_file(
        self,
        url: str,
        local_path: Path,
        max_concurrency: int = 2,
    ) -> Dict[str, Any]:
        """Download an S3 object to local path and return metadata with SHA256 checksum."""
        bucket, key = parse_s3_url(url)
        if not bucket or not key:
            raise RuntimeError(f"Invalid or unparseable S3 URL: {url}")

        local_path.parent.mkdir(parents=True, exist_ok=True)
        transfer_config = TransferConfig(max_concurrency=max_concurrency, use_threads=True)

        try:
            self.client.download_file(
                Bucket=bucket,
                Key=key,
                Filename=str(local_path),
                Config=transfer_config,
            )
        except (ClientError, BotoCoreError, OSError) as exc:
            raise RuntimeError(f"S3 download failed for {url}: {exc}") from exc

        if not local_path.exists():
            raise RuntimeError(f"S3 download completed but local file was not created: {local_path}")

        file_size = local_path.stat().st_size
        mime = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"

        # Calculate SHA256
        hasher = hashlib.sha256()
        with local_path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                hasher.update(chunk)

        return {
            "bucket": bucket,
            "key": key,
            "size": file_size,
            "content_type": mime,
            "sha256": hasher.hexdigest(),
        }
