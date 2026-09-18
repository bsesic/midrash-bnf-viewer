#!/usr/bin/env python3

"""
Load the BnF Hebrew corpus into the Elasticsearch index "manuscripts".

The corpus on disk is one folder per manuscript:

    data/index-meta.json                     the catalogue, 349 records
    data/<id>/pages.json                     the page list, in reading order
    data/<id>/IE..._P000009_FL....json       one page: image box plus OCR lines

Two kinds of document go into the same index, told apart by `doc_type`:

    doc_type=manuscript    one per manuscript, the catalogue record
    doc_type=page          one per page, its lines held as nested documents

Nested lines are the point of the whole design. A search can then answer with
the exact line that matched, its bounding box in image pixels and its position
in the page, so the reader is put in front of the words rather than the folio.

Only the standard library is used, so the script runs on a server with nothing
installed but Python.

    python3 import_manuscripts.py ../data
    python3 import_manuscripts.py ../data --recreate
    python3 import_manuscripts.py ../data --only 990000532270205171_IE58044885
    python3 import_manuscripts.py ../data --dry-run
"""

import argparse
import base64
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable

HERE = Path(__file__).resolve().parent
MAPPING_FILE = HERE / "mapping.json"

RETRY_STATUS = (429, 502, 503, 504)
RETRY_ATTEMPTS = 4


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Connection:
    """Everything needed to talk to one Elasticsearch cluster.

    Held as a plain value object because worker processes are handed a copy of
    it; an open socket could not be passed across a process boundary.
    """

    def __init__(
        self,
        url: str = "http://127.0.0.1:9200",
        user: str | None = None,
        password: str | None = None,
        api_key: str | None = None,
        ca_cert: str | None = None,
        insecure: bool = False,
        timeout: float = 180.0,
    ) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.headers: dict[str, str] = {"Accept": "application/json"}

        if api_key:
            self.headers["Authorization"] = f"ApiKey {api_key}"
        elif user:
            raw = f"{user}:{password or ''}".encode("utf-8")
            token = base64.b64encode(raw).decode("ascii")
            self.headers["Authorization"] = f"Basic {token}"

        self.ca_cert = ca_cert
        self.insecure = insecure
        self._context: ssl.SSLContext | None = None

    @property
    def context(self) -> "ssl.SSLContext | None":
        """Built on first use, and never pickled.

        An SSLContext cannot be sent to a worker process, and this object is
        handed to every one of them, so the context is rebuilt on the far side.
        """

        if self._context is None:
            if self.insecure:
                self._context = ssl._create_unverified_context()
            elif self.ca_cert:
                self._context = ssl.create_default_context(cafile=self.ca_cert)

        return self._context

    def __getstate__(self) -> dict:
        state = dict(self.__dict__)
        state["_context"] = None
        return state

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)

    def path(self, *parts: str) -> str:
        quoted = "/".join(
            urllib.parse.quote(str(part), safe="") for part in parts
        )

        return f"{self.url}/{quoted}"


def request(
    connection: Connection,
    method: str,
    url: str,
    body: Any = None,
    content_type: str = "application/json",
) -> tuple[int, Any]:
    """Send one HTTP request to Elasticsearch, retrying what is worth retrying.

    A busy cluster answers 429 or 503 rather than failing outright, and a bulk
    import is exactly what makes it busy. Backing off and trying again is the
    difference between an import that finishes and one that dies at 80 percent.
    """

    data = None

    if body is not None:
        if isinstance(body, bytes):
            data = body
        else:
            data = json.dumps(
                body,
                ensure_ascii=False,
            ).encode("utf-8")

    headers = dict(connection.headers)
    headers["Content-Type"] = content_type

    request_object = urllib.request.Request(
        url=url,
        data=data,
        method=method,
        headers=headers,
    )

    last_error: Exception | None = None

    for attempt in range(RETRY_ATTEMPTS):
        try:
            with urllib.request.urlopen(
                request_object,
                timeout=connection.timeout,
                context=connection.context,
            ) as response:
                raw = response.read().decode("utf-8")

                if not raw:
                    return response.status, None

                return response.status, json.loads(raw)

        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")

            try:
                details = json.loads(raw)
            except json.JSONDecodeError:
                details = raw

            if error.code in RETRY_STATUS and attempt < RETRY_ATTEMPTS - 1:
                last_error = error
                time.sleep(2.0 * (attempt + 1))
                continue

            raise RuntimeError(
                f"{method} {url} failed "
                f"(HTTP {error.code}): {details}"
            ) from error

        except (urllib.error.URLError, TimeoutError) as error:
            if attempt < RETRY_ATTEMPTS - 1:
                last_error = error
                time.sleep(2.0 * (attempt + 1))
                continue

            raise RuntimeError(
                f"{method} {url} failed: {error}"
            ) from error

    raise RuntimeError(f"{method} {url} gave up: {last_error}")


