from __future__ import annotations

import asyncio
import io
import mimetypes
from typing import Optional

import boto3
from botocore.config import Config as BotoConfig

from app.config import settings
from app.utils.logger import log_event

_cfg = BotoConfig(region_name=settings.AWS_REGION, retries={"max_attempts": 3, "mode": "standard"})

_s3 = None


def _s3_client():
    global _s3
    if _s3 is None:
        kwargs = {"config": _cfg, "region_name": settings.AWS_REGION}
        if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
            kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
            kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
            kwargs["aws_session_token"]=settings.AWS_SESSION_TOKEN
        _s3 = boto3.client("s3", **kwargs)
    return _s3


async def upload_kb_document(
    *,
    department_code: str,
    filename: str,
    data: bytes,
    uploader_email: str,
    metadata: Optional[dict] = None,
) -> dict:
    """
    Upload a document to s3://<bucket>/<department>/<filename>.

    Bedrock KB ingestion reads the companion `.metadata.json` written
    alongside; the `department` field is what the retrieval filter
    matches on, so getting it right is non-optional. Any additional
    admin-defined `metadata` (an arbitrary {key: value} dict) is merged
    into BOTH the S3 object metadata and the sidecar `metadataAttributes`
    so each entry becomes a retrieval-time filter dimension.
    """
    bucket = settings.S3_BUCKET_NAME
    dept = department_code.lower()
    key = f"{dept}/{filename}"
    ct = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    
    custom_raw = {str(k): v for k, v in (metadata or {}).items()
                  if k is not None and v is not None and str(k).strip()}
    custom_s3 = {
        k: (", ".join(str(x) for x in v) if isinstance(v, (list, tuple, set)) else str(v))
        for k, v in custom_raw.items()
    }
    custom_sidecar = {
        k: ([str(x) for x in v] if isinstance(v, (list, tuple, set)) else str(v))
        for k, v in custom_raw.items()
    }

    extra: dict = {
        "ContentType": ct,
        "Metadata": {
            "department": dept,
            "uploader": uploader_email,
            **custom_s3,
        },
        "ServerSideEncryption": "AES256",
    }
    # if settings.S3_KMS_KEY_ID:
    #     extra["ServerSideEncryption"] = "aws:kms"
    #     extra["SSEKMSKeyId"] = settings.S3_KMS_KEY_ID

    # Sidecar metadata file consumed by Bedrock KB connector. Every custom
    # attribute here can be used as a `filter` key at retrieval time.
    sidecar = {
        "metadataAttributes": {
            "department": dept,
            "uploader": uploader_email,
            "source_filename": filename,
            **custom_sidecar,
        }
    }

    def _put():
        client = _s3_client()
        client.put_object(Bucket=bucket, Key=key, Body=data, **extra)
        client.put_object(
            Bucket=bucket,
            Key=key + ".metadata.json",
            Body=__import__("json").dumps(sidecar).encode("utf-8"),
            ContentType="application/json",
            # ServerSideEncryption=extra["ServerSideEncryption"],
            # **({"SSEKMSKeyId": settings.S3_KMS_KEY_ID} if settings.S3_KMS_KEY_ID else {}),
        )

    await asyncio.to_thread(_put)

    log_event("admin", "info", "S3 upload OK",
              bucket=bucket, key=key, bytes=len(data),
              dept=department_code, meta_keys=list(custom_raw.keys()))

    return {
        "s3_key": key,
        "s3_uri": f"s3://{bucket}/{key}",
        "department_code": department_code,
        "metadata": custom_raw,
        "bytes": len(data),
    }


async def update_kb_metadata(
    *,
    s3_key: str,
    department_code: str,
    filename: str,
    uploader_email: str,
    metadata: Optional[dict] = None,
) -> bool:
    """Rewrite ONLY the Bedrock KB sidecar (`<s3_key>.metadata.json`) for
    an already-uploaded document — no file re-upload, no body fetch.

    Used by the "edit metadata" admin action so re-tagging an existing
    file is cheap. Mirrors the sidecar shape written by
    :func:`upload_kb_document` so retrieval-time filtering keeps working.

    Returns True on success, False on any S3 failure (logged but not
    raised so the caller can still update the DB row and surface the
    partial outcome to the admin).
    """
    bucket = settings.S3_BUCKET_NAME
    dept = (department_code or "").lower()

    custom_raw = {str(k): v for k, v in (metadata or {}).items()
                  if k is not None and v is not None and str(k).strip()}
    custom_sidecar = {
        k: ([str(x) for x in v] if isinstance(v, (list, tuple, set)) else str(v))
        for k, v in custom_raw.items()
    }
    sidecar = {
        "metadataAttributes": {
            "department": dept,
            "uploader": uploader_email,
            "source_filename": filename,
            **custom_sidecar,
        }
    }

    def _put():
        _s3_client().put_object(
            Bucket=bucket,
            Key=s3_key + ".metadata.json",
            Body=__import__("json").dumps(sidecar).encode("utf-8"),
            ContentType="application/json",
        )

    try:
        await asyncio.to_thread(_put)
        log_event("admin", "info", "S3 sidecar updated",
                  bucket=bucket, key=s3_key,
                  dept=department_code, meta_keys=list(custom_raw.keys()))
        return True
    except Exception as e:
        log_event("errors", "error", "Sidecar update failed",
                  bucket=bucket, key=s3_key, error=str(e))
        return False


