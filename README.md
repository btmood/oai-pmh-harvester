[![Coverage Status](https://coveralls.io/repos/github/MITLibraries/oai-pmh-harvester/badge.svg?branch=rdi-updates)](https://coveralls.io/github/MITLibraries/oai-pmh-harvester?branch=rdi-updates)

# oai-pmh-harvester

OAI-PMH-Harvester is a Python CLI application for harvesting metadata from repositories (also known as "Data Providers") available via the Open Archives Initiative Protocol for Metadata Harvesting (OAI-PMH). 

## Development
- To preview a list of available Makefile commands: `make help`
- To install with dev dependencies: `make install`
- To update dependencies: `make update`
- To run unit tests: `make test`
- To lint the repo: `make lint`
- To auto-fix formatting and lint issues: `make lint-fix`
- To run dependency vulnerability checks: `make security`
- To run the app: `uv run oai --help`

### Running the application on your local machine

Create a virtual environment and install dev dependencies: `make install`. 

Additional notes: 

1. To execute the steps below, you can use the following sample url to an OAI-PMH repo: `https://aspace-staff-dev.mit.edu/oai`.

2. To write the output file to an S3 bucket, include S3 in the `-o/--output-file` argument.
   * With AWS credentials: 
      ```
      -o s3://<AWS_KEY>:<AWS_SECRET_KEY>@<BUCKET_NAME>/<output-filename>.xml
      ```
   * Wihout AWS credentials (if you have your credentials stored locally):
      ```
      -o s3://<BUCKET_NAME>/<output-filename>.xml
      ```

#### With Docker

1. Run `make dist-dev` to build the Docker container image.

2. To run a harvest, execute the following command in your terminal:
   ```
   docker run -it --volume <local-file-path>:<docker-file-path>' oai-pmh-harvester-dev -h <url-to-oai-pmh-repo> -o <docker-file-path>/<output-filename>.xml harvest <optional-command-args>
   ```

   **Note:** The `-v/--volume` argument mounts the \<local-file-path> in the current directory into the container at \<docker-file-path>, which allows us to view the generated output file in \<local-file-path>.


#### Without Docker 

1. To run a harvest, execute the following command in your terminal:

   ```
   uv run oai -h <url-to-oai-pmh-repo> -o <output-filename>.xml harvest <optional-command-args>
   ```

## Environment variables

### Required

```shell
# Set to dev for local development, this will be set to 'stage' and 'prod' in those environments by Terraform.
WORKSPACE=dev
```

### Optional

```shell
# Required only if a source has records that cause errors during a harvest and --method=get. The value provided must be a space-separated list of OAI-PMH record identifiers to skip during harvest.
RECORD_SKIP_LIST=<oai-pmh-id1> <oai-pmh-id2>

# JSON object string of custom headers to include in all HTTP requests to the OAI-PMH source. Used as the default for the --request-header-mixins CLI option; the CLI option takes precedence if both are set. If neither is set, no custom headers are applied.
OAI_REQUEST_HEADER_MIXINS='{"User-Agent": "my-harvester"}'

# Sets the interval for logging status updates as records are written to the output file. Defaults to 1000, which will log a status update for every thousandth record.
STATUS_UPDATE_INTERVAL=1000

# If set to a valid Sentry DSN, enables Sentry exception monitoring. This can also be set to 'none'.
SENTRY_DSN=<sentry-dsn-for-oai-pmh-harvester>
```

## CLI commands

All CLI commands can be run with `uv run <COMMAND>`.

### `oai`

```text
Usage: -c [OPTIONS] COMMAND [ARGS]...

Options:
  -h, --host TEXT               Hostname of server for an OAI-PMH compliant
                                source.  [required]
  -o, --output-file TEXT        Filepath for generated output (either an XML
                                file with harvested metadata or a JSON file
                                describing set structure of an OAI-PMH
                                compliant source). This value can be a local
                                filepath or an S3 URI.  [required]
  --request-header-mixins TEXT  JSON object string of custom headers to
                                include in all HTTP requests to the OAI-PMH
                                source, e.g. '{"User-Agent": "my-harvester"}'.
                                Values can also be retrieved through the
                                OAI_REQUEST_HEADER_MIXINS env var.
  -v, --verbose                 Pass to log at debug level instead of info
  --help                        Show this message and exit.

Commands:
  harvest  Harvest command to retrieve records from an OAI-PMH compliant source.
  setlist  Create a JSON file describing the set structure of an OAI-PMH compliant source.
```

### `oai harvest`

```text
Usage: -c harvest [OPTIONS]

  Harvest command to retrieve records from an OAI-PMH compliant source.

Options:
  --method [get|list]         Method for record retrieval. The 'list' method
                              is faster and should be used in most cases;
                              'get' method should be used for ArchivesSpace
                              due to errors retrieving a full record set with
                              the 'list' method.  [default: list]
  -m, --metadata-format TEXT  Alternate metadata format for harvested records.
                              A record should only be returned if the format
                              specified can be disseminated from the item
                              identified by the value of the identifier
                              argument.  [default: oai_dc]
  -f, --from-date TEXT        Filter for files modified on or after this date;
                              format YYYY-MM-DD.
  -u, --until-date TEXT       Filter for files modified before this date;
                              format YYYY-MM-DD.
  -s, --set-spec TEXT         SetSpec of set to be harvested. Limits harvest
                              to records in the provided set.
  -sr, --skip-record TEXT     Set of OAI-PMH identifiers for records to skip
                              during a harvest. Only works when --method=get.
                              Multiple identifiers can be provided using the
                              syntax: '-sr oai:12345 -sr oai:67890'. Values
                              can also be retrieved through the
                              RECORD_SKIP_LIST env var (see README for more
                              details).
  --exclude-deleted           Pass to exclude deleted records from harvest.
  --state-dir PATH            Directory in which to persist harvested records
                              and progress for a resumable harvest. Only
                              supported with --method=list and a local output
                              file. Resuming requires passing --resume
                              alongside this option.
  --checkpoint-every INTEGER  Save progress at least once per this many
                              harvested records. Must be a positive integer.
                              Only used together with --state-dir.  [default:
                              1000]
  --resume                    Resume the harvest previously persisted in
                              --state-dir. All other harvest parameters must
                              match the original run.
  --help                      Show this message and exit.
```

### Resumable harvests (`harvest --method list`)

A long-running `list` harvest can be interrupted and later continued without
losing the records already retrieved. The feature is opt-in: runs without
`--state-dir` behave exactly as before and write the output file directly.
Resumable harvests require `--method list` and a **local** output file;
combining `--state-dir` with `--method get` or an `s3://` output is a
parameter error.

Options:

- `--state-dir PATH` — directory in which harvested records, the resumption
  token and the harvest parameters are persisted. The directory is created if
  it does not exist.
- `--checkpoint-every N` — progress is saved at least once every `N` newly
  harvested records (positive integer, default `1000`), and always at page
  boundaries.
- `--resume` — continue the harvest previously saved in `--state-dir`. A state
  directory that already contains a harvest cannot be reused without this flag.

First run:

```shell
oai -h http://127.0.0.1:8765/oai -o output.xml harvest --method list \
  --state-dir .harvest-state --checkpoint-every 3
```

Resume after an interruption (use the identical host, output path, metadata
format, dates, set and deleted-record settings):

```shell
oai -h http://127.0.0.1:8765/oai -o output.xml harvest --method list \
  --state-dir .harvest-state --checkpoint-every 3 --resume
```

Guarantees and behavior:

- Records are written in the same UTF-8 `<records>` XML format. Identifiers are
  deduplicated on disk (a SQLite database), keeping first-seen order, so
  re-fetching a page after a restart never produces duplicates; neither the
  full record set nor all identifiers are held in memory.
- The final output path is only touched once the complete document has been
  assembled and checksum-verified, when it is published atomically. Until then
  an existing output file stays byte-for-byte unchanged, and a failed first run
  leaves no output file behind. A legal empty result succeeds and writes an
  empty `<records>` document.
- Failures exit non-zero and leave resumable state behind. If the server
  rejects the saved resumption token (`badResumptionToken`), the harvest
  restarts from the original query and relies on the saved state to
  deduplicate; any other protocol error preserves the state and fails clearly.
- Resuming verifies the saved host, absolute output path, metadata format,
  date range, set and `--exclude-deleted` setting against the new arguments and
  fails before writing anything if they differ.
- An OS lock in the state directory prevents two processes harvesting the same
  directory at once; a process that dies (including a hard kill) releases the
  lock automatically, so the directory remains recoverable.
- Re-running `--resume` on a successfully completed harvest succeeds without
  re-contacting the server and leaves the output and completion state
  unchanged. If the process died after replacing the output but before marking
  the harvest complete, the next resume detects this and finishes cleanly.

State directory contents:

| File | Purpose |
| --- | --- |
| `manifest.json` | Harvest parameters, current resumption token, run status, record count and the staged document checksum. |
| `records.sqlite3` | On-disk record store and deduplication index, in first-seen order. |
| `staging.xml` | Complete output document, built before being atomically moved to the output path; removed once the harvest is marked complete. |
| `harvest.lock` | OS byte-range lock guarding against concurrent processes. |

### `oai setlist`
```
Usage: -c setlist [OPTIONS]

  Create a JSON file describing the set structure of an OAI-PMH compliant
  source.

  Uses the OAI-PMH ListSets verbs to retrieve all sets from a repository, and
  writes the set names and specs to a JSON output file.

Options:
  --help  Show this message and exit.
```