def index_exists(connection: Connection, index_name: str) -> bool:
    try:
        request(connection, "HEAD", connection.path(index_name))
        return True

    except RuntimeError as error:
        if "HTTP 404" in str(error):
            return False

        raise


# ---------------------------------------------------------------------------
# Reading the corpus
# ---------------------------------------------------------------------------
def load_catalogue(metadata_file: Path) -> list[dict[str, Any]]:
    """Read index-meta.json and return its manuscript records.

    The file ships as {"manuscripts": [...], "facets": {...}}, but a bare list
    and a {"records": [...]} wrapper are accepted too, so a hand-trimmed
    catalogue can be imported without editing this script.
    """

    with metadata_file.open("r", encoding="utf-8-sig") as file:
        root = json.load(file)

    records: Iterable[Any]

    if isinstance(root, list):
        records = root
    elif isinstance(root, dict):
        for key in ("manuscripts", "records", "items", "documents"):
            if isinstance(root.get(key), list):
                records = root[key]
                break
        else:
            raise ValueError(
                "index-meta.json holds no manuscript list "
                f"(keys: {', '.join(sorted(root))})"
            )
    else:
        raise ValueError("index-meta.json is neither a list nor an object")

    out = [
        record for record in records
        if isinstance(record, dict) and record.get("id")
    ]

    if not out:
        raise ValueError("index-meta.json holds no record with an 'id'")

    return out


def split_multiple(value: Any) -> list[str]:
    """Catalogue fields carry several values in one string: "heb; jrb"."""

    if not value:
        return []

    out: list[str] = []

    for part in str(value).replace("|", ";").split(";"):
        part = part.strip(" .,")

        if part:
            out.append(part)

    return out


def as_integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_manuscript_document(
    record: dict[str, Any],
    order: int,
) -> dict[str, Any]:
    """One catalogue record becomes one manuscript document.

    `_f` is dropped. It is the folded search blob the old in-page search needed,
    and Elasticsearch does that folding itself in the `hebrew_fold` analyzer, so
    carrying it would only be a second spelling of the same words.
    """

    return {
        "doc_type": "manuscript",
        "ms_id": record["id"],
        "ms_order": order,
        "alma": record.get("alma") or None,
        "ark": record.get("ark") or None,
        "shelfmark": record.get("shelfmark") or None,
        "title": record.get("title") or None,
        "date": record.get("date") or None,
        "year": as_integer(record.get("year")),
        "language": split_multiple(record.get("language")),
        "script": split_multiple(record.get("script")),
        "genre": split_multiple(record.get("genre")),
        "subject": split_multiple(record.get("subject")),
        "persons": record.get("persons") or None,
        "extent": record.get("extent") or None,
        "n_pages": as_integer(record.get("n_pages")),
        "n_lines": as_integer(record.get("n_lines")),
    }


