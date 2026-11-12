"""Unit tests for the checkpointed, resumable ListRecords harvest engine."""

import json
import sqlite3
from pathlib import Path

import pytest
from sickle.oaiexceptions import BadResumptionToken, NoRecordsMatch

import harvester.checkpoint as checkpoint_module
from harvester.checkpoint import (
    CheckpointError,
    HarvestParameters,
    Page,
    ResumableListHarvest,
    StateLockedError,
    StateMismatchError,
)


class FakeRecord:
    """Minimal record satisfying the engine's HarvestedRecord protocol."""

    def __init__(self, identifier, deleted=False, raw=None):
        self.identifier = identifier
        self.deleted = deleted
        self.raw = raw if raw is not None else f"<record>{identifier}</record>"


def make_parameters(output_file, **overrides):
    defaults = {
        "host": "http://example.invalid/oai",
        "output_file": str(output_file),
        "metadata_format": "oai_dc",
        "from_date": None,
        "until_date": None,
        "set_spec": None,
        "exclude_deleted": False,
    }
    defaults.update(overrides)
    return HarvestParameters(**defaults)


def make_fetcher(pages):
    """Build a fetcher from an ordered mapping of token -> Page.

    The initial query uses token ``None``.  Tokens placed in ``fail_tokens``
    raise the mapped exception when requested instead.
    """
    fail_tokens: dict = {}
    calls: list = []

    def fetcher(token):
        calls.append(token)
        if token in fail_tokens:
            raise fail_tokens[token]
        return pages[token]

    fetcher.fail_tokens = fail_tokens
    fetcher.calls = calls
    return fetcher


def three_page_fetcher():
    pages = {
        None: Page(
            records=(
                FakeRecord("oai:1"),
                FakeRecord("oai:2"),
                FakeRecord("oai:3"),
            ),
            resumption_token="tok-a",
        ),
        "tok-a": Page(
            records=(
                FakeRecord("oai:4"),
                FakeRecord("oai:5", deleted=True),
                FakeRecord("oai:6"),
            ),
            resumption_token="tok-b",
        ),
        "tok-b": Page(
            records=(FakeRecord("oai:7"), FakeRecord("oai:8"), FakeRecord("oai:9")),
            resumption_token=None,
        ),
    }
    return make_fetcher(pages)


def make_harvest(tmp_path, fetcher, *, resume=False, checkpoint_every=3, **params):
    output_file = tmp_path / "output.xml"
    state_dir = tmp_path / "state"
    parameters = make_parameters(output_file.resolve(), **params)
    return (
        ResumableListHarvest(
            state_dir=state_dir,
            output_file=output_file.resolve(),
            parameters=parameters,
            page_fetcher=fetcher,
            checkpoint_every=checkpoint_every,
            resume=resume,
        ),
        output_file,
        state_dir,
    )


