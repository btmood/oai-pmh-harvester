# ruff: noqa: FBT001

"""harvester.cli module."""

import json
import logging
import sys
from datetime import timedelta
from pathlib import Path
from time import perf_counter
from typing import Literal

import click
from sickle.oaiexceptions import NoRecordsMatch

from harvester.checkpoint import (
    CheckpointError,
    HarvestParameters,
    Page,
    ResumableListHarvest,
)
from harvester.config import Config
from harvester.oai import OAIClient, write_records, write_sets

logger = logging.getLogger(__name__)

CONFIG = Config()

DEFAULT_CHECKPOINT_EVERY = 1000


def parse_request_header_mixins(
    _ctx: click.Context, _param: click.Parameter, value: str | None
) -> dict[str, str] | None:
    """Parse a JSON object string of custom request headers."""
    if not value:
        return None
    try:
        headers = json.loads(value)
    except json.JSONDecodeError as exception:
        message = f"must be a JSON object string: {exception}"
        raise click.BadParameter(message) from exception
    if not isinstance(headers, dict) or not all(
        isinstance(key, str) and isinstance(val, str) for key, val in headers.items()
    ):
        message = "must be a JSON object with string keys and string values"
        raise click.BadParameter(message)
    return headers


def positive_int(
    _ctx: click.Context, _param: click.Parameter, value: int | None
) -> int | None:
    """Click callback validating that an integer option is positive."""
    if value is not None and value < 1:
        message = "must be a positive integer"
        raise click.BadParameter(message)
    return value


def _is_s3_output(output_file: str) -> bool:
    return output_file.lower().startswith("s3://")


@click.group()
@click.option(
    "-h",
    "--host",
    required=True,
    help="Hostname of server for an OAI-PMH compliant source.",
)
@click.option(
    "-o",
    "--output-file",
    required=True,
    help="Filepath for generated output (either an XML file with harvested metadata or "
    "a JSON file describing set structure of an OAI-PMH compliant source). "
    "This value can be a local filepath or an S3 URI.",
)
@click.option(
    "--request-header-mixins",
    envvar="OAI_REQUEST_HEADER_MIXINS",
    default=None,
    callback=parse_request_header_mixins,
    help="JSON object string of custom headers to include in all HTTP requests to "
    'the OAI-PMH source, e.g. \'{"User-Agent": "my-harvester"}\'. Values can '
    "also be retrieved through the OAI_REQUEST_HEADER_MIXINS env var.",
)
@click.option(
    "-v", "--verbose", help="Pass to log at debug level instead of info", is_flag=True
)
@click.pass_context
def main(
    ctx: click.Context,
    host: str,
    output_file: str,
    request_header_mixins: dict[str, str] | None,
    verbose: bool,
) -> None:
    ctx.ensure_object(dict)
    ctx.obj["START_TIME"] = perf_counter()
    ctx.obj["HOST"] = host
    ctx.obj["OUTPUT_FILE"] = output_file
    ctx.obj["REQUEST_HEADERS"] = request_header_mixins
    logger.info(CONFIG.configure_logger(verbose))
    logger.info(CONFIG.configure_sentry())
    CONFIG.check_required_env_vars()
    if request_header_mixins:
        logger.info(
            "Custom request headers will be applied: %s",
            list(request_header_mixins.keys()),
        )