def read_page_file(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read one page file in either the compact or the long form.

    Compact, which is what the corpus actually ships:

        {"i": id, "p": page, "f": folio, "u": iiif, "w": width, "h": height,
         "l": [[x, y, w, h, main, conf, text], ...]}

    Long, which the converter writes when asked for readable output:

        {"id": ..., "lines": [{"x": ..., "text": ...}, ...]}
    """

    with path.open("r", encoding="utf-8-sig") as file:
        data = json.load(file)

    if isinstance(data.get("l"), list):
        head = {
            "id": data.get("i"),
            "page": data.get("p"),
            "folio": data.get("f"),
            "iiif": data.get("u"),
            "image": data.get("c"),
            "width": data.get("w"),
            "height": data.get("h"),
        }

        raw = [
            {
                "x": entry[0],
                "y": entry[1],
                "w": entry[2],
                "h": entry[3],
                "main": entry[4] == 1,
                "conf": entry[5],
                "text": entry[6],
            }
            for entry in data["l"]
            if isinstance(entry, list) and len(entry) >= 7
        ]

    else:
        head = {
            "id": data.get("id"),
            "page": data.get("page"),
            "folio": data.get("folio"),
            "iiif": data.get("iiif"),
            "image": data.get("image"),
            "width": data.get("width"),
            "height": data.get("height"),
        }

        raw = data.get("lines") or []

    lines: list[dict[str, Any]] = []

    for number, line in enumerate(raw):
        text = str(line.get("text") or "").strip()

        if not text:
            continue

        lines.append(
            {
                "n": number,
                "text": text,
                "box": [
                    as_integer(line.get("x")) or 0,
                    as_integer(line.get("y")) or 0,
                    as_integer(line.get("w")) or 0,
                    as_integer(line.get("h")) or 0,
                ],
                "conf": line.get("conf"),
                "main": line.get("main") is not False,
            }
        )

    return head, lines


def list_page_files(folder: Path) -> list[tuple[str, str | None]]:
    """Return (filename, folio) in reading order.

    pages.json carries the order the manuscript is bound in, which is not the
    alphabetical order of the filenames for every manuscript. Where it is
    missing or unreadable, fall back to whatever is on disk rather than
    importing nothing.
    """

    manifest = folder / "pages.json"

    if manifest.is_file():
        try:
            with manifest.open("r", encoding="utf-8-sig") as file:
                pages = json.load(file).get("pages") or []

            listed = [
                (str(page["j"]), page.get("f"))
                for page in pages
                if isinstance(page, dict) and page.get("j")
            ]

            if listed:
                return listed

        except (OSError, ValueError, KeyError):
            pass

    return [
        (entry.name, None)
        for entry in sorted(folder.iterdir())
        if entry.suffix.lower() == ".json" and entry.name != "pages.json"
    ]


def build_page_document(
    manuscript: dict[str, Any],
    head: dict[str, Any],
    lines: list[dict[str, Any]],
    filename: str,
    folio_hint: str | None,
) -> dict[str, Any]:
    return {
        "doc_type": "page",
        "ms_id": manuscript["ms_id"],
        "ms_order": manuscript["ms_order"],
        # Denormalised, so a search hit can be listed without a second lookup.
        "shelfmark": manuscript.get("shelfmark"),
        "title": manuscript.get("title"),
        "year": manuscript.get("year"),
        "page_no": as_integer(head.get("page")),
        "folio": head.get("folio") or folio_hint or None,
        "file": filename,
        "iiif": head.get("iiif") or None,
        "image": head.get("image") or None,
        "width": as_integer(head.get("width")),
        "height": as_integer(head.get("height")),
        "lines": lines,
    }


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------
class BulkSender:
    """Collect documents and post them once they are worth a round trip.

    Batching by byte size rather than by document count, because a page of 40
    lines and a page of 3 lines differ by an order of magnitude and a fixed
    count would send either tiny requests or ones the cluster rejects.
    """

    def __init__(
        self,
        connection: Connection,
        index_name: str,
        max_bytes: int,
    ) -> None:
        self.connection = connection
        self.index_name = index_name
        self.max_bytes = max_bytes

        self.lines: list[str] = []
        self.size = 0

        self.successful = 0
        self.failed = 0
        self.errors: list[str] = []

    def add(self, document_id: str, document: dict[str, Any]) -> None:
        action = json.dumps(
            {
                "index": {
                    "_index": self.index_name,
                    "_id": document_id,
                }
            },
            ensure_ascii=False,
        )

        body = json.dumps(document, ensure_ascii=False)

        self.lines.append(action)
        self.lines.append(body)
        self.size += len(action) + len(body) + 2

        if self.size >= self.max_bytes:
            self.flush()

    def flush(self) -> None:
        if not self.lines:
            return

        # The Bulk API requires a final newline.
        payload = ("\n".join(self.lines) + "\n").encode("utf-8")

        _, response = request(
            self.connection,
            "POST",
            f"{self.connection.url}/_bulk",
            payload,
            content_type="application/x-ndjson",
        )

        for item in (response or {}).get("items", []):
            result = item.get("index", {})
            status = result.get("status", 500)

            if 200 <= status < 300:
                self.successful += 1
            else:
                self.failed += 1

                if len(self.errors) < 5:
                    self.errors.append(
                        json.dumps(result, ensure_ascii=False)[:300]
                    )

        self.lines = []
        self.size = 0


# Set once per worker process by `configure_worker`.
WORKER: dict[str, Any] = {}


def configure_worker(
    connection: Connection,
    index_name: str,
    data_root: str,
    max_bytes: int,
    dry_run: bool,
) -> None:
    WORKER["connection"] = connection
    WORKER["index_name"] = index_name
    WORKER["data_root"] = Path(data_root)
    WORKER["max_bytes"] = max_bytes
    WORKER["dry_run"] = dry_run


def import_manuscript(manuscript: dict[str, Any]) -> dict[str, Any]:
    """Index one manuscript and all of its pages. Runs in a worker process."""

    connection: Connection = WORKER["connection"]
    index_name: str = WORKER["index_name"]
    data_root: Path = WORKER["data_root"]
    dry_run: bool = WORKER["dry_run"]

    sender = BulkSender(connection, index_name, WORKER["max_bytes"])

    manuscript_id = manuscript["ms_id"]
    folder = data_root / manuscript_id

    pages = 0
    lines_total = 0
    skipped: list[str] = []

    if folder.is_dir():
        for filename, folio_hint in list_page_files(folder):
            try:
                head, lines = read_page_file(folder / filename)

            except (OSError, ValueError, TypeError, IndexError) as error:
                skipped.append(f"{filename} ({error})")
                continue

            if not lines:
                continue

            document_id = f"page:{manuscript_id}:{Path(filename).stem}"

            if not dry_run:
                sender.add(
                    document_id,
                    build_page_document(
                        manuscript,
                        head,
                        lines,
                        filename,
                        folio_hint,
                    ),
                )

            pages += 1
            lines_total += len(lines)

    else:
        skipped.append("no folder on disk")

    # The catalogue record carries the counts just measured, so what the reader
    # is told a manuscript holds never disagrees with what is searchable.
    record = dict(manuscript)
    record["n_pages"] = pages or record.get("n_pages")
    record["n_lines"] = lines_total or record.get("n_lines")

    if not dry_run:
        sender.add(f"ms:{manuscript_id}", record)
        sender.flush()

    return {
        "ms_id": manuscript_id,
        "pages": pages,
        "lines": lines_total,
        "successful": sender.successful,
        "failed": sender.failed,
        "errors": sender.errors,
        "skipped": skipped,
    }


def ensure_index(
    connection: Connection,
    index_name: str,
    recreate: bool,
) -> None:
    """Create the index from mapping.json, or verify the one already there."""

    with MAPPING_FILE.open("r", encoding="utf-8") as file:
        mapping = json.load(file)

    index_url = connection.path(index_name)

    if recreate and index_exists(connection, index_name):
        request(connection, "DELETE", index_url)
        print(f"Deleted index {index_name!r}.")

    if not index_exists(connection, index_name):
        request(connection, "PUT", index_url, mapping)
        print(f"Created index {index_name!r} from mapping.json.")
        return

    # The index is already there. It has to carry our analyzer, or Hebrew goes
    # in with its vowel points and final letters intact and nothing a reader
    # types will match. An analyzer cannot be added to an open index, so say so
    # plainly rather than importing into something that will not answer.
    _, settings = request(connection, "GET", f"{index_url}/_settings")

    analyzers = (
        settings.get(index_name, {})
        .get("settings", {})
        .get("index", {})
        .get("analysis", {})
        .get("analyzer", {})
    )

    if "hebrew_fold" not in analyzers:
        _, counted = request(connection, "GET", f"{index_url}/_count")

        raise SystemExit(
            f"The index {index_name!r} exists but was not created from "
            "mapping.json: it has no 'hebrew_fold' analyzer, and an analyzer "
            "cannot be added to an index that is already open.\n"
            f"It currently holds {counted.get('count', 0)} documents.\n"
            "Re-run with --recreate to drop and rebuild it."
        )

    request(
        connection,
        "PUT",
        f"{index_url}/_mapping",
        {"properties": mapping["mappings"]["properties"]},
    )

    print(f"Verified or extended the mapping for {index_name!r}.")


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Load manuscript metadata and page transcriptions into the "
            "Elasticsearch 'manuscripts' index."
        )
    )

    parser.add_argument(
        "data_root",
        type=Path,
        nargs="?",
        default=HERE.parent / "data",
        help=(
            "Corpus directory holding index-meta.json and one folder per "
            "manuscript (default: ../data)"
        ),
    )

    parser.add_argument(
        "--metadata-file",
        type=Path,
        default=None,
        help="Path to index-meta.json (default: <data_root>/index-meta.json)",
    )

    parser.add_argument(
        "--url",
        default=os.environ.get("ES_URL", "http://127.0.0.1:9200"),
        help="Elasticsearch URL (default: $ES_URL or http://127.0.0.1:9200)",
    )

    parser.add_argument(
        "--index",
        default=os.environ.get("ES_INDEX", "manuscripts"),
        help="Target index (default: manuscripts)",
    )

    parser.add_argument("--user", default=os.environ.get("ES_USER"))
    parser.add_argument("--password", default=os.environ.get("ES_PASSWORD"))
    parser.add_argument("--api-key", default=os.environ.get("ES_API_KEY"))

    parser.add_argument(
        "--ca-cert",
        default=os.environ.get("ES_CA_CERT"),
        help="CA bundle for a cluster with its own certificate",
    )

    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Do not verify TLS certificates",
    )

    parser.add_argument(
        "--recreate",
        action="store_true",
        help="Delete the index and rebuild it from mapping.json",
    )

    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="Import just this manuscript id (may be repeated)",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Stop after this many manuscripts, for a smoke test",
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=min(8, os.cpu_count() or 2),
        help="Parallel importer processes (default: one per core, up to 8)",
    )

    parser.add_argument(
        "--bulk-mb",
        type=float,
        default=8.0,
        help="Bulk request size in megabytes (default: 8)",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and validate every file without indexing anything",
    )

    args = parser.parse_args()

    data_root = args.data_root.resolve()
    metadata_file = (
        args.metadata_file or data_root / "index-meta.json"
    ).resolve()

    if not data_root.is_dir():
        print(f"Data directory not found: {data_root}", file=sys.stderr)
        return 1

    if not metadata_file.is_file():
        print(f"Metadata file not found: {metadata_file}", file=sys.stderr)
        return 1

    connection = Connection(
        url=args.url,
        user=args.user,
        password=args.password,
        api_key=args.api_key,
        ca_cert=args.ca_cert,
        insecure=args.insecure,
    )

    if not args.dry_run:
        try:
            _, info = request(connection, "GET", connection.url + "/")
        except RuntimeError as error:
            print(f"Cannot reach Elasticsearch: {error}", file=sys.stderr)
            return 1

        version = (info or {}).get("version", {}).get("number", "?")
        print(f"Elasticsearch {version} at {connection.url}")

        ensure_index(connection, args.index, args.recreate)

    try:
        records = load_catalogue(metadata_file)
    except ValueError as error:
        print(f"{metadata_file}: {error}", file=sys.stderr)
        return 1

    manuscripts = [
        build_manuscript_document(record, order)
        for order, record in enumerate(records)
    ]

    if args.only:
        wanted = set(args.only)
        manuscripts = [m for m in manuscripts if m["ms_id"] in wanted]

        if not manuscripts:
            print(
                "None of the requested ids are in the catalogue: "
                + ", ".join(sorted(wanted)),
                file=sys.stderr,
            )
            return 1

    if args.limit:
        manuscripts = manuscripts[: args.limit]

    print(
        f"Found {len(records)} catalogue records; "
        f"importing {len(manuscripts)} "
        f"with {args.workers} worker(s)."
        + (" Dry run: nothing will be indexed." if args.dry_run else "")
    )

    started = time.time()
    interactive = sys.stdout.isatty()

    totals = {"pages": 0, "lines": 0, "successful": 0, "failed": 0}
    problems: list[tuple[str, list[str]]] = []

    worker_args = (
        connection,
        args.index,
        str(data_root),
        int(args.bulk_mb * 1024 * 1024),
        args.dry_run,
    )

    def account(result: dict[str, Any], done: int) -> None:
        for key in totals:
            totals[key] += result[key]

        if result["skipped"]:
            problems.append((result["ms_id"], result["skipped"]))

        if result["errors"]:
            problems.append((result["ms_id"], result["errors"]))

        # A terminal gets one line that rewrites itself; a log file gets an
        # occasional line, rather than 349 of them overwriting nothing.
        if not interactive and done % 25 and done != len(manuscripts):
            return

        sys.stdout.write(
            "%s  %d/%d  %-30s %6d pages  %8d lines  %4.0fs %s"
            % (
                "\r" if interactive else "",
                done,
                len(manuscripts),
                result["ms_id"][:30],
                totals["pages"],
                totals["lines"],
                time.time() - started,
                "" if interactive else "\n",
            )
        )
        sys.stdout.flush()

    if args.workers <= 1:
        configure_worker(*worker_args)

        for done, manuscript in enumerate(manuscripts, start=1):
            account(import_manuscript(manuscript), done)

    else:
        with ProcessPoolExecutor(
            max_workers=args.workers,
            initializer=configure_worker,
            initargs=worker_args,
        ) as pool:
            futures = {
                pool.submit(import_manuscript, manuscript): manuscript["ms_id"]
                for manuscript in manuscripts
            }

            for done, future in enumerate(as_completed(futures), start=1):
                try:
                    account(future.result(), done)

                except Exception as error:
                    # One unreadable manuscript must not end the whole import.
                    problems.append((futures[future], [str(error)]))
                    sys.stdout.write(f"\n  ! {futures[future]}: {error}\n")

    print()

    indexed = 0

    if not args.dry_run:
        request(
            connection,
            "POST",
            connection.path(args.index) + "/_refresh",
        )

        _, counted = request(
            connection,
            "GET",
            connection.path(args.index) + "/_count",
        )

        indexed = (counted or {}).get("count", 0)

    print()
    print("Completed in %.0fs:" % (time.time() - started))
    print(f"  Manuscripts:            {len(manuscripts)}")
    print(f"  Pages read:             {totals['pages']}")
    print(f"  Lines read:             {totals['lines']}")
    print(f"  Documents indexed:      {totals['successful']}")
    print(f"  Indexing errors:        {totals['failed']}")
    print(f"  Documents in the index: {indexed}")

    if problems:
        print(f"\n{len(problems)} manuscripts reported problems:")

        for manuscript_id, items in problems[:20]:
            shown = "; ".join(items[:3])
            more = " ..." if len(items) > 3 else ""
            print(f"  {manuscript_id}: {shown}{more}")

        if len(problems) > 20:
            print(f"  ... and {len(problems) - 20} more")

    return 0 if totals["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