async def upload_transcript(
    *,
    user_email: str,
    department_code: str,
    content: str,
    suffix: str = "txt",
) -> str:
    """Upload a chat transcript for a ticket; returns the s3 key."""
    bucket = settings.S3_BUCKET_NAME
    safe_email = user_email.replace("@", "_at_").replace("/", "_")
    key = f"transcripts/{department_code.lower()}/{safe_email}_{int(asyncio.get_event_loop().time())}.{suffix}"

    def _put():
        _s3_client().put_object(
            Bucket=bucket,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType="text/plain",
            ServerSideEncryption="AES256",
        )

    try:
        await asyncio.to_thread(_put)
        return key
    except Exception as e:
        log_event("errors", "error", "Transcript upload failed", error=str(e))
        return ""


async def generate_presigned_get(key: str, expires: int = 900) -> Optional[str]:
    """Time-limited URL for a transcript / document download."""
    def _gen():
        return _s3_client().generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.S3_BUCKET_NAME, "Key": key},
            ExpiresIn=expires,
        )
    try:
        return await asyncio.to_thread(_gen)
    except Exception as e:
        log_event("errors", "error", "presign failed", error=str(e))
        return None


async def list_kb_objects(
    department_codes: Optional[list[str]] = None,
    *,
    max_keys: int = 5000,
) -> list[dict]:
    """List KB objects in the configured S3 bucket.

    Returns a list of dicts:
      [{"s3_key": str, "size_bytes": int, "last_modified": datetime,
        "department_code": str, "filename": str}, ...]

    Filters:
      * Skips sidecar `.metadata.json` files.
      * Skips the `transcripts/` prefix (those are ticket transcripts,
        not KB documents).
      * If `department_codes` is provided, only objects under those
        per-department prefixes are returned.

    Soft-fails: returns an empty list when S3 isn't reachable (dev runs
    without AWS creds, network glitches, …). The admin sees the
    DB-tracked rows in that case.
    """
    if not settings.S3_BUCKET_NAME:
        return []

    bucket = settings.S3_BUCKET_NAME
    prefixes = (
        [f"{c.lower()}/" for c in department_codes]
        if department_codes else [""]
    )

    def _list_prefix(prefix: str) -> list[dict]:
        client = _s3_client()
        out: list[dict] = []
        token: Optional[str] = None
        while True:
            kwargs = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
            if token:
                kwargs["ContinuationToken"] = token
            resp = client.list_objects_v2(**kwargs)
            for obj in resp.get("Contents", []) or []:
                key = obj["Key"]
                # Skip sidecars + non-KB prefixes.
                if key.endswith(".metadata.json"):
                    continue
                if key.startswith("transcripts/"):
                    continue
                # Department-prefix convention is "<code>/<filename>".
                # Anything after the first segment is the filename (custom
                # metadata lives in the sidecar, not the path).
                parts = key.split("/", 1)
                if len(parts) < 2 or not parts[1]:
                    continue
                dept_code, filename = parts[0], parts[1]
                out.append({
                    "s3_key": key,
                    "size_bytes": int(obj.get("Size", 0) or 0),
                    "last_modified": obj.get("LastModified"),
                    "department_code": dept_code,
                    "filename": filename,
                })
                if len(out) >= max_keys:
                    return out
            if resp.get("IsTruncated"):
                token = resp.get("NextContinuationToken")
                continue
            return out

    try:
        results: list[dict] = []
        for p in prefixes:
            results.extend(await asyncio.to_thread(_list_prefix, p))
            if len(results) >= max_keys:
                break
        return results[:max_keys]
    except Exception as e:
        log_event("errors", "warning", "S3 list failed", error=str(e))
        return []


async def delete_kb_document(s3_key: str) -> bool:
    """Delete an admin-uploaded KB doc *and* its companion metadata sidecar.

    Returns True on success (or no-op when no creds are configured for dev
    runs), False on AWS failure. Errors are logged but never raised — the
    DB-side soft-delete still needs to proceed so the management UI stays
    consistent.
    """
    if not settings.AWS_ACCESS_KEY_ID and not settings.S3_BUCKET_NAME:
        log_event("admin", "info", "[DEV] S3 delete skipped", key=s3_key)
        return True

    bucket = settings.S3_BUCKET_NAME

    def _delete():
        client = _s3_client()
        client.delete_object(Bucket=bucket, Key=s3_key)
        # Sidecar metadata file written next to each KB upload.
        client.delete_object(Bucket=bucket, Key=s3_key + ".metadata.json")

    try:
        await asyncio.to_thread(_delete)
        log_event("admin", "info", "S3 delete OK", bucket=bucket, key=s3_key)
        return True
    except Exception as e:
        log_event("errors", "error", "S3 delete failed", key=s3_key, error=str(e))
        return False
