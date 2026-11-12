# Boolean flags appear in the checkpointed-harvest engine by design.
# ruff: noqa: FBT001

"""Resumable, checkpointed OAI-PMH ListRecords harvests.

A harvest run persists its progress inside a local *state directory* so that a
new process can resume the same harvest after an interruption.  The directory
contains:

``manifest.json``
    JSON document describing the harvest parameters (source URL, absolute
    output path, metadata format, date range, set, deleted-record filter),
    the resumption token to continue with, the run status, and the checksum
    of the staged output document.

``records.sqlite3``
    SQLite database holding every harvested record keyed by OAI identifier.
    It is the on-disk deduplication index, so neither the full record set nor
    all identifiers need to be held in memory, and it preserves the order in
    which identifiers were first seen.

``staging.xml``
    The complete output document, streamed from SQLite.  It is only copied to
    the final output path once the whole harvest has succeeded.

``harvest.lock``
    An OS byte-range lock held for the duration of a run.  It prevents two
    processes from using the same state directory concurrently and is
    released automatically by the operating system if a process dies, which
    makes a crashed run recoverable.
"""

import contextlib
import hashlib
import json
import logging
import os
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Protocol, Self

from sickle.oaiexceptions import BadResumptionToken, NoRecordsMatch

if os.name == "nt":
    import msvcrt
else:
    import fcntl  # type: ignore[import-not-found]

logger = logging.getLogger(__name__)

XML_PREFIX = '<?xml version="1.0" encoding="UTF-8"?>\n<records>\n'
XML_SUFFIX = "</records>"
MANIFEST_NAME = "manifest.json"
DATABASE_NAME = "records.sqlite3"
STAGING_NAME = "staging.xml"
LOCK_NAME = "harvest.lock"
SCHEMA_VERSION = 1

_STATUS_IN_PROGRESS = "in_progress"
_STATUS_COMPLETED = "completed"


class CheckpointError(Exception):
    """Base exception for resumable harvest state and protocol failures."""


class StateLockedError(CheckpointError):
    """Raised when another process already holds the state directory lock."""


class StateMismatchError(CheckpointError):
    """Raised when resume parameters do not match the saved state."""


class StateFormatError(CheckpointError):
    """Raised when the persisted state cannot be read."""


@dataclass(frozen=True)
class HarvestedRecord(Protocol):
    """Structural type for the record objects consumed by the engine."""

    identifier: str | None
    deleted: bool
    raw: str