@main.command()
@click.option(
    "--method",
    default="list",
    show_default=True,
    help="Method for record retrieval. The 'list' method is faster and should "
    "be used in most cases; 'get' method should be used for ArchivesSpace due to "
    "errors retrieving a full record set with the 'list' method.",
    type=click.Choice(["get", "list"], case_sensitive=False),
)
@click.option(
    "-m",
    "--metadata-format",
    default="oai_dc",
    show_default=True,
    help="Alternate metadata format for harvested records. A record should only be "
    "returned if the format specified can be disseminated from the item identified "
    "by the value of the identifier argument.",
)
@click.option(
    "-f",
    "--from-date",
    default=None,
    help="Filter for files modified on or after this date; format YYYY-MM-DD.",
)
@click.option(
    "-u",
    "--until-date",
    default=None,
    help="Filter for files modified before this date; format YYYY-MM-DD.",
)
@click.option(
    "-s",
    "--set-spec",
    default=None,
    show_default=True,
    help="SetSpec of set to be harvested. Limits harvest to records in the provided set.",
)
@click.option(
    "-sr",
    "--skip-record",
    envvar="RECORD_SKIP_LIST",
    multiple=True,
    show_default=True,
    help="Set of OAI-PMH identifiers for records to skip during a harvest. Only works "
    "when --method=get. Multiple identifiers can be provided using the syntax: "
    "'-sr oai:12345 -sr oai:67890'. Values can also be retrieved through the "
    "RECORD_SKIP_LIST env var (see README for more details).",
)
@click.option(
    "--exclude-deleted",
    help="Pass to exclude deleted records from harvest.",
    is_flag=True,
)
@click.option(
    "--state-dir",
    default=None,
    type=click.Path(),
    help="Directory in which to persist harvested records and progress for a "
    "resumable harvest. Only supported with --method=list and a local output "
    "file. Resuming requires passing --resume alongside this option.",
)
@click.option(
    "--checkpoint-every",
    default=DEFAULT_CHECKPOINT_EVERY,
    show_default=True,
    callback=positive_int,
    type=int,
    help="Save progress at least once per this many harvested records. Must be a "
    "positive integer. Only used together with --state-dir.",
)
@click.option(
    "--resume",
    is_flag=True,
    default=False,
    help="Resume the harvest previously persisted in --state-dir. All other harvest "
    "parameters must match the original run.",
)
@click.pass_context
def harvest(
    ctx: click.Context,
    method: Literal["get", "list"],
    metadata_format: str,
    from_date: str,
    until_date: str,
    set_spec: str,
    skip_record: tuple[str] | None,
    exclude_deleted: bool,
    state_dir: str | None,
    checkpoint_every: int,
    resume: bool,
) -> None:
    """Harvest command to retrieve records from an OAI-PMH compliant source."""
    logger.info(
        "OAI-PMH harvesting from source %s with parameters: method=%s, "
        "metadata_format=%s, from_date=%s, until_date=%s, set=%s, skip_record=%s, "
        "exclude_deleted=%s",
        ctx.obj["HOST"],
        method,
        metadata_format,
        from_date,
        until_date,
        set_spec,
        skip_record,
        exclude_deleted,
    )

    output_file = ctx.obj["OUTPUT_FILE"]
    _validate_checkpoint_options(method, output_file, state_dir, checkpoint_every, resume)

    oai_client = OAIClient(
        ctx.obj["HOST"],
        metadata_format,
        from_date,
        until_date,
        set_spec,
        request_headers=ctx.obj["REQUEST_HEADERS"],
    )
    if method == "list" and skip_record:
        logger.warning(
            "Option --skip-record only works with the 'get' --method option, these "
            "records will not be skipped during harvest: %s",
            skip_record,
        )

    if state_dir:
        count = _run_resumable_harvest(
            oai_client,
            host=ctx.obj["HOST"],
            output_file=output_file,
            metadata_format=metadata_format,
            from_date=from_date,
            until_date=until_date,
            set_spec=set_spec,
            exclude_deleted=exclude_deleted,
            state_dir=state_dir,
            checkpoint_every=checkpoint_every,
            resume=resume,
        )
    else:
        count = _run_standard_harvest(
            oai_client,
            method=method,
            exclude_deleted=exclude_deleted,
            skip_record=skip_record,
            output_file=output_file,
        )

    logger.info(
        "Harvest completed. Total records harvested (%sincluding deleted records): %d",
        "not " if exclude_deleted else "",
        count,
    )
    elapsed_time = perf_counter() - ctx.obj["START_TIME"]
    logger.info(
        "Total time to complete harvest: %s", str(timedelta(seconds=elapsed_time))
    )


