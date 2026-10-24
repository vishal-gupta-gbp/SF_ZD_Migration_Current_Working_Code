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


def normalize_s3_key_slashes(key: str) -> str:
    """Fix missing slashes after environment prefixes (e.g. 'Prod...' -> 'Prod/...')."""
    k = str(key or "").strip().lstrip("/")
    if k.startswith(("Prod", "prod")) and not k.startswith(("Prod/", "prod/")):
        return f"Prod/{k[4:].lstrip('/')}"
    if k.startswith(("UAT", "uat")) and not k.startswith(("UAT/", "uat/")):
        return f"UAT/{k[3:].lstrip('/')}"
    return k


def parse_s3_url(url: str) -> Tuple[str, str]:
    """Parse standard, virtual-hosted, or path-style S3 URLs into (bucket, key)."""
    raw = str(url or "").strip()
    if not raw:
        return "", ""

    if raw.startswith("s3://"):
        part = raw[5:]
        bucket, _, key = part.partition("/")
        return bucket, normalize_s3_key_slashes(unquote(key))

    parsed = urlparse(raw)
    host = parsed.netloc or ""
    path = parsed.path.lstrip("/")

    vh_match = S3_VIRTUAL_HOST_RE.match(host)
    if vh_match:
        return vh_match.group(1), normalize_s3_key_slashes(unquote(path))

    ps_match = S3_PATH_STYLE_RE.match(host)
    if ps_match and "/" in path:
        bucket, _, key = path.partition("/")
        return bucket, normalize_s3_key_slashes(unquote(key))

    if host.endswith(".amazonaws.com") and ".s3." in host:
        bucket = host.split(".s3.", 1)[0]
        return bucket, normalize_s3_key_slashes(unquote(path))

    return "", ""


def strip_s3_env_prefix(key: str) -> Tuple[str, Optional[str]]:
    """Strip leading UAT/ or Prod/ prefixes from key, returning (clean_key, original_prefix)."""
    k = normalize_s3_key_slashes(key)
    original_prefix = None
    while True:
        matched = False
        for prefix in ("Prod/UAT/", "UAT/Prod/", "prod/uat/", "uat/prod/"):
            if k.startswith(prefix):
                k = k[len(prefix):].lstrip("/")
                matched = True
        for prefix in ("UAT/", "Prod/", "uat/", "prod/"):
            if k.startswith(prefix):
                if original_prefix is None:
                    original_prefix = prefix
                k = k[len(prefix):].lstrip("/")
                matched = True
        if not matched:
            break
    return k, original_prefix


def get_s3_bucket_base_url(target_base_url: str, bucket: str = "", region: str = "us-east-1") -> str:
    """Return base bucket URL stripped of any trailing /Prod or /UAT or trailing slash."""
    base = str(target_base_url or "").strip().rstrip("/")
    for env_suffix in ("/Prod", "/UAT", "/prod", "/uat"):
        if base.endswith(env_suffix):
            base = base[:-len(env_suffix)].rstrip("/")
    if base:
        return base
    if bucket:
        return f"https://{bucket}.s3.{region}.amazonaws.com"
    return ""


def get_candidate_s3_keys(raw_key: str) -> list[str]:
    """Generate candidate keys across UAT/, Prod/, and base/root folder."""
    clean_key, orig_prefix = strip_s3_env_prefix(raw_key)
    if not clean_key:
        return []

    # Handle cases like Prodimage001 or UATimage001 missing a slash
    clean_alt = None
    if clean_key.startswith(("Prod", "prod")) and len(clean_key) > 4 and clean_key[4] != "/":
        clean_alt = clean_key[4:].lstrip("/")
    elif clean_key.startswith(("UAT", "uat")) and len(clean_key) > 3 and clean_key[3] != "/":
        clean_alt = clean_key[3:].lstrip("/")

    # Prioritize candidate folders based on original prefix hint
    prefixes = ["UAT/", "Prod/", ""]
    if orig_prefix in ("Prod/", "prod/"):
        prefixes = ["Prod/", "UAT/", ""]
    elif orig_prefix in ("UAT/", "uat/"):
        prefixes = ["UAT/", "Prod/", ""]

    candidates: list[str] = []
    for p in prefixes:
        c = f"{p}{clean_key}"
        if c not in candidates:
            candidates.append(c)
        if clean_alt:
            c2 = f"{p}{clean_alt}"
            if c2 not in candidates:
                candidates.append(c2)

    return candidates


