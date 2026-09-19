from __future__ import annotations

import html
import json
import threading
import uuid
import webbrowser
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, urlsplit

from .core import CELLS, Warden
from .errors import ArchotrazError

MAX_REQUEST_BYTES = 128 * 1024 * 1024


def _is_allowed_local_host(raw_host: str | None) -> bool:
    if not raw_host or any(char in raw_host for char in "/\\@"):
        return False
    try:
        parsed = urlsplit(f"//{raw_host}")
    except ValueError:
        return False
    return parsed.hostname in {"127.0.0.1", "localhost"} and parsed.username is None and parsed.password is None


def state_payload(warden: Warden) -> dict[str, object]:
    return warden.state_summary()


def _badge(label: str, value: str) -> str:
    return f'<span class="badge">{html.escape(label)}: {html.escape(value)}</span>'


def render_dashboard(warden: Warden, *, message: str = "") -> str:
    state = state_payload(warden)
    counts = state["counts"]
    capabilities = state["capabilities"]
    candidates = state["candidates"]
    dossiers = warden.list_kitchen_dossiers()

    candidate_rows: list[str] = []
    for candidate in candidates:
        cid = html.escape(candidate["id"])
        source = html.escape(candidate["source_uri"])
        cell = html.escape(candidate["cell"] or "UNASSIGNED")
        eligibility = "eligible" if candidate["eligible"] else f"held — {candidate['exclusion_reason'] or 'no reason'}"
        eligibility = html.escape(eligibility)
        version = int(candidate["version"])
        actions: list[str] = []
        if candidate["eligible"]:
            action_key = f"web-exclude-{uuid.uuid4().hex}"
            actions.append(
                f'''<form method="post" action="/candidate/{cid}/exclude" class="inline">
                <input name="reason" value="manual hold" aria-label="Hold reason">
                <input type="hidden" name="expected_version" value="{version}">
                <input type="hidden" name="idempotency_key" value="{action_key}">
                <button type="submit">Hold</button></form>'''
            )
        else:
            action_key = f"web-restore-{uuid.uuid4().hex}"
            actions.append(
                f'''<form method="post" action="/candidate/{cid}/restore" class="inline">
                <input type="hidden" name="expected_version" value="{version}">
                <input type="hidden" name="idempotency_key" value="{action_key}">
                <button type="submit">Restore</button></form>'''
            )
        cell_options = "".join(
            f'<option value="{value}"{" selected" if candidate["cell"] == value else ""}>{value}</option>'
            for value in sorted(CELLS)
        )
        cell_key = f"web-cell-{uuid.uuid4().hex}"
        actions.append(
            f'''<form method="post" action="/candidate/{cid}/cell" class="inline">
            <select name="cell" aria-label="Manual cell assignment">{cell_options}</select>
            <input type="hidden" name="expected_version" value="{version}">
            <input type="hidden" name="idempotency_key" value="{cell_key}">
            <button type="submit">Assign</button></form>'''
        )
        candidate_rows.append(
            f"<tr><td><code>{cid}</code></td><td>{source}</td><td>{eligibility}</td><td>{cell}</td><td>{''.join(actions)}</td></tr>"
        )

    dossier_rows: list[str] = []
    for dossier in dossiers:
        component_ids = " + ".join(html.escape(item["candidate_id"]) for item in dossier["components"])
        unresolved = ", ".join(html.escape(item) for item in dossier["unresolved_requirements"])
        dossier_rows.append(
            "<tr>"
            f"<td><code>{html.escape(dossier['match_id'])}</code></td>"
            f"<td>{component_ids}</td>"
            f"<td>{html.escape(dossier['status'])}</td>"
            f"<td>{html.escape(dossier['compatibility'])}</td>"
            f"<td>{unresolved}</td>"
            "</tr>"
        )

    message_html = f'<div class="message">{html.escape(message)}</div>' if message else ""
    intake_key = f"web-ingest-{uuid.uuid4().hex}"
    match_key = f"web-match-{uuid.uuid4().hex}"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ARCHOTRAZ — Warden Console</title>
