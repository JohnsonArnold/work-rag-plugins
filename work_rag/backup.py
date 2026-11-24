"""Snapshot the database and the immutable blobs it references."""
from contextlib import closing
import os
from pathlib import Path
import shutil
import tempfile

from .contracts import CoreError, ErrorCode
from .filesystem import fsync_dir, publish_directory
from .storage import Store, library_lock


def copy_library(source: Store, destination: str | Path) -> dict:
    """Copy to a new target; every supported publication has a fresh generation.

    Backups retain library identity and all request replay history. A later
    restore uses this same primitive and stamps another generation, so requests
    frozen for the old live target cannot accidentally replay after restoration.
    """
    destination = Path(destination).expanduser().resolve()
    if destination.exists() or destination.is_relative_to(source.data_dir):
        raise CoreError(ErrorCode.INVALID_ARGUMENT, "Choose a new directory outside the library")
    if not destination.parent.is_dir():
        raise CoreError(ErrorCode.INVALID_ARGUMENT, "Destination parent must exist")
    with tempfile.TemporaryDirectory(prefix=".work-rag-copy-", dir=destination.parent) as temporary:
        saved = Store(Path(temporary) / "library")
        with library_lock(source.data_dir), closing(source.connect()) as original, closing(saved.connect()) as snapshot:
            original.backup(snapshot)
            hashes = snapshot.execute(
                """SELECT sha256,size FROM source_versions
                UNION SELECT sha256,size FROM source_attachments WHERE sha256 IS NOT NULL"""
            ).fetchall()
        for row in hashes:
            original_path = source.verify_blob(row["sha256"], row["size"])
            target = saved.blob_path(row["sha256"])
            shutil.copyfile(original_path, target)
            saved.verify_blob(row["sha256"], row["size"])
            with target.open("r+b") as handle:
                os.fsync(handle.fileno())
            target.chmod(0o444)
        saved.renew_restore_generation()
        report = saved.recovery_report()
        if report["sqlite_integrity"] != "ok" or report["foreign_key_errors"]:
            raise CoreError(ErrorCode.INTEGRITY, "Copied database failed integrity checks")
        with saved.db_path.open("r+b") as database:
            os.fsync(database.fileno())
        fsync_dir(saved.blobs)
        fsync_dir(saved.data_dir)
        # The sibling lock survives publication and is shared with bridge/core
        # writes. Recheck existence under it: a bridge may have initialized the
        # target while the backup was being staged.
        with library_lock(destination):
            if destination.exists():
                raise CoreError(ErrorCode.INVALID_ARGUMENT, "Destination already exists")
            try:
                publish_directory(saved.data_dir, destination)
            except FileExistsError as error:
                raise CoreError(ErrorCode.INVALID_ARGUMENT, "Destination already exists") from error
            fsync_dir(destination.parent)
    return {"directory": str(destination), "blobs": len(hashes)}