_KEY_RESOLUTION_CACHE: Dict[Tuple[str, str], Optional[str]] = {}


def resolve_existing_s3_key(
    client: Any,
    bucket: str,
    raw_key: str,
) -> Optional[str]:
    """Check whether the file exists in UAT folder, Prod folder, or base folder in S3."""
    if not bucket or not raw_key:
        return None

    cache_key = (bucket, raw_key)
    if cache_key in _KEY_RESOLUTION_CACHE:
        return _KEY_RESOLUTION_CACHE[cache_key]

    candidates = get_candidate_s3_keys(raw_key)
    for c in candidates:
        try:
            client.head_object(Bucket=bucket, Key=c)
            _KEY_RESOLUTION_CACHE[cache_key] = c
            return c
        except ClientError as exc:
            err_code = str(exc.response.get("Error", {}).get("Code", ""))
            if err_code in ("404", "NoSuchKey", "NotFound"):
                continue
            continue
        except Exception:
            continue

    _KEY_RESOLUTION_CACHE[cache_key] = None
    return None


def construct_migration_s3_url(
    raw_url: str,
    target_base_url: str,
    s3_client: Optional[Any] = None,
) -> str:
    """Normalize source S3 URL by checking existence in UAT, Prod, or base folder.

    Prevents duplicate '/Prod/UAT' or '/UAT/Prod' segments.
    """
    raw = str(raw_url or "").strip()
    if not raw:
        return ""

    bucket, key = parse_s3_url(raw)
    clean_base = get_s3_bucket_base_url(target_base_url, bucket=bucket)

    if not key:
        return raw

    clean_key, orig_prefix = strip_s3_env_prefix(key)

    resolved_key = None
    if s3_client is not None:
        client_obj = getattr(s3_client, "client", s3_client)
        if hasattr(client_obj, "head_object"):
            resolved_key = resolve_existing_s3_key(client_obj, bucket, key)

    if resolved_key:
        final_key = resolved_key
    elif orig_prefix:
        final_key = f"{orig_prefix.rstrip('/')}/{clean_key}"
    elif target_base_url and any(target_base_url.rstrip("/").endswith(p) for p in ("/Prod", "/prod")):
        final_key = f"Prod/{clean_key}"
    elif target_base_url and any(target_base_url.rstrip("/").endswith(p) for p in ("/UAT", "/uat")):
        final_key = f"UAT/{clean_key}"
    else:
        final_key = clean_key

    if clean_base:
        return f"{clean_base}/{final_key}"
    return final_key


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

    def resolve_key(self, bucket: str, raw_key: str) -> Optional[str]:
        """Check existence across UAT/, Prod/, and base folder."""
        return resolve_existing_s3_key(self.client, bucket, raw_key)

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

        resolved_key = self.resolve_key(bucket, key)
        actual_key = resolved_key or key

        last_exc: Any = None
        for attempt in range(1, 4):
            try:
                self.client.download_file(
                    Bucket=bucket,
                    Key=actual_key,
                    Filename=str(local_path),
                    Config=transfer_config,
                )
                if local_path.exists():
                    break
            except (ClientError, BotoCoreError, OSError) as exc:
                last_exc = exc
                # If initial key failed with 404, fallback to checking other candidates
                if isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                    candidates = get_candidate_s3_keys(key)
                    downloaded = False
                    for cand in candidates:
                        if cand == actual_key:
                            continue
                        try:
                            self.client.download_file(
                                Bucket=bucket,
                                Key=cand,
                                Filename=str(local_path),
                                Config=transfer_config,
                            )
                            if local_path.exists():
                                actual_key = cand
                                downloaded = True
                                break
                        except Exception:
                            continue
                    if downloaded:
                        break

                if attempt < 3:
                    import time
                    print(f"  [S3] Download attempt {attempt} failed for {url} ({exc}). Retrying ({attempt}/2)...", flush=True)
                    time.sleep(1.5)
                    continue
                raise RuntimeError(f"S3 download failed for {url} (key: {actual_key}) after 2 retries: {exc}") from exc
        else:
            if last_exc:
                raise RuntimeError(f"S3 download failed for {url} (key: {actual_key}): {last_exc}") from last_exc

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
            "key": actual_key,
            "size": file_size,
            "content_type": mime,
            "sha256": hasher.hexdigest(),
        }