<style>
:root {{ color-scheme: dark; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }}
body {{ margin:0; background:#0b0d10; color:#e9edf1; }}
header {{ padding:22px 28px; border-bottom:1px solid #2a3038; background:#10141a; }}
main {{ padding:24px 28px 48px; max-width:1500px; margin:auto; }}
h1 {{ margin:0 0 6px; letter-spacing:.12em; font-size:24px; }}
h2 {{ margin-top:28px; font-size:17px; }}
p.subtle, .subtle {{ color:#9ba7b4; }}
.badges {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:14px; }}
.badge {{ border:1px solid #39424e; border-radius:999px; padding:6px 9px; font-size:12px; background:#151a21; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin:18px 0; }}
.stat {{ border:1px solid #2b333d; padding:14px; background:#11161c; border-radius:8px; }}
.stat strong {{ display:block; font-size:24px; margin-top:4px; }}
.panel {{ border:1px solid #2b333d; background:#11161c; padding:18px; border-radius:8px; margin:16px 0; overflow:auto; }}
form {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; }}
form.inline {{ display:inline-flex; margin:2px 4px 2px 0; }}
input, select, button, textarea {{ background:#0c1117; color:#e9edf1; border:1px solid #3a4654; border-radius:5px; padding:8px; font:inherit; }}
input[type=file] {{ max-width:360px; }}
button {{ cursor:pointer; background:#1a2530; }}
button:hover {{ background:#263647; }}
table {{ border-collapse:collapse; width:100%; min-width:900px; }}
th, td {{ text-align:left; vertical-align:top; border-bottom:1px solid #27303a; padding:9px; font-size:12px; }}
th {{ color:#aeb8c3; }}
code {{ color:#cfe4ff; }}
.message {{ border:1px solid #46617c; background:#122131; padding:10px 12px; border-radius:6px; margin:14px 0; }}
.warning {{ border-left:3px solid #8f7c49; padding-left:10px; }}
</style>
</head>
<body>
<header>
  <h1>ARCHOTRAZ</h1>
  <div class="subtle">Warden local operations console · canonical state remains SQLite + immutable evidence artifacts</div>
  <div class="badges">
    {_badge("Untrusted execution", capabilities["untrusted_execution"])}
    {_badge("Automatic scoring", capabilities["automatic_scoring"])}
    {_badge("Automatic cell assignment", capabilities["automatic_cell_assignment"])}
    {_badge("Model API required", capabilities["model_api_required"])}
  </div>
</header>
<main>
{message_html}
<section class="grid">
  {''.join(f'<div class="stat"><span>{html.escape(key)}</span><strong>{value}</strong></div>' for key, value in counts.items())}
</section>

<section class="panel">
<h2>Upload snapshot</h2>
<p class="subtle">Static intake only. Uploaded bytes are preserved exactly and are not executed.</p>
<form method="post" action="/intake" enctype="multipart/form-data">
  <input type="text" name="source_uri" placeholder="manual://repo-name" required size="32">
  <input type="text" name="description" placeholder="submitted claim / note" size="38">
  <input type="file" name="snapshot" required>
  <input type="hidden" name="idempotency_key" value="{intake_key}">
  <button type="submit">Ingest snapshot</button>
</form>
</section>

<section class="panel">
<h2>Candidate population</h2>
<p class="subtle warning">Cell assignment is manual until the historical policy is explicitly activated and measured. Holds are recoverable history, not deletion.</p>
<table><thead><tr><th>ID</th><th>Source</th><th>Eligibility</th><th>Cell</th><th>Actions</th></tr></thead>
<tbody>{''.join(candidate_rows) if candidate_rows else '<tr><td colspan="5">No candidates yet.</td></tr>'}</tbody></table>
</section>

<section class="panel">
<h2>Guards → pair universe</h2>
<p class="subtle">Enumerates the complete unordered pair universe for current candidates; held candidates are counted as exclusions.</p>
<form method="post" action="/matches">
  <input type="hidden" name="idempotency_key" value="{match_key}">
  <button type="submit">Generate proposed matches</button>
</form>
</section>

<section class="panel">
<h2>Kitchen proposals</h2>
<p class="subtle warning">A dossier here is a hypothesis. Compatibility remains UNKNOWN and no improvement is claimed until later validation.</p>
<table><thead><tr><th>Match</th><th>Components</th><th>Status</th><th>Compatibility</th><th>Still required</th></tr></thead>
<tbody>{''.join(dossier_rows) if dossier_rows else '<tr><td colspan="5">No Kitchen proposals yet.</td></tr>'}</tbody></table>
</section>
</main>
</body>
</html>"""


def _parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    pseudo_message = (
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + body
    )
    message = BytesParser(policy=default).parsebytes(pseudo_message)
    if not message.is_multipart():
        raise ValueError("expected multipart/form-data")
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename is None:
            fields[name] = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        else:
            files[name] = (filename, payload)
    return fields, files


def make_handler(warden: Warden) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ArchotrazLocal/0.1"

        def log_message(self, format: str, *args: object) -> None:
            return

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, message: str = "") -> None:
            target = "/"
            if message:
                from urllib.parse import quote

                target += "?message=" + quote(message)
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", target)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                return b""
            if length > MAX_REQUEST_BYTES:
                raise ValueError(f"request exceeds {MAX_REQUEST_BYTES} byte local limit")
            return self.rfile.read(length)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/state":
                payload = (json.dumps(state_payload(warden), indent=2, sort_keys=True) + "\n").encode("utf-8")
                self._send(HTTPStatus.OK, "application/json; charset=utf-8", payload)
                return
            if parsed.path == "/":
                message = parse_qs(parsed.query).get("message", [""])[0]
                body = render_dashboard(warden, message=message).encode("utf-8")
                self._send(HTTPStatus.OK, "text/html; charset=utf-8", body)
                return
            self._send(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"not found\n")

        def do_POST(self) -> None:  # noqa: N802
            try:
                origin = self.headers.get("Origin")
                host = self.headers.get("Host")
                if not _is_allowed_local_host(host):
                    raise ValueError("mutating requests require a local Host header")
                if origin and origin != f"http://{host}":
                    raise ValueError("cross-origin writes are not accepted by the local console")
                parsed = urlparse(self.path)
                body = self._read_body()
                content_type = self.headers.get("Content-Type", "")
                if parsed.path == "/intake":
                    fields, files = _parse_multipart(content_type, body)
                    if "snapshot" not in files:
                        raise ValueError("snapshot file is required")
                    filename, snapshot_bytes = files["snapshot"]
                    result = warden.ingest_snapshot(
                        source_uri=fields.get("source_uri", "").strip(),
                        snapshot_bytes=snapshot_bytes,
                        filename=filename or "snapshot.bin",
                        description=fields.get("description", ""),
                        idempotency_key=fields.get("idempotency_key", "").strip() or f"web-{uuid.uuid4().hex}",
                    )
                    self._redirect(f"Ingested {result['candidate_id']} / {result['snapshot_id']}")
                    return

                values = parse_qs(body.decode("utf-8", errors="replace"))
                if parsed.path == "/matches":
                    key = values.get("idempotency_key", [f"web-{uuid.uuid4().hex}"])[0]
                    report = warden.generate_matches(idempotency_key=key)
                    self._redirect(
                        f"Pair universe {report['declared_pair_universe']}: "
                        f"{report['generated_pairs']} proposed, {report['excluded_pairs']} excluded"
                    )
                    return

                segments = [segment for segment in parsed.path.split("/") if segment]
                if len(segments) == 3 and segments[0] == "candidate":
                    candidate_id, action = segments[1], segments[2]
                    key = values.get("idempotency_key", [""])[0].strip()
                    if not key:
                        raise ValueError("idempotency_key is required for candidate transitions")
                    try:
                        expected_version = int(values.get("expected_version", [""])[0])
                    except ValueError as exc:
                        raise ValueError("expected_version is required for candidate transitions") from exc
                    if action == "exclude":
                        reason = values.get("reason", ["manual hold"])[0]
                        warden.exclude_candidate(
                            candidate_id,
                            reason=reason,
                            expected_version=expected_version,
                            idempotency_key=key,
                        )
                        self._redirect(f"Held {candidate_id}")
                        return
                    if action == "restore":
                        warden.restore_candidate(
                            candidate_id,
                            expected_version=expected_version,
                            idempotency_key=key,
                        )
                        self._redirect(f"Restored {candidate_id}")
                        return
                    if action == "cell":
                        cell = values.get("cell", [""])[0]
                        warden.assign_cell(
                            candidate_id,
                            cell=cell,
                            expected_version=expected_version,
                            idempotency_key=key,
                        )
                        self._redirect(f"Assigned {candidate_id} to {cell}")
                        return
                self._send(HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"not found\n")
            except (ValueError, ArchotrazError) as exc:
                self._send(HTTPStatus.BAD_REQUEST, "text/plain; charset=utf-8", f"{exc}\n".encode("utf-8"))

    return Handler


def serve(
    warden: Warden,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
) -> None:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("the built-in console is intentionally local-only")
    server = ThreadingHTTPServer((host, port), make_handler(warden))
    url = f"http://{host}:{server.server_port}/"
    print(f"ARCHOTRAZ Warden console: {url}")
    if open_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
