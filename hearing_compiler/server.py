"""Local browser interface; deliberately bound to loopback."""

from __future__ import annotations

import json
import mimetypes
import re
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import TCPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .pipeline import InputError
from .providers import normalize_macwhisper
from .subtitle_import import normalize_transcript_file
from .store import Conflict, Store
from .audio import AudioQueue, audio_path, byte_range, receive_audio

ROOT = Path(__file__).resolve().parent.parent


class WorkspaceLease:
    """One server owns a workspace; OS locks release automatically after a crash."""

    def __init__(self, state: Path):
        state.mkdir(parents=True, exist_ok=True)
        self.handle = (state / "workspace.lock").open("a+b")
        self.held = False
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.held = True
        except OSError as error:
            self.handle.close()
            raise InputError("This saved workspace is already open in another app instance. Use that window or stop it before reopening.") from error

    def close(self):
        if self.held:
            self.handle.seek(0)
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.held = False
        if not self.handle.closed:
            self.handle.close()


def make_server(state: Path, port: int = 8765, seed: bool = False, transcription_provider=None,
                archive_samples: bool = True, extraction_provider=None) -> ThreadingHTTPServer:
    if seed:
        raise InputError("Built-in sample seeding is retired. Import developer fixtures explicitly.")
    lease = WorkspaceLease(state)
    try:
        store = Store(state)
        if archive_samples:
            store.archive_legacy_samples()
    except Exception:
        lease.close()
        raise
    token = secrets.token_urlsafe(32)
    stop_event = threading.Event()
    from .extraction import ExtractionService
    extraction = ExtractionService(store, stop_event)
    store.extractor = extraction_provider or extraction
    queue = AudioQueue(store, transcription_provider, stop_event=stop_event)
    store.on_queued = queue.wake.set
    from .backup_download import BackupDownloads, COOKIE, DOWNLOAD_PATH, download_cookie
    downloads = BackupDownloads(store)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            # An abandoned upload must not keep ownership indefinitely.
            self.connection.settimeout(15)

        def log_message(self, format, *args):
            if args and isinstance(args[0], str):
                print(args[0])

        def send(self, status: int, payload, content_type="application/json; charset=utf-8", filename=None, cookie=None):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if isinstance(payload, (dict, list)) else payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if filename is not None:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            if cookie is not None:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def allowed_host(self):
            return self.headers.get("Host") in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

        def do_GET(self):
            if not self.allowed_host():
                return self.send(403, {"error": "Use the local application address."})
            path = unquote(urlparse(self.path).path)
            try:
                if path == "/api/session":
                    return self.send(200, {"token": token})
                if path == "/api/version":
                    from .version import info
                    return self.send(200, info())
                if path == "/api/update":
                    from .updater import status
                    return self.send(200, status(store))
                if path == DOWNLOAD_PATH:
                    return self.send_backup_download()
                if path == "/api/transcription":
                    return self.send(200, queue.info())
                if path == "/api/extraction":
                    return self.send(200, extraction.info())
                if path == "/api/workspace":
                    return self.send(200, {"directory": str(store.directory), "hearings": len(store.list()),
                                           "set_aside": len(store.data.get("set_aside", [])),
                                           "backup_includes_recordings": True, "restore_requires_empty": True,
                                           "backup_cleanup_pending": bool(downloads.cleanup_pending)
                                           or bool(getattr(self.server, "backup_cleanup_error", None))})
                if path == "/api/hearings":
                    return self.send(200, store.list())
                if path.startswith("/api/export/"):
                    return self.send_export(path[len("/api/export/"):])
                if path.startswith("/api/hearings/"):
                    return self.send(200, store.get(path[len("/api/hearings/"):]))
                if path.startswith("/api/audio/"):
                    return self.send_audio(path[len("/api/audio/"):])
                names = {"/": "index.html", "/app.js": "app.js", "/rich.js": "rich.js", "/theme.js": "theme.js",
                         "/review_focus.js": "review_focus.js", "/styles.css": "styles.css"}
                if path not in names:
                    return self.send(404, {"error": "Not found."})
                target = ROOT / "web" / names[path]
                return self.send(200, target.read_bytes(), mimetypes.guess_type(target.name)[0] + "; charset=utf-8")
            except KeyError:
                self.send(404, {"error": "Hearing not found."})
            except Conflict as error:
                self.send(409, {"error": str(error)})
            except InputError as error:
                self.send(400, {"error": str(error)})
            except OSError as error:
                self.send(500, {"error": str(error)})

        def send_export(self, job_id: str):
            query = parse_qs(urlparse(self.path).query)
            format_name = query.get("format", ["docx"])[0]
            if format_name not in ("docx", "json", "md"):
                raise InputError("Choose Word (.docx), saved review data (.json), or Markdown (.md).")
            raw_revision = query.get("revision", [None])[0]
            if raw_revision is not None and not re.fullmatch(r"[0-9]+", raw_revision):
                raise InputError("The export revision must be a nonnegative integer.")
            snapshot = store.export_snapshot(job_id, int(raw_revision) if raw_revision is not None else None)
            job = snapshot["job"]
            stem = re.sub(r"[^A-Za-z0-9._-]+", "-", job["hearing"]["title"]).strip(".-")[:80] or "hearing"
            filename = f"{stem}-notes.{format_name}"
            if format_name == "json":
                payload = json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
                return self.send(200, payload, "application/json; charset=utf-8", filename)
            if format_name == "md":
                return self.send(200, (job.get("notion") or job["markdown"]).encode("utf-8"),
                                 "text/markdown; charset=utf-8", filename)
            from .word_export import docx_bytes
            draft = job["draft"]
            draft["export_revision"] = job["revision"]
            draft["export_saved_at"] = job.get("updated_at")
            return self.send(200, docx_bytes(draft),
                             "application/vnd.openxmlformats-officedocument.wordprocessingml.document", filename)

        def send_backup_download(self):
            from http.cookies import SimpleCookie, CookieError
            if self.server.closing.is_set():
                return self.send(503, {"error": "The workspace is stopping."})
            origin = self.headers.get("Origin")
            site = self.headers.get("Sec-Fetch-Site")
            if (origin and origin not in (f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}")
                    or site is not None and site not in {"same-origin", "none"}):
                return self.send(403, {"error": "Download the backup from this local app."})
            try:
                cookies = SimpleCookie(self.headers.get("Cookie", ""))
                key = cookies[COOKIE].value if COOKIE in cookies else None
            except CookieError:
                key = None
            if not key:
                return self.send(403, {"error": "Prepare a full backup in this app before downloading."})
            with downloads.take(key) as (source, size):
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", 'attachment; filename="hearing-workspace-backup.zip"')
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Set-Cookie", download_cookie(expires=True))
                self.end_headers()
                try:
                    while block := source.read(1024 * 1024):
                        self.wfile.write(block)
                except OSError:
                    # Headers are already sent. Closing signals a failed transfer;
                    # never append a JSON error body to the ZIP stream.
                    self.close_connection = True

        def send_audio(self, job_id: str, head_only=False):
            try:
                target, asset = audio_path(store, job_id)
            except (KeyError, InputError):
                return self.send(404, {"error": "The source recording is not available."})
            size = target.stat().st_size
            try:
                start, end, partial = byte_range(self.headers.get("Range"), size)
            except InputError:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", asset["content_type"])
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "private, max-age=3600")
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if head_only:
                return
            try:
                with target.open("rb") as audio:
                    audio.seek(start)
                    remaining = end - start + 1
                    while remaining:
                        block = audio.read(min(65536, remaining))
                        if not block:
                            break
                        self.wfile.write(block)
                        remaining -= len(block)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_HEAD(self):
            if not self.allowed_host():
                return self.send(403, {"error": "Use the local application address."})
            path = unquote(urlparse(self.path).path)
            if path.startswith("/api/audio/"):
                return self.send_audio(path[len("/api/audio/"):], head_only=True)
            self.send(404, {"error": "Not found."})

        def do_POST(self):
            if self.server.closing.is_set():
                return self.send(503, {"error": "The workspace is stopping. Reopen the app before changing saved work."})
            if not self.allowed_host() or self.headers.get("X-Review-Token") != token:
                return self.send(403, {"error": "Reload the local application before saving."})
            origin = self.headers.get("Origin")
            if origin and origin not in (f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"):
                return self.send(403, {"error": "Only the local application can change saved work."})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                path = urlparse(self.path).path
                if path == "/api/workspace/backup-prepare":
                    return self.send(200, {"download_url": DOWNLOAD_PATH},
                                     cookie=download_cookie(downloads.prepare()))
                if path == "/api/workspace/backup":
                    from .workspace_backup import create_backup, verify_backup
                    target = create_backup(store)
                    try:
                        verify_backup(target)
                        size = target.stat().st_size
                        self.send_response(200)
                        self.send_header("Content-Type", "application/zip")
                        self.send_header("Content-Disposition", 'attachment; filename="hearing-workspace-backup.zip"')
                        self.send_header("Content-Length", str(size))
                        self.send_header("Cache-Control", "no-store")
                        self.end_headers()
                        with target.open("rb") as archive:
                            while block := archive.read(1024 * 1024):
                                self.wfile.write(block)
                    finally:
                        target.unlink(missing_ok=True)
                    return
                if path == "/api/workspace/restore":
                    from .workspace_backup import MAX_BACKUP_BYTES, restore_backup
                    if not 0 < length <= MAX_BACKUP_BYTES:
                        raise InputError("Choose a nonempty full workspace ZIP within the supported size.")
                    if store.list():
                        raise Conflict("Restore requires a new empty workspace; existing reviews are preserved.")
                    target = store.directory / f".restore-upload-{secrets.token_hex(16)}.zip"
                    try:
                        with target.open("xb") as output:
                            remaining = length
                            while remaining:
                                block = self.rfile.read(min(1024 * 1024, remaining))
                                if not block:
                                    raise InputError("Backup upload was interrupted; no saved state was replaced.")
                                output.write(block)
                                remaining -= len(block)
                        result = restore_backup(store, target)
                        store.reset_stage_timers()
                        queue.wake.set()
                        return self.send(200, result)
                    finally:
                        target.unlink(missing_ok=True)
                if path == "/api/audio/upload":
                    from urllib.parse import parse_qs
                    query = parse_qs(urlparse(self.path).query)
                    metadata = json.loads(query.get("metadata", ["{}"])[0])
                    if not isinstance(metadata, dict) or metadata.get("source_scope") != "public_hearing":
                        raise InputError("Declare that the recording is a public hearing.")
                    if not metadata.get("attach_to") and any(
                        not isinstance(metadata.get(key), str) or not metadata[key].strip()
                        for key in ("title", "date", "committee", "source_file")
                    ):
                        raise InputError("Hearing title, date, committee and source filename are required.")
                    asset = receive_audio(store.directory, self.rfile, length, metadata.get("source_file", ""))
                    try:
                        if metadata.get("attach_to"):
                            result = store.attach_audio(metadata["attach_to"], asset)
                        else:
                            result = store.enqueue_audio(metadata, asset, queue.available())
                            queue.wake.set()
                    except Exception:
                        store.discard_unused_audio(asset)
                        raise
                    return self.send(200, result)
                if length <= 0 or length > 8 * 1024 * 1024:
                    raise InputError("Use a JSON request smaller than 8 MB.")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise InputError("The request must be a JSON object.")
                if path == "/api/import":
                    result = store.import_batch(body.get("documents"))
                elif path == "/api/transcription/check":
                    result = queue.models()
                elif path == "/api/extraction/configure":
                    # The pasted key goes only to the Keychain; it is never echoed or saved in state.
                    result = extraction.configure(body.get("provider"), body.get("model"), body.get("api_key"))
                elif path == "/api/extraction/forget-key":
                    result = extraction.forget_key()
                elif path == "/api/transcription/configure":
                    queue.configure(body.get("model_id", ""), body.get("output_format", "json"))
                    result = queue.info()
                elif path == "/api/review":
                    result = store.review(body.get("id"), body.get("revision"), body.get("changes"),
                                          body.get("operations"), body.get("request_id"))
                elif path == "/api/macwhisper":
                    result = store.import_one(normalize_macwhisper(
                        body.get("export"), body.get("hearing"), body.get("synthetic", True)))
                elif path == "/api/text":
                    if body.get("attach_to"):
                        job = store.get(body["attach_to"])
                        document = normalize_transcript_file(body.get("text"), job["hearing"], body.get("filename"))
                        store.cache_transcript(job["id"], document)
                        queue.wake.set()
                        result = store.get(job["id"])
                    else:
                        if body.get("synthetic") is not True and body.get("source_scope") != "public_hearing":
                            raise InputError("Declare that this text is a public hearing transcript.")
                        from .agenda import check_url
                        if isinstance(body.get("hearing"), dict) and body["hearing"].get("hearing_url") is not None:
                            body["hearing"]["hearing_url"] = check_url(body["hearing"]["hearing_url"])
                            if body["hearing"]["hearing_url"] is None:
                                del body["hearing"]["hearing_url"]
                        document = normalize_transcript_file(
                            body.get("text"), body.get("hearing"), body.get("filename"),
                            synthetic=body.get("synthetic", False))
                        if (body.get("hearing") or {}).get("hearing_url"):
                            # validate() keeps five hearing fields; keep the link where cache_transcript puts it.
                            document["hearing_url"] = body["hearing"]["hearing_url"]
                        result = store.import_one(document)
                elif path == "/api/audio/transcript":
                    job = store.get(body.get("id"))
                    document = normalize_macwhisper(body.get("export"), job["hearing"], synthetic=False)
                    if job["processing"] in ("Transcribing", "Analyzing"):
                        raise Conflict("This hearing is already processing. Wait for its result before attaching a transcript.")
                    store.cache_transcript(job["id"], document)
                    queue.wake.set()
                    result = store.get(job["id"])
                elif path == "/api/retry":
                    job = store.get(body.get("id"))
                    result = queue.retry(job["id"]) if job.get("audio") else store.retry(body.get("id"), body.get("document"))
                elif path == "/api/redraft":
                    result = store.redraft(body.get("id"))
                elif path == "/api/rebuild":
                    result = store.rebuild(body.get("id"))
                elif path == "/api/shutdown":
                    self.send(200, {"stopping": True})
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                elif path == "/api/update/install":
                    from .updater import install
                    result = install(store)
                    # The staged swap waits for this process to exit, so stop even if the tab was closed.
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    try:
                        self.send(200, result)
                    except OSError:
                        pass
                    return
                else:
                    return self.send(404, {"error": "Unknown action."})
                self.send(200, result)
            except Conflict as error:
                self.send(409, {"error": str(error)})
            except KeyError:
                self.send(404, {"error": "Hearing not found."})
            except (InputError, ValueError, TypeError) as error:
                self.send(400, {"error": str(error)})
            except Exception:
                self.send(500, {"error": "The operation failed. Saved work remains on disk; inspect the server log."})
                raise

    class LocalServer(ThreadingHTTPServer):
        daemon_threads = False
        block_on_close = True

        def server_bind(self):
            # The address is deliberately literal loopback. Reverse DNS adds
            # no authority or functionality and can stall platform startup.
            TCPServer.server_bind(self)
            self.server_name, self.server_port = self.server_address

        def __init__(self, *args, **kwargs):
            self.closing = threading.Event()
            super().__init__(*args, **kwargs)

        def shutdown(self):
            self.closing.set()
            downloads.stop_accepting()
            queue.stop_event.set()
            queue.wake.set()
            super().shutdown()

        def service_actions(self):
            super().service_actions()
            try:
                downloads.prune()
                self.backup_cleanup_error = None
            except OSError as error:
                self.backup_cleanup_error = str(error)

        def server_close(self):
            self.closing.set()
            downloads.stop_accepting()
            queue.stop_event.set()
            queue.wake.set()
            # Non-daemon request writers drain before ownership can transfer.
            super().server_close()
            queue.close()
            try:
                downloads.close()
            finally:
                if not queue.worker.is_alive():
                    lease.close()

    try:
        server = LocalServer(("127.0.0.1", port), Handler)
    except OSError as error:
        queue.close()
        lease.close()
        raise InputError(f"Cannot open the local app port. Try --port 0 to choose a free port: {error}") from error
    server.store = store
    server.audio_queue = queue
    server.backup_downloads = downloads
    return server
