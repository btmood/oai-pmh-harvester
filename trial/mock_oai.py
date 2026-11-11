"""Synthetic, loopback-only OAI-PMH source for the q1 trial."""

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree as ET

OAI = "http://www.openarchives.org/OAI/2.0/"
DC = "http://purl.org/dc/elements/1.1/"
OAI_DC = "http://www.openarchives.org/OAI/2.0/oai_dc/"
ET.register_namespace("", OAI)
ET.register_namespace("dc", DC)
ET.register_namespace("oai_dc", OAI_DC)

RECORDS = [
    {"id": f"oai:trial:{i:03d}", "date": f"2026-01-{i:02d}",
     "title": title, "deleted": i == 5}
    for i, title in enumerate(
        ["河流观测", "Café observations", "Wetland survey", "城市采样",
         "Deleted record", "Δ water quality", "Coastal station", "山地资料", "Final sample"], 1)
]


def q(name):
    return f"{{{OAI}}}{name}"


def record_element(record, header_only=False):
    node = ET.Element(q("header") if header_only else q("record"))
    header = node if header_only else ET.SubElement(node, q("header"))
    if record["deleted"]:
        header.set("status", "deleted")
    ET.SubElement(header, q("identifier")).text = record["id"]
    ET.SubElement(header, q("datestamp")).text = record["date"]
    ET.SubElement(header, q("setSpec")).text = "trial:fixtures"
    if not header_only and not record["deleted"]:
        metadata = ET.SubElement(node, q("metadata"))
        dc = ET.SubElement(metadata, f"{{{OAI_DC}}}dc")
        ET.SubElement(dc, f"{{{DC}}}title").text = record["title"]
        ET.SubElement(dc, f"{{{DC}}}identifier").text = record["id"]
    return node


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address=("127.0.0.1", 0), page_size=3):
        super().__init__(address, Handler)
        self.page_size = page_size
        self.guard = threading.Lock()
        self.calls = []
        self.failed = set()
        self.epochs = {}
        self.tokens = {}
        self.serial = 0
        self.pause_at_offset = None
        self.pause_started = threading.Event()
        self.pause_release = threading.Event()

    def issue_token(self, path, rows, offset):
        self.serial += 1
        token = f"opaque-{self.serial:08d}-x"
        self.tokens[token] = (path, self.epochs.get(path, 0), rows, offset)
        return token


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def send_body(self, body, content_type="application/xml; charset=utf-8", status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def respond(self):
        parsed = urlsplit(self.path)
        raw = parsed.query
        if self.command == "POST":
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        params = parse_qs(raw, keep_blank_values=True)
        args = {key: values[0] for key, values in params.items()}
        if parsed.path == "/health":
            return self.send_body(b'{"ok":true}', "application/json")
        if parsed.path == "/stats":
            with self.server.guard:
                data = json.dumps({"requests": self.server.calls}, ensure_ascii=False).encode()
            return self.send_body(data, "application/json; charset=utf-8")
        if parsed.path not in {"/oai", "/oai/fail-once", "/oai/expire-once", "/oai/empty"}:
            return self.send_body(b"not found", "text/plain", 404)
        with self.server.guard:
            self.server.calls.append({"path": parsed.path, "params": args})
            if len(self.server.calls) > 4096:
                self.server.calls.pop(0)
            token_state = self.server.tokens.get(args.get("resumptionToken"))
            pause = token_state and token_state[3] == self.server.pause_at_offset
        if pause:
            self.server.pause_started.set()
            if not self.server.pause_release.wait(20):
                return self.send_body(b"fixture pause timed out", "text/plain", 504)
        with self.server.guard:
            body = self.build_response(parsed.path, args)
        self.send_body(body)

    def build_response(self, path, args):
        root = ET.Element(q("OAI-PMH"))
        ET.SubElement(root, q("responseDate")).text = "2026-01-20T00:00:00Z"
        ET.SubElement(root, q("request"), {"verb": args.get("verb", "")}).text = (
            f"http://127.0.0.1:{self.server.server_port}{path}"
        )

        def result():
            return ET.tostring(root, encoding="utf-8", xml_declaration=True)

        def error(code, message):
            ET.SubElement(root, q("error"), {"code": code}).text = message
            return result()

        verb = args.get("verb")
        if verb == "Identify":
            node = ET.SubElement(root, q(verb))
            for key, value in {"repositoryName": "Synthetic q1 fixture", "baseURL": root[1].text,
                               "protocolVersion": "2.0", "adminEmail": "fixture@example.invalid",
                               "earliestDatestamp": "2026-01-01", "deletedRecord": "persistent",
                               "granularity": "YYYY-MM-DD"}.items():
                ET.SubElement(node, q(key)).text = value
            return result()
        if verb == "ListMetadataFormats":
            node = ET.SubElement(ET.SubElement(root, q(verb)), q("metadataFormat"))
            for key, value in {"metadataPrefix": "oai_dc", "schema": OAI_DC + "oai_dc.xsd",
                               "metadataNamespace": OAI_DC}.items():
                ET.SubElement(node, q(key)).text = value
            return result()
        if verb == "ListSets":
            node = ET.SubElement(ET.SubElement(root, q(verb)), q("set"))
            ET.SubElement(node, q("setSpec")).text = "trial:fixtures"
            ET.SubElement(node, q("setName")).text = "Synthetic fixture records"
            return result()
        if verb not in {"ListRecords", "ListIdentifiers", "GetRecord"}:
            return error("badVerb", "unsupported verb")
        if args.get("metadataPrefix", "oai_dc") != "oai_dc":
            return error("cannotDisseminateFormat", "only oai_dc is available")
        offset = 0
        if "resumptionToken" in args:
            if set(args) != {"verb", "resumptionToken"}:
                return error("badArgument", "continuation accepts only verb and token")
            state = self.server.tokens.get(args["resumptionToken"])
            if not state or state[0] != path or state[1] != self.server.epochs.get(path, 0):
                return error("badResumptionToken", "token expired")
            _, _, rows, offset = state
            if path.endswith("expire-once") and path not in self.server.failed:
                self.server.failed.add(path)
                self.server.epochs[path] = self.server.epochs.get(path, 0) + 1
                return error("badResumptionToken", "synthetic token expiry")
            if path.endswith("fail-once") and path not in self.server.failed:
                self.server.failed.add(path)
                return error("badArgument", "synthetic one-shot protocol failure")
        else:
            rows = [r for r in RECORDS if args.get("from", "0000") <= r["date"]
                    <= args.get("until", "9999")
                    and args.get("set", "trial:fixtures") == "trial:fixtures"]
            if path.endswith("empty"):
                rows = []
            if verb == "GetRecord":
                rows = [r for r in rows if r["id"] == args.get("identifier")]
        if not rows:
            return error("idDoesNotExist" if verb == "GetRecord" else "noRecordsMatch", "no records")
        node = ET.SubElement(root, q(verb))
        page = rows[offset:offset + self.server.page_size] if verb != "GetRecord" else rows
        for record in page:
            node.append(record_element(record, verb == "ListIdentifiers"))
        if verb != "GetRecord":
            next_offset = offset + len(page)
            token = ET.SubElement(node, q("resumptionToken"),
                                  {"cursor": str(offset), "completeListSize": str(len(rows))})
            if next_offset < len(rows):
                token.text = self.server.issue_token(path, rows, next_offset)
        return result()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--page-size", type=int, default=3)
    options = parser.parse_args()
    if options.page_size < 1:
        parser.error("page-size must be positive")
    server = FixtureServer(("127.0.0.1", options.port), options.page_size)
    print(f"Fixture listening at http://127.0.0.1:{server.server_port}/oai", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
