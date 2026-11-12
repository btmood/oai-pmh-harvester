"""CLI tests for the resumable (--state-dir) ListRecords harvest.

The end-to-end tests drive the real Click CLI against a locally-started copy
of ``trial/mock_oai.py`` (loopback only, no external network), which provides
pagination, deleted records, Chinese/accented content, token expiry and
one-shot protocol failures.
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen
from xml.etree import ElementTree as ET

import pytest
from click.testing import CliRunner

from harvester.cli import main

REPO_ROOT = Path(__file__).resolve().parents[1]
MOCK_SERVER = REPO_ROOT / "trial" / "mock_oai.py"
OAI_NS = "{http://www.openarchives.org/OAI/2.0/}"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def mock_server_factory():
    processes = []

    def start(port=None):
        if port is None:
            port = _free_port()
        env = dict(os.environ)
        env.update({"SENTRY_DSN": "none", "WORKSPACE": "test", "PYTHONUTF8": "1"})
        process = subprocess.Popen(
            [
                sys.executable,
                str(MOCK_SERVER),
                "--port",
                str(port),
                "--page-size",
                "3",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        processes.append(process)
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                    if response.status == 200:
                        return f"http://127.0.0.1:{port}", process
            except OSError:
                time.sleep(0.1)
        message = "mock OAI server did not become ready"
        raise RuntimeError(message)

    yield start
    for process in processes:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture
def mock_server(mock_server_factory):
    host, _process = mock_server_factory()
    return host


def _invoke(runner, host, output_file, *harvest_args):
    return runner.invoke(
        main, ["-h", host, "-o", str(output_file), "harvest", *harvest_args]
    )


def _record_identifiers(output_file: Path) -> list[str]:
    tree = ET.parse(output_file)
    root = tree.getroot()
    return [
        node.find(f"{OAI_NS}header/{OAI_NS}identifier").text
        for node in root.findall(OAI_NS + "record")
    ]


def test_resumable_harvest_end_to_end(mock_server, tmp_path):
    runner = CliRunner()
    output_file = tmp_path / "output.xml"
    state_dir = tmp_path / "state"
    result = _invoke(
        runner,
        f"{mock_server}/oai",
        output_file,
        "--method",
        "list",
        "--state-dir",
        str(state_dir),
        "--checkpoint-every",
        "3",
    )
    assert result.exit_code == 0, result.output
    identifiers = _record_identifiers(output_file)
    assert len(identifiers) == 9
    assert identifiers == [f"oai:trial:{index:03d}" for index in range(1, 10)]
    # Non-ASCII content survives the UTF-8 round trip.
    text = output_file.read_text(encoding="utf-8")
    assert "河流观测" in text
    assert "Café observations" in text
    assert "Δ water quality" in text
    manifest = json.loads((state_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"
    assert manifest["parameters"]["host"] == f"{mock_server}/oai"


def test_resume_after_failure_against_mock_server(mock_server, tmp_path):
    runner = CliRunner()
    output_file = tmp_path / "output.xml"
    state_dir = tmp_path / "state"
    failed = _invoke(
        runner,
        f"{mock_server}/oai/fail-once",
        output_file,
        "--method",
        "list",
        "--state-dir",
        str(state_dir),
        "--checkpoint-every",
        "3",
    )
    assert failed.exit_code != 0
    assert not output_file.exists()

    resumed = _invoke(
        runner,
        f"{mock_server}/oai/fail-once",
        output_file,
        "--method",
        "list",
        "--state-dir",
        str(state_dir),
        "--checkpoint-every",
        "3",
        "--resume",
    )
    assert resumed.exit_code == 0, resumed.output
    assert len(_record_identifiers(output_file)) == 9


def test_expired_token_resume_restarts_and_deduplicates(mock_server_factory, tmp_path):
    runner = CliRunner()
    output_file = tmp_path / "output.xml"
    state_dir = tmp_path / "state"
    fixed_port = _free_port()
    first_host, first_process = mock_server_factory(fixed_port)

    # Run until the first page is checkpointed with a server-issued token,
    # then force the run to fail on the next page request.
    failed = _invoke(
        runner,
        f"{first_host}/oai/fail-once",
        output_file,
        "--method",
        "list",
        "--state-dir",
        str(state_dir),
        "--checkpoint-every",
        "3",
    )
    assert failed.exit_code != 0
    saved_token = json.loads((state_dir / "manifest.json").read_text(encoding="utf-8"))[
        "resumption_token"
    ]
    assert saved_token

    # Restarting the fixture on the same port invalidates every previously
    # issued token while keeping the saved host parameter valid.
    first_process.terminate()
    first_process.wait(timeout=10)
    second_host, _second_process = mock_server_factory(fixed_port)
    assert second_host == first_host

    resume_args = [
        "--method",
        "list",
        "--state-dir",
        str(state_dir),
        "--checkpoint-every",
        "3",
        "--resume",
    ]
    # First resume: the saved token is rejected (badResumptionToken), so the
    # harvest restarts from the initial query and deduplicates against the
    # saved records.  The restarted fixture re-arms its one-shot fault, which
    # is a different protocol error and must preserve state for another resume.
    first_resume = _invoke(
        runner, f"{second_host}/oai/fail-once", output_file, *resume_args
    )
    assert first_resume.exit_code != 0
    assert not output_file.exists()

    # Second resume: the one-shot fault is consumed; remaining pages flow
    # through and duplicate identifiers are ignored.
    second_resume = _invoke(
        runner, f"{second_host}/oai/fail-once", output_file, *resume_args
    )
    assert second_resume.exit_code == 0, second_resume.output
    identifiers = _record_identifiers(output_file)
    assert len(identifiers) == 9
    assert len(set(identifiers)) == 9
    assert identifiers == [f"oai:trial:{index:03d}" for index in range(1, 10)]
    manifest = json.loads((state_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"


def test_empty_result_is_a_successful_empty_document(mock_server, tmp_path):
    runner = CliRunner()
    output_file = tmp_path / "output.xml"
    state_dir = tmp_path / "state"
    result = _invoke(
        runner,
        f"{mock_server}/oai/empty",
        output_file,
        "--method",
        "list",
        "--state-dir",
        str(state_dir),
    )
    assert result.exit_code == 0, result.output
    assert (
        output_file.read_text(encoding="utf-8")
        == '<?xml version="1.0" encoding="UTF-8"?>\n<records>\n</records>'
    )


def test_exclude_deleted_filter_is_persisted_and_applied(mock_server, tmp_path):
    runner = CliRunner()
    include_output = tmp_path / "all.xml"
    include_state = tmp_path / "state-all"
    include = _invoke(
        runner,
        f"{mock_server}/oai",
        include_output,
        "--method",
        "list",
        "--state-dir",
        str(include_state),
    )
    assert include.exit_code == 0, include.output
    assert len(_record_identifiers(include_output)) == 9

    excluded_output = tmp_path / "kept.xml"
    excluded_state = tmp_path / "state-kept"
    excluded = _invoke(
        runner,
        f"{mock_server}/oai",
        excluded_output,
        "--method",
        "list",
        "--state-dir",
        str(excluded_state),
        "--exclude-deleted",
    )
    assert excluded.exit_code == 0, excluded.output
    identifiers = _record_identifiers(excluded_output)
    assert len(identifiers) == 8
    assert "oai:trial:005" not in identifiers
    manifest = json.loads((excluded_state / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["parameters"]["exclude_deleted"] is True


def test_resume_parameter_mismatch_is_a_usage_error(mock_server, tmp_path):
    runner = CliRunner()
    output_file = tmp_path / "output.xml"
    state_dir = tmp_path / "state"
    first = _invoke(
        runner,
        f"{mock_server}/oai",
        output_file,
        "--method",
        "list",
        "--state-dir",
        str(state_dir),
        "-m",
        "oai_dc",
    )
    assert first.exit_code == 0, first.output
    completed_bytes = output_file.read_bytes()

    mismatched = _invoke(
        runner,
        f"{mock_server}/oai",
        output_file,
        "--method",
        "list",
        "--state-dir",
        str(state_dir),
        "-m",
        "some_other_format",
        "--resume",
    )
    assert mismatched.exit_code != 0
    assert "do not match" in mismatched.output
    assert output_file.read_bytes() == completed_bytes


def test_existing_state_requires_resume_flag(mock_server, tmp_path):
    runner = CliRunner()
    output_file = tmp_path / "output.xml"
    state_dir = tmp_path / "state"
    first = _invoke(
        runner,
        f"{mock_server}/oai",
        output_file,
        "--method",
        "list",
        "--state-dir",
        str(state_dir),
    )
    assert first.exit_code == 0
    second = _invoke(
        runner,
        f"{mock_server}/oai",
        output_file,
        "--method",
        "list",
        "--state-dir",
        str(state_dir),
    )
    assert second.exit_code != 0
    assert "--resume" in second.output


def test_repeated_resume_of_completed_harvest_succeeds_and_keeps_output(
    mock_server, tmp_path
):
    runner = CliRunner()
    output_file = tmp_path / "output.xml"
    state_dir = tmp_path / "state"
    args = [
        "--method",
        "list",
        "--state-dir",
        str(state_dir),
        "--checkpoint-every",
        "3",
    ]
    first = _invoke(runner, f"{mock_server}/oai", output_file, *args)
    assert first.exit_code == 0, first.output
    completed_bytes = output_file.read_bytes()

    resumed = _invoke(runner, f"{mock_server}/oai", output_file, *args, "--resume")
    assert resumed.exit_code == 0, resumed.output
    assert output_file.read_bytes() == completed_bytes
    manifest = json.loads((state_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"


def test_state_dir_rejected_for_get_method(tmp_path):
    runner = CliRunner()
    result = _invoke(
        runner,
        "http://example.invalid/oai",
        tmp_path / "output.xml",
        "--method",
        "get",
        "--state-dir",
        str(tmp_path / "state"),
    )
    assert result.exit_code != 0
    assert "--method=list" in result.output
    assert not (tmp_path / "state").exists()


def test_state_dir_rejected_for_s3_output(tmp_path):
    runner = CliRunner()
    result = _invoke(
        runner,
        "http://example.invalid/oai",
        "s3://bucket/output.xml",
        "--method",
        "list",
        "--state-dir",
        str(tmp_path / "state"),
    )
    assert result.exit_code != 0
    assert "local output file" in result.output


def test_resume_without_state_dir_is_rejected(tmp_path):
    runner = CliRunner()
    result = _invoke(
        runner,
        "http://example.invalid/oai",
        tmp_path / "output.xml",
        "--method",
        "list",
        "--resume",
    )
    assert result.exit_code != 0
    assert "--resume requires --state-dir" in result.output


def test_checkpoint_every_must_be_positive(tmp_path):
    runner = CliRunner()
    result = _invoke(
        runner,
        "http://example.invalid/oai",
        tmp_path / "output.xml",
        "--method",
        "list",
        "--state-dir",
        str(tmp_path / "state"),
        "--checkpoint-every",
        "0",
    )
    assert result.exit_code != 0
    assert "positive integer" in result.output
