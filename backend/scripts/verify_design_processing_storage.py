from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass

from storage3.exceptions import StorageApiError

from backend.app.config import settings
from backend.app.services.storage_ingest import upload_with_retry
from backend.app.supabase_client import supabase


PROBE_ITEM_ID = "phase1-storage-probe"
PROBE_FILENAME = "phase1_storage_probe.txt"
PROBE_CONTENT = b"design-processing-storage-round-trip-v1\n"


class StorageVerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class StorageProbeResult:
    bucket: str
    object_key: str
    content_sha256: str


def build_design_processing_object_key(
    *,
    board_id: str,
    item_id: str,
    input_revision: str,
    pipeline_digest: str,
    filename: str,
) -> str:
    if not all((board_id, item_id, input_revision, pipeline_digest, filename)):
        raise ValueError("Design-processing object-key components must not be empty")
    if "/" in filename:
        raise ValueError("Design-processing artifact filename must not contain a slash")
    return (
        f"design-processing/{board_id}/{item_id}/{input_revision}/"
        f"{pipeline_digest}/{filename}"
    )


def _get_private_bucket(bucket_name: str, *, create_bucket: bool):
    try:
        bucket = supabase.storage.get_bucket(bucket_name)
    except StorageApiError as exc:
        if str(exc.status) != "404" or not create_bucket:
            raise StorageVerificationError(
                f"Unable to load artifact bucket {bucket_name}: {exc}"
            ) from exc
        supabase.storage.create_bucket(
            bucket_name,
            name=bucket_name,
            options={"public": False},
        )
        bucket = supabase.storage.get_bucket(bucket_name)

    if bucket.public:
        raise StorageVerificationError(
            f"Artifact bucket {bucket_name} is public; a private bucket is required"
        )
    return bucket


def verify_private_storage_round_trip(*, create_bucket: bool = False) -> StorageProbeResult:
    bucket_name = settings.design_processing_artifact_bucket
    _get_private_bucket(bucket_name, create_bucket=create_bucket)

    content_sha256 = hashlib.sha256(PROBE_CONTENT).hexdigest()
    object_key = build_design_processing_object_key(
        board_id=settings.design_processing_board_id,
        item_id=PROBE_ITEM_ID,
        input_revision=content_sha256,
        pipeline_digest=settings.design_processing_pipeline_digest,
        filename=PROBE_FILENAME,
    )
    bucket = supabase.storage.from_(bucket_name)
    uploaded = False
    try:
        upload_with_retry(
            bucket_name,
            object_key,
            PROBE_CONTENT,
            "text/plain",
        )
        uploaded = True
        downloaded = bucket.download(object_key)
        downloaded_sha256 = hashlib.sha256(downloaded).hexdigest()
        if downloaded_sha256 != content_sha256:
            raise StorageVerificationError(
                f"Storage round-trip hash mismatch: expected {content_sha256}, "
                f"got {downloaded_sha256}"
            )
    finally:
        if uploaded:
            bucket.remove([object_key])

    return StorageProbeResult(
        bucket=bucket_name,
        object_key=object_key,
        content_sha256=content_sha256,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify the private design-processing artifact storage round trip."
    )
    parser.add_argument(
        "--create-bucket",
        action="store_true",
        help="Create the configured bucket as private when it does not exist.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        result = verify_private_storage_round_trip(create_bucket=args.create_bucket)
    except StorageVerificationError as exc:
        print(f"Design-processing storage verification failed: {exc}")
        return 1

    print(
        f"Verified private bucket {result.bucket} with reversible object "
        f"{result.object_key} (sha256 {result.content_sha256})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())