@dataclass(frozen=True)
class HarvestParameters:
    """Harvest-defining parameters that must stay stable across resumes."""

    host: str
    output_file: str
    metadata_format: str | None
    from_date: str | None
    until_date: str | None
    set_spec: str | None
    exclude_deleted: bool

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of the parameters."""
        return {
            "host": self.host,
            "output_file": self.output_file,
            "metadata_format": self.metadata_format,
            "from_date": self.from_date,
            "until_date": self.until_date,
            "set_spec": self.set_spec,
            "exclude_deleted": self.exclude_deleted,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HarvestParameters":
        """Build parameters from a persisted JSON dictionary."""
        try:
            return cls(
                host=data["host"],
                output_file=data["output_file"],
                metadata_format=data.get("metadata_format"),
                from_date=data.get("from_date"),
                until_date=data.get("until_date"),
                set_spec=data.get("set_spec"),
                exclude_deleted=bool(data["exclude_deleted"]),
            )
        except KeyError as exception:
            message = f"Manifest is missing required parameter: {exception.args[0]}"
            raise StateFormatError(message) from exception


@dataclass(frozen=True)
class Page:
    """One ListRecords page: the records it carries and the next token."""

    records: tuple[HarvestedRecord, ...]
    resumption_token: str | None


# A page fetcher takes the resumption token of the next page (``None`` for the
# initial query) and returns the parsed page.
PageFetcher = Callable[[str | None], Page]


class HarvestLock:
    """Cross-process, non-blocking byte-range lock for a state directory."""

    def __init__(self, state_dir: Path) -> None:
        self._path = state_dir / LOCK_NAME
        self._file: IO[bytes] | None = None

    def acquire(self) -> None:
        """Acquire the lock, raising :class:`StateLockedError` if held.

        The lock is a one-byte range lock (Windows ``msvcrt`` or POSIX
        ``fcntl``).  The operating system releases it automatically when the
        holding process exits, including after a crash.
        """
        lock_file = open(self._path, "a+b")  # noqa: SIM115
        lock_file.write(b"x")
        lock_file.flush()
        lock_file.seek(0)
        try:
            self._lock_file_descriptor(lock_file.fileno())
        except OSError as exception:
            lock_file.close()
            message = (
                f"State directory {self._path.parent} is already in use by "
                "another harvest process"
            )
            raise StateLockedError(message) from exception
        self._file = lock_file

    @staticmethod
    def _lock_file_descriptor(file_descriptor: int) -> None:
        if os.name == "nt":
            msvcrt.locking(file_descriptor, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(  # type: ignore[attr-defined]
                file_descriptor,
                fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
            )

    @staticmethod
    def _unlock_file_descriptor(file_descriptor: int) -> None:
        if os.name == "nt":
            msvcrt.locking(file_descriptor, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(  # type: ignore[attr-defined]
                file_descriptor,
                fcntl.LOCK_UN,  # type: ignore[attr-defined]
            )

    def release(self) -> None:
        """Release the lock if currently held by this instance."""
        lock_file = self._file
        if lock_file is None:
            return
        with contextlib.suppress(OSError):
            lock_file.seek(0)
            self._unlock_file_descriptor(lock_file.fileno())
        lock_file.close()
        self._file = None

    def __enter__(self) -> Self:
        """Acquire the lock when entering a ``with`` block."""
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        """Release the lock when leaving a ``with`` block."""
        self.release()


class ResumableListHarvest:
    """Orchestrates a checkpointed, resumable ListRecords harvest.

    Args:
        state_dir: Directory used for all persisted state.
        output_file: Absolute final output path (local filesystem only).
        parameters: Harvest parameters to persist and verify on resume.
        page_fetcher: Callable returning the next :class:`Page` for a token.
        checkpoint_every: Save progress at least once per this many new
            records; must be a positive integer.
        resume: Whether an existing state directory should be resumed.
    """

    def __init__(
        self,
        state_dir: Path,
        output_file: Path,
        parameters: HarvestParameters,
        page_fetcher: PageFetcher,
        checkpoint_every: int,
        resume: bool,
    ) -> None:
        if checkpoint_every < 1:
            message = "checkpoint_every must be a positive integer"
            raise ValueError(message)
        self.state_dir = Path(state_dir)
        self.output_file = Path(output_file)
        self.parameters = parameters
        self.page_fetcher = page_fetcher
        self.checkpoint_every = checkpoint_every
        self.resume = resume
        self.lock = HarvestLock(self.state_dir)
        self._db: sqlite3.Connection | None = None
        self._manifest: dict[str, Any] | None = None

    # ------------------------------------------------------------------ setup
    def _manifest_path(self) -> Path:
        return self.state_dir / MANIFEST_NAME

    @property
    def _state(self) -> dict[str, Any]:
        """Return the in-memory manifest, requiring :meth:`start` first."""
        if self._manifest is None:
            message = "Harvest has not been started"
            raise CheckpointError(message)
        return self._manifest

    @property
    def _db_conn(self) -> sqlite3.Connection:
        """Return the database connection, requiring :meth:`start` first."""
        if self._db is None:
            message = "Harvest has not been started"
            raise CheckpointError(message)
        return self._db

    def _db_path(self) -> Path:
        return self.state_dir / DATABASE_NAME

    def _staging_path(self) -> Path:
        return self.state_dir / STAGING_NAME

    def start(self) -> None:
        """Acquire the lock and open or validate the persisted state.

        Parameter mismatches and unreadable state are reported before any
        output is written or existing state is modified.
        """
        manifest_path = self._manifest_path()
        existing_state = manifest_path.exists()
        if existing_state and not self.resume:
            message = (
                f"State directory {self.state_dir} already contains a harvest; "
                "pass --resume to continue it"
            )
            raise CheckpointError(message)
        if not existing_state and self.resume:
            message = f"Cannot resume: no harvest state found in {self.state_dir}"
            raise CheckpointError(message)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.lock.acquire()
        if existing_state:
            self._manifest = self._load_manifest(manifest_path)
            self._verify_parameters()
        else:
            self._manifest = self._fresh_manifest()
            self._write_manifest()
        self._open_database(existing_state)

    def _fresh_manifest(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "parameters": self.parameters.as_dict(),
            "resumption_token": None,
            "status": _STATUS_IN_PROGRESS,
            "record_count": 0,
            "staging_sha256": None,
            "staging_size": None,
        }

    def _load_manifest(self, manifest_path: Path) -> dict[str, Any]:
        try:
            with open(manifest_path, encoding="utf-8") as file:
                manifest = json.load(file)
        except (OSError, json.JSONDecodeError) as exception:
            message = f"Cannot read harvest manifest {manifest_path}: {exception}"
            raise StateFormatError(message) from exception
        if not isinstance(manifest, dict) or "parameters" not in manifest:
            message = f"Harvest manifest {manifest_path} is not valid"
            raise StateFormatError(message)
        return manifest

    @staticmethod
    def _same_path(left: str, right: str) -> bool:
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
            os.path.abspath(right)
        )

    def _verify_parameters(self) -> None:
        saved = HarvestParameters.from_dict(self._state["parameters"])
        mismatches: list[str] = []
        current = self.parameters
        if saved.host != current.host:
            mismatches.append(f"host: {saved.host!r} != {current.host!r}")
        if not self._same_path(saved.output_file, current.output_file):
            mismatches.append(
                f"output_file: {saved.output_file!r} != {current.output_file!r}"
            )
        if saved.metadata_format != current.metadata_format:
            mismatches.append(
                f"metadata_format: {saved.metadata_format!r} != "
                f"{current.metadata_format!r}"
            )
        if saved.from_date != current.from_date:
            mismatches.append(f"from_date: {saved.from_date!r} != {current.from_date!r}")
        if saved.until_date != current.until_date:
            mismatches.append(
                f"until_date: {saved.until_date!r} != {current.until_date!r}"
            )
        if saved.set_spec != current.set_spec:
            mismatches.append(f"set_spec: {saved.set_spec!r} != {current.set_spec!r}")
        if saved.exclude_deleted != current.exclude_deleted:
            mismatches.append(
                f"exclude_deleted: {saved.exclude_deleted!r} != "
                f"{current.exclude_deleted!r}"
            )
        if mismatches:
            message = (
                "Resume parameters do not match the saved harvest state: "
                + "; ".join(mismatches)
            )
            raise StateMismatchError(message)

    def _open_database(self, existing_state: bool) -> None:
        try:
            self._db = sqlite3.connect(self._db_path())
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS records (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    identifier TEXT NOT NULL UNIQUE,
                    raw TEXT NOT NULL,
                    deleted INTEGER NOT NULL
                )
                """
            )
            self._db.commit()
        except sqlite3.Error as exception:
            message = f"Cannot open harvest database {self._db_path()}: {exception}"
            raise CheckpointError(message) from exception
        if existing_state:
            self._state["record_count"] = self._count_records()

    def close(self) -> None:
        """Close the database connection and release the lock."""
        if self._db is not None:
            with contextlib.suppress(sqlite3.Error):
                self._db.close()
            self._db = None
        self.lock.release()

    def __enter__(self) -> Self:
        """Start the harvest when entering a ``with`` block."""
        self.start()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        """Release the lock and close the database when leaving the block."""
        self.close()

    # ------------------------------------------------------------------- run
    def run(self) -> int:
        """Execute or resume the harvest and return the record count."""
        if self._state["status"] == _STATUS_COMPLETED:
            return self._handle_completed()
        staging = self._staging_path()
        recorded_digest = self._state.get("staging_sha256")
        if staging.exists() and recorded_digest:
            if self._sha256_of(staging) == recorded_digest:
                # The complete document was built (and possibly published) but
                # the completion marker was not written.
                return self._resume_finalization()
            # A partial staging file left by a crash while building it cannot
            # be trusted; discard it and rebuild from the saved records.
            logger.warning(
                "Discarding incomplete staging document %s; it will be rebuilt",
                staging,
            )
            staging.unlink()
        self._harvest_pages()
        return self._finalize()

    def _harvest_pages(self) -> None:
        token = self._state["resumption_token"]
        new_records = 0
        while True:
            try:
                page = self.page_fetcher(token)
            except BadResumptionToken:
                if token is None:
                    raise
                logger.warning(
                    "Server rejected resumption token %r; restarting the "
                    "harvest from the initial query and deduplicating against "
                    "saved records",
                    token,
                )
                token = None
                continue
            except NoRecordsMatch:
                # A legal empty result (or an empty restart after an expired
                # token) simply ends the harvest.
                logger.info("Server reported no records match; ending harvest")
                break
            except CheckpointError:
                raise
            except Exception as exception:
                # Preserve everything stored so far, then surface a single
                # failure type to the CLI while keeping the saved state usable.
                self._db_conn.commit()
                message = (
                    "Harvest interrupted while fetching records; progress is "
                    f"saved in {self.state_dir} and can be resumed with --resume: "
                    f"{type(exception).__name__}: {exception}"
                )
                raise CheckpointError(message) from exception
            for record in page.records:
                if self._store_record(record):
                    new_records += 1
                    if new_records >= self.checkpoint_every:
                        self._checkpoint(token)
                        new_records = 0
            token = page.resumption_token
            # Always persist at page boundaries so the next page is never
            # re-requested needlessly and the token stays current.
            self._checkpoint(token)
            if token is None:
                break

    def _store_record(self, record: HarvestedRecord) -> bool:
        """Persist one record; return True if the identifier is new."""
        identifier = record.identifier
        if not identifier:
            message = "Received a record without an identifier"
            raise CheckpointError(message)
        try:
            cursor = self._db_conn.execute(
                "INSERT OR IGNORE INTO records (identifier, raw, deleted) "
                "VALUES (?, ?, ?)",
                (identifier, record.raw, 1 if record.deleted else 0),
            )
        except sqlite3.Error as exception:
            message = f"Failed to persist record {identifier}: {exception}"
            raise CheckpointError(message) from exception
        return cursor.rowcount == 1

    def _checkpoint(self, resumption_token: str | None) -> None:
        self._db_conn.commit()
        count = self._count_records()
        state = self._state
        state["resumption_token"] = resumption_token
        state["record_count"] = count
        self._write_manifest()
        logger.info(
            "Checkpoint saved: %d unique record(s) stored in %s",
            count,
            self.state_dir,
        )

    def _count_records(self) -> int:
        with contextlib.closing(
            self._db_conn.execute("SELECT COUNT(*) FROM records")
        ) as cursor:
            row = cursor.fetchone()
        return int(row[0])

    # ------------------------------------------------------------- finalizing
    def _finalize(self) -> int:
        state = self._state
        staging = self._staging_path()
        staging.unlink(missing_ok=True)
        digest, size, count = self._build_staging(staging)
        state["resumption_token"] = None
        state["record_count"] = count
        state["staging_sha256"] = digest
        state["staging_size"] = size
        self._write_manifest()
        self._publish(staging, digest, size)
        return self._mark_completed(staging, count)

    def _build_staging(self, staging: Path) -> tuple[str, int, int]:
        digest = hashlib.sha256()
        size = 0

        def write(chunk: bytes) -> None:
            nonlocal size
            staging_file.write(chunk)
            digest.update(chunk)
            size += len(chunk)

        with open(staging, "wb") as staging_file:
            write(XML_PREFIX.encode("utf-8"))
            with contextlib.closing(
                self._db_conn.execute("SELECT raw FROM records ORDER BY seq")
            ) as cursor:
                for (raw,) in cursor:
                    write(b"  " + raw.encode("utf-8") + b"\n")
            write(XML_SUFFIX.encode("utf-8"))
        return digest.hexdigest(), size, self._count_records()

    def _publish(self, staging: Path, digest: str, size: int) -> None:
        """Atomically place the complete document at the output path."""
        output = self.output_file
        temp_name = f".{output.name}.tmp"
        temp_path = output.with_name(temp_name)
        try:
            with open(staging, "rb") as source, open(temp_path, "wb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
            self._verify_published(temp_path, digest, size)
            os.replace(temp_path, output)
        except CheckpointError:
            with contextlib.suppress(OSError):
                temp_path.unlink()
            raise
        except OSError as exception:
            with contextlib.suppress(OSError):
                temp_path.unlink()
            message = f"Failed to publish output to {output}: {exception}"
            raise CheckpointError(message) from exception
        logger.info("Published complete harvest output to %s", output)

    @staticmethod
    def _verify_published(temp_path: Path, digest: str, size: int) -> None:
        """Checksum and size check for a document awaiting publication."""
        published_digest = hashlib.sha256()
        with open(temp_path, "rb") as published:
            while True:
                chunk = published.read(1024 * 1024)
                if not chunk:
                    break
                published_digest.update(chunk)
        if published_digest.hexdigest() != digest:
            message = (
                "Verification of staged output failed before publication: "
                "checksum mismatch"
            )
            raise CheckpointError(message)
        if temp_path.stat().st_size != size:
            message = (
                "Verification of staged output failed before publication: size mismatch"
            )
            raise CheckpointError(message)

    def _mark_completed(self, staging: Path, count: int) -> int:
        self._state["status"] = _STATUS_COMPLETED
        self._state["record_count"] = count
        self._write_manifest()
        staging.unlink(missing_ok=True)
        logger.info("Harvest marked completed with %d record(s)", count)
        return count

    def _resume_finalization(self) -> int:
        state = self._state
        staging = self._staging_path()
        count = self._count_records()
        digest = state.get("staging_sha256")
        if not staging.exists() or not digest:
            message = (
                "Previous run ended during publication but the complete staged "
                f"document {staging} is missing"
            )
            raise CheckpointError(message)
        if self.output_file.exists() and self._sha256_of(self.output_file) == digest:
            # The replace already happened; only the completion marker was lost.
            return self._mark_completed(staging, count)
        self._publish(staging, digest, staging.stat().st_size)
        return self._mark_completed(staging, count)

    def _handle_completed(self) -> int:
        state = self._state
        count = int(state["record_count"])
        digest = state.get("staging_sha256")
        if (
            self.output_file.exists()
            and digest
            and self._sha256_of(self.output_file) == digest
        ):
            logger.info(
                "Harvest for %s already completed; output unchanged", self.output_file
            )
            return count
        logger.warning(
            "Completed harvest output %s is missing or changed; rebuilding it "
            "from saved state",
            self.output_file,
        )
        staging = self._staging_path()
        rebuilt_digest, rebuilt_size, count = self._build_staging(staging)
        if digest and rebuilt_digest != digest:
            message = (
                "Rebuilt output does not match the recorded completed-harvest checksum"
            )
            raise CheckpointError(message)
        self._publish(staging, rebuilt_digest, rebuilt_size)
        return self._mark_completed(staging, count)

    @staticmethod
    def _sha256_of(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as file:
            while True:
                chunk = file.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def _write_manifest(self) -> None:
        state = self._state
        manifest_path = self._manifest_path()
        temp_path = manifest_path.with_name(f".{MANIFEST_NAME}.tmp")
        try:
            with open(temp_path, "w", encoding="utf-8") as file:
                json.dump(state, file, ensure_ascii=False, indent=2)
                file.write("\n")
            os.replace(temp_path, manifest_path)
        except OSError as exception:
            with contextlib.suppress(OSError):
                temp_path.unlink()
            message = f"Failed to write harvest manifest {manifest_path}: {exception}"
            raise CheckpointError(message) from exception