def _validate_checkpoint_options(
    method: str,
    output_file: str,
    state_dir: str | None,
    checkpoint_every: int,
    resume: bool,
) -> None:
    """Reject resumable-harvest options where they are not supported."""
    if state_dir is None:
        if resume:
            message = "--resume requires --state-dir"
            raise click.UsageError(message)
        if checkpoint_every != DEFAULT_CHECKPOINT_EVERY:
            message = "--checkpoint-every can only be used together with --state-dir"
            raise click.UsageError(message)
        return
    if method != "list":
        message = "Resumable harvests (--state-dir) are only supported with --method=list"
        raise click.UsageError(message)
    if _is_s3_output(output_file):
        message = (
            "Resumable harvests (--state-dir) require a local output file; S3 "
            "output is not supported"
        )
        raise click.UsageError(message)
    if checkpoint_every < 1:
        message = "--checkpoint-every must be a positive integer"
        raise click.BadParameter(message, param_hint="'--checkpoint-every'")


def _run_standard_harvest(
    oai_client: OAIClient,
    method: Literal["get", "list"],
    exclude_deleted: bool,
    skip_record: tuple[str] | None,
    output_file: str,
) -> int:
    try:
        records = oai_client.retrieve_records(
            method, exclude_deleted=exclude_deleted, skip_records=skip_record or None
        )
        logger.info("Writing records to output file: %s", output_file)
        return write_records(records, output_file)
    except NoRecordsMatch:
        logger.info(
            "No records harvested: the combination of the provided options results in "
            "an empty list."
        )
        sys.exit()


def _run_resumable_harvest(
    oai_client: OAIClient,
    host: str,
    output_file: str,
    metadata_format: str,
    from_date: str,
    until_date: str,
    set_spec: str,
    exclude_deleted: bool,
    state_dir: str,
    checkpoint_every: int,
    resume: bool,
) -> int:
    absolute_output = str(Path(output_file).resolve())
    parameters = HarvestParameters(
        host=host,
        output_file=absolute_output,
        metadata_format=metadata_format,
        from_date=from_date,
        until_date=until_date,
        set_spec=set_spec,
        exclude_deleted=exclude_deleted,
    )

    def page_fetcher(token: str | None) -> Page:
        return oai_client.list_records_page(
            exclude_deleted=exclude_deleted, resumption_token=token
        )

    harvest_run = ResumableListHarvest(
        state_dir=Path(state_dir).resolve(),
        output_file=Path(absolute_output),
        parameters=parameters,
        page_fetcher=page_fetcher,
        checkpoint_every=checkpoint_every,
        resume=resume,
    )
    logger.info(
        "%s resumable harvest using state directory %s",
        "Resuming" if resume else "Starting",
        state_dir,
    )
    try:
        with harvest_run:
            return harvest_run.run()
    except CheckpointError as exception:
        logger.exception("Resumable harvest failed")
        raise click.ClickException(str(exception)) from exception


@main.command()
@click.pass_context
def setlist(ctx: click.Context) -> None:
    """Create a JSON file describing the set structure of an OAI-PMH compliant source.

    Uses the OAI-PMH ListSets verbs to retrieve all sets from a repository, and writes
    the set names and specs to a JSON output file.
    """
    oai_client = OAIClient(ctx.obj["HOST"], request_headers=ctx.obj["REQUEST_HEADERS"])
    logger.info("Getting set list from source: %s", ctx.obj["HOST"])
    sets = oai_client.get_sets()
    logger.info("Writing setlist to output file %s", ctx.obj["OUTPUT_FILE"])
    write_sets(sets, ctx.obj["OUTPUT_FILE"])
    logger.info("Setlist completed")