def test_full_harvest_writes_ordered_xml_and_completed_state(tmp_path):
    fetcher = three_page_fetcher()
    harvest, output_file, state_dir = make_harvest(tmp_path, fetcher)
    with harvest:
        count = harvest.run()

    assert count == 9
    contents = output_file.read_text(encoding="utf-8")
    assert contents.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<records>\n')
    assert contents.endswith("</records>")
    identifiers = [
        line.split("<record>")[1].split("</record>")[0]
        for line in contents.splitlines()
        if line.startswith("  <record>")
    ]
    assert identifiers == [f"oai:{index}" for index in range(1, 10)]
    manifest = json.loads((state_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"
    assert manifest["record_count"] == 9
    assert not (state_dir / "staging.xml").exists()


def test_output_file_is_absent_when_first_run_fails(tmp_path):
    fetcher = three_page_fetcher()
    fetcher.fail_tokens["tok-a"] = RuntimeError("connection reset")
    harvest, output_file, _state_dir = make_harvest(tmp_path, fetcher)
    with pytest.raises(CheckpointError):
        with harvest:
            harvest.run()
    assert not output_file.exists()
    assert not (tmp_path / ".output.xml.tmp").exists()


def test_resume_after_failure_deduplicates_and_completes(tmp_path):
    first_fetcher = three_page_fetcher()
    first_fetcher.fail_tokens["tok-a"] = RuntimeError("connection reset")
    first_harvest, output_file, state_dir = make_harvest(tmp_path, first_fetcher)
    with pytest.raises(CheckpointError):
        with first_harvest:
            first_harvest.run()

    manifest = json.loads((state_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "in_progress"
    assert manifest["resumption_token"] == "tok-a"
    assert manifest["record_count"] == 3

    second_fetcher = three_page_fetcher()
    second_harvest, _output, _state = make_harvest(tmp_path, second_fetcher, resume=True)
    with second_harvest:
        count = second_harvest.run()

    assert count == 9
    assert second_fetcher.calls[0] == "tok-a"
    contents = output_file.read_text(encoding="utf-8")
    assert contents.count("<record>") == 9
    assert contents.count("oai:3") == 1


def test_bad_resumption_token_restarts_from_initial_query_and_deduplicates(tmp_path):
    first_fetcher = three_page_fetcher()
    first_fetcher.fail_tokens["tok-a"] = RuntimeError("server vanished")
    first_harvest, output_file, state_dir = make_harvest(tmp_path, first_fetcher)
    with pytest.raises(CheckpointError):
        with first_harvest:
            first_harvest.run()

    resume_pages = {
        None: Page(
            records=(
                FakeRecord("oai:1"),
                FakeRecord("oai:2"),
                FakeRecord("oai:3"),
                FakeRecord("oai:4"),
            ),
            resumption_token="tok-new",
        ),
        "tok-new": Page(
            records=(
                FakeRecord("oai:5", deleted=True),
                FakeRecord("oai:6"),
                FakeRecord("oai:7"),
                FakeRecord("oai:8"),
                FakeRecord("oai:9"),
            ),
            resumption_token=None,
        ),
    }
    resume_fetcher = make_fetcher(resume_pages)
    resume_fetcher.fail_tokens["tok-a"] = BadResumptionToken("expired")
    resume_harvest, _output, _state = make_harvest(tmp_path, resume_fetcher, resume=True)
    with resume_harvest:
        count = resume_harvest.run()

    assert count == 9
    assert resume_fetcher.calls == ["tok-a", None, "tok-new"]
    contents = output_file.read_text(encoding="utf-8")
    assert contents.count("<record>") == 9
    manifest = json.loads((state_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"


def test_other_protocol_error_preserves_state_and_fails(tmp_path):
    fetcher = three_page_fetcher()

    class CustomProtocolError(Exception):
        pass

    fetcher.fail_tokens["tok-a"] = CustomProtocolError("cannotDisseminateFormat")
    harvest, output_file, state_dir = make_harvest(tmp_path, fetcher)
    with pytest.raises(CheckpointError):
        with harvest:
            harvest.run()
    assert not output_file.exists()
    manifest = json.loads((state_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "in_progress"
    assert manifest["resumption_token"] == "tok-a"


def test_empty_result_writes_empty_records_document(tmp_path):
    def empty_fetcher(token):
        message = "no records"
        raise NoRecordsMatch(message)

    harvest, output_file, state_dir = make_harvest(tmp_path, empty_fetcher)
    with harvest:
        count = harvest.run()
    assert count == 0
    contents = output_file.read_text(encoding="utf-8")
    assert contents == '<?xml version="1.0" encoding="UTF-8"?>\n<records>\n</records>'
    manifest = json.loads((state_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"


def test_resume_parameter_mismatch_raises_before_changing_state(tmp_path):
    fetcher = three_page_fetcher()
    first_harvest, output_file, state_dir = make_harvest(
        tmp_path, fetcher, metadata_format="oai_dc", set_spec="set-a"
    )
    with first_harvest:
        first_harvest.run()
    original_manifest = (state_dir / "manifest.json").read_text(encoding="utf-8")

    mismatched_fetcher = three_page_fetcher()
    mismatched_harvest, _output, _state = make_harvest(
        tmp_path, mismatched_fetcher, resume=True, set_spec="set-b"
    )
    with pytest.raises(StateMismatchError, match="set_spec"):
        with mismatched_harvest:
            mismatched_harvest.run()
    assert (state_dir / "manifest.json").read_text(encoding="utf-8") == original_manifest
    # The already-published output is left byte-identical.
    assert output_file.exists()


def test_repeated_resume_of_completed_harvest_is_idempotent(tmp_path):
    fetcher = three_page_fetcher()
    first_harvest, output_file, state_dir = make_harvest(tmp_path, fetcher)
    with first_harvest:
        first_harvest.run()
    completed_bytes = output_file.read_bytes()

    second_fetcher = three_page_fetcher()
    second_harvest, _output, _state = make_harvest(tmp_path, second_fetcher, resume=True)
    with second_harvest:
        count = second_harvest.run()

    assert count == 9
    assert output_file.read_bytes() == completed_bytes
    assert second_fetcher.calls == []
    manifest = json.loads((state_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"


def test_existing_state_without_resume_flag_is_rejected(tmp_path):
    fetcher = three_page_fetcher()
    first_harvest, _output, _state = make_harvest(tmp_path, fetcher)
    with first_harvest:
        first_harvest.run()

    second_fetcher = three_page_fetcher()
    second_harvest, _output, _state = make_harvest(tmp_path, second_fetcher)
    with pytest.raises(CheckpointError, match="--resume"):
        with second_harvest:
            second_harvest.run()


def test_resume_without_existing_state_is_rejected(tmp_path):
    fetcher = three_page_fetcher()
    harvest, _output, _state = make_harvest(tmp_path, fetcher, resume=True)
    with pytest.raises(CheckpointError, match="no harvest state"):
        with harvest:
            harvest.run()


def test_concurrent_state_directory_use_is_rejected(tmp_path):
    first_fetcher = three_page_fetcher()
    first_harvest, _output, _state = make_harvest(tmp_path, first_fetcher)
    first_harvest.start()
    try:
        second_fetcher = three_page_fetcher()
        second_harvest, _output, _state = make_harvest(
            tmp_path, second_fetcher, resume=True
        )
        with pytest.raises(StateLockedError):
            second_harvest.start()
    finally:
        first_harvest.close()


def test_lock_is_released_after_crash_and_state_remains_recoverable(tmp_path):
    fetcher = three_page_fetcher()
    harvest, output_file, _state = make_harvest(tmp_path, fetcher)
    harvest.start()
    # Simulate an abnormal process exit: the OS closes the lock file handle
    # without an explicit unlock.
    harvest.lock._file.close()
    harvest._db.close()

    resume_fetcher = three_page_fetcher()
    resume_harvest, _output, _state = make_harvest(tmp_path, resume_fetcher, resume=True)
    with resume_harvest:
        count = resume_harvest.run()
    assert count == 9
    assert output_file.exists()


def test_checkpoint_every_must_be_positive(tmp_path):
    output_file = tmp_path / "output.xml"
    with pytest.raises(ValueError, match="positive integer"):
        ResumableListHarvest(
            state_dir=tmp_path / "state",
            output_file=output_file,
            parameters=make_parameters(output_file),
            page_fetcher=three_page_fetcher(),
            checkpoint_every=0,
            resume=False,
        )


def test_database_holds_records_on_disk_with_duplicate_identifiers(tmp_path):
    pages = {
        None: Page(
            records=(
                FakeRecord("oai:1", raw="<record>first</record>"),
                FakeRecord("oai:2"),
            ),
            resumption_token="tok",
        ),
        "tok": Page(
            records=(
                FakeRecord("oai:1", raw="<record>duplicate-must-be-ignored</record>"),
                FakeRecord("oai:3"),
            ),
            resumption_token=None,
        ),
    }
    fetcher = make_fetcher(pages)
    harvest, output_file, state_dir = make_harvest(tmp_path, fetcher, checkpoint_every=1)
    with harvest:
        count = harvest.run()
    assert count == 3
    with sqlite3.connect(state_dir / "records.sqlite3") as connection:
        rows = connection.execute(
            "SELECT identifier, raw FROM records ORDER BY seq"
        ).fetchall()
    assert rows == [
        ("oai:1", "<record>first</record>"),
        ("oai:2", "<record>oai:2</record>"),
        ("oai:3", "<record>oai:3</record>"),
    ]
    assert "duplicate-must-be-ignored" not in output_file.read_text(encoding="utf-8")


def test_resume_after_crash_during_publication_marks_completed(tmp_path, monkeypatch):
    fetcher = three_page_fetcher()
    harvest, output_file, state_dir = make_harvest(tmp_path, fetcher)
    original_replace = checkpoint_module.os.replace

    def crashing_replace(source, target):
        # Publication moves the verified temp file onto the output path.
        if Path(target).name == "output.xml":
            raise KeyboardInterrupt
        return original_replace(source, target)

    monkeypatch.setattr(checkpoint_module.os, "replace", crashing_replace)
    with pytest.raises(KeyboardInterrupt):
        with harvest:
            harvest.run()
    monkeypatch.setattr(checkpoint_module.os, "replace", original_replace)

    assert not output_file.exists()
    staging = state_dir / "staging.xml"
    assert staging.exists()
    manifest = json.loads((state_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "in_progress"
    assert manifest["staging_sha256"]

    resume_fetcher = three_page_fetcher()
    resume_harvest, _output, _state = make_harvest(tmp_path, resume_fetcher, resume=True)
    with resume_harvest:
        count = resume_harvest.run()
    assert count == 9
    assert output_file.exists()
    assert not staging.exists()
    manifest = json.loads((state_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"


def test_resume_when_output_was_replaced_but_completion_marker_lost(tmp_path):
    fetcher = three_page_fetcher()
    harvest, output_file, state_dir = make_harvest(tmp_path, fetcher)
    harvest.start()
    # Drive the harvest up to a fully-built, checksummed staging file.
    harvest._harvest_pages()
    staging = state_dir / "staging.xml"
    digest, size, count = harvest._build_staging(staging)
    state = harvest._state
    state["resumption_token"] = None
    state["staging_sha256"] = digest
    state["staging_size"] = size
    harvest._write_manifest()
    # Simulate the os.replace publication succeeding, then the process dying
    # before _mark_completed runs.
    harvest._publish(staging, digest, size)
    assert output_file.exists()
    harvest.close()

    resume_fetcher = three_page_fetcher()
    resume_harvest, _output, _state = make_harvest(tmp_path, resume_fetcher, resume=True)
    with resume_harvest:
        resumed_count = resume_harvest.run()
    assert resumed_count == count
    manifest = json.loads((state_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"
    assert not staging.exists()


def test_partial_staging_file_is_discarded_and_rebuilt_on_resume(tmp_path):
    fetcher = three_page_fetcher()
    harvest, output_file, state_dir = make_harvest(tmp_path, fetcher)
    harvest.start()
    harvest._harvest_pages()
    staging = state_dir / "staging.xml"
    staging.write_text("<partial", encoding="utf-8")
    # No checksum is recorded yet, mirroring a crash while writing staging.xml.
    harvest._state["resumption_token"] = None
    harvest._write_manifest()
    harvest.close()

    resume_fetcher = three_page_fetcher()
    resume_harvest, _output, _state = make_harvest(tmp_path, resume_fetcher, resume=True)
    with resume_harvest:
        count = resume_harvest.run()
    assert count == 9
    assert output_file.read_text(encoding="utf-8").endswith("</records>")
