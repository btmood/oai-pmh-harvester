# ruff: noqa: FBT001, UP012

"""oai.py module."""

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

import smart_open
from requests import RequestException
from sickle import Sickle, oaiexceptions
from sickle.models import Record
from sickle.oaiexceptions import IdDoesNotExist, OAIError

from harvester.checkpoint import HarvestedRecord, Page
from harvester.config import (
    DEFAULT_RETRY_AFTER,
    MAX_ALLOWED_ERRORS,
    MAX_RETRIES,
    RETRY_STATUS_CODES,
    Config,
)
from harvester.exceptions import MaxAllowedErrorsReached
from harvester.utils import send_sentry_message

logger = logging.getLogger(__name__)

CONFIG = Config()


@dataclass(frozen=True)
class FetchedRecord:
    """Sickle-agnostic view of one record, satisfying :class:`HarvestedRecord`."""

    identifier: str | None
    deleted: bool
    raw: str


def _adapt_record(sickle_record: Record) -> FetchedRecord:
    """Project the fields the checkpoint engine needs from a sickle record."""
    return FetchedRecord(
        identifier=sickle_record.header.identifier,
        deleted=sickle_record.deleted,
        raw=sickle_record.raw,
    )


class OAIClient:
    def __init__(
        self,
        source_url: str,
        metadata_format: str | None = None,
        from_date: str | None = None,
        until_date: str | None = None,
        set_spec: str | None = None,
        max_retries: int | None = MAX_RETRIES,
        retry_status_codes: list[int] = RETRY_STATUS_CODES,
        request_headers: dict[str, str] | None = None,
    ) -> None:
        self.source_url = source_url
        # sickle passes extra kwargs through to requests for every HTTP request
        request_args: dict[str, Any] = {}
        if request_headers:
            request_args["headers"] = request_headers
        self.client = Sickle(
            self.source_url,
            default_retry_after=DEFAULT_RETRY_AFTER,
            max_retries=max_retries,
            retry_status_codes=retry_status_codes,
            **request_args,
        )
        self.metadata_format = metadata_format
        self._set_params(metadata_format, from_date, until_date, set_spec)

    def _set_params(
        self,
        metadata_format: str | None = None,
        from_date: str | None = None,
        until_date: str | None = None,
        set_spec: str | None = None,
    ) -> None:
        params = {}
        if metadata_format:
            params["metadataPrefix"] = metadata_format
        if from_date:
            params["from"] = from_date
        if until_date:
            params["until"] = until_date
        if set_spec:
            params["set"] = set_spec
        self.params = params

    def get_identifiers(self, exclude_deleted: bool) -> Iterator[str]:
        responses = self.client.ListIdentifiers(
            ignore_deleted=exclude_deleted, **self.params
        )
        for record in responses:
            yield record.identifier

    def get_records(
        self,
        identifiers: Iterator[str],
        skip_list: tuple[str] | None = None,
        max_allowed_errors: int = MAX_ALLOWED_ERRORS,
    ) -> Iterator[Record]:
        failed_records: list[tuple[str, Any | str | None]] = []
        for identifier in identifiers:
            if len(failed_records) == max_allowed_errors:
                message = (
                    f"OAI harvest ABORTED, max errors reached: {max_allowed_errors}."
                )
                send_sentry_message(
                    message,
                    {"failed_records": failed_records},
                )
                raise MaxAllowedErrorsReached(message)

            if skip_list and identifier in skip_list:
                logger.warning(
                    "Skipped retrieving record with identifier %s because it is in the "
                    "skip list",
                    identifier,
                )
                continue

            try:
                record = self.client.GetRecord(
                    identifier=identifier, metadataPrefix=self.metadata_format
                )
                logger.debug("Record retrieved: %s", identifier)
            except (RequestException, OAIError) as e:
                logger.warning(
                    "GetRecord error for identifier %s, reporting to Sentry", identifier
                )
                failed_records.append((identifier, getattr(e.request, "url", None)))
                continue
            except IdDoesNotExist:
                logger.warning(
                    "Identifier %s retrieved in identifiers list returned 'id does not "
                    "exist' during getRecord request",
                    identifier,
                )
                continue
            yield record

        if len(failed_records) > 0:
            send_sentry_message(
                f"OAI harvest COMPLETED, but with errors: {len(failed_records)} "
                f"records skipped.",
                {"failed_records": failed_records},
            )

    def get_sets(self) -> list[dict]:
        responses = self.client.ListSets()
        return [
            {"Set name": response_set.setName, "Set spec": response_set.setSpec}
            for response_set in responses
        ]

    def list_records(self, exclude_deleted: bool) -> Iterator[Record]:
        return self.client.ListRecords(ignore_deleted=exclude_deleted, **self.params)

    def list_records_page(
        self, exclude_deleted: bool, resumption_token: str | None
    ) -> Page:
        """Fetch one ListRecords page without hiding resumption details.

        Unlike :meth:`list_records`, which returns a sickle iterator, this
        performs a single HTTP request and returns the page's records together
        with the next resumption token.  It mirrors sickle's response parsing
        and OAI error mapping so record contents are identical.

        Args:
            exclude_deleted: Whether deleted records on the page are filtered.
            resumption_token: Token for the page to fetch; ``None`` issues the
                initial query with the configured harvest parameters.

        Returns:
            A :class:`~harvester.checkpoint.Page` for the response.
        """
        if resumption_token:
            params = {"verb": "ListRecords", "resumptionToken": resumption_token}
        else:
            params = {"verb": "ListRecords", **self.params}
        response = self.client.harvest(**params)
        xml = response.xml
        error = xml.find(".//" + self.client.oai_namespace + "error")
        if error is not None:
            code = error.attrib.get("code", "UNKNOWN")
            description = error.text or ""
            exception_class = getattr(oaiexceptions, code[0].upper() + code[1:], None)
            if exception_class is None:
                raise OAIError(description)
            raise exception_class(description)
        token_element = xml.find(".//" + self.client.oai_namespace + "resumptionToken")
        next_token: str | None = None
        if token_element is not None:
            next_token = token_element.text or None
        page_records: tuple[HarvestedRecord, ...] = ()
        for element in xml.iterfind(".//" + self.client.oai_namespace + "record"):
            record = Record(element)
            if exclude_deleted and record.deleted:
                continue
            page_records = (*page_records, _adapt_record(record))
        return Page(records=page_records, resumption_token=next_token)

    def retrieve_records(
        self,
        method: Literal["get", "list"],
        exclude_deleted: bool,
        skip_records: tuple[str] | None = None,
    ) -> Iterator[Record]:
        if method == "get":
            identifiers = self.get_identifiers(exclude_deleted)
            return self.get_records(identifiers, skip_list=skip_records)
        if method == "list":
            return self.list_records(exclude_deleted)

        message = f'Method must be either "get" or "list", method provided was "{method}"'
        raise ValueError(message)


def write_records(records: Iterator, filepath: str) -> int:
    count = 0
    with smart_open.open(filepath, "wb") as file:
        file.write('<?xml version="1.0" encoding="UTF-8"?>\n<records>\n'.encode())
        for record in records:
            file.write("  ".encode() + record.raw.encode() + "\n".encode())
            count += 1
            if count % int(CONFIG.STATUS_UPDATE_INTERVAL) == 0:
                logger.info(
                    "Status update: %s records written to output file so far!",
                    count,
                )
        file.write("</records>".encode())
    return count


def write_sets(sets: list[dict[str, str]], filepath: str) -> None:
    with open(filepath, "w") as file:
        file.write(json.dumps(sets, indent=2))
