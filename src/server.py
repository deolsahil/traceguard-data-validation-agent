"""
Minimal local presentation server.

Usage:
    python src/server.py

Endpoints:
    GET  /             → traceability dashboard
    GET  /architecture → architecture & comparison page
    GET  /stream       → runs the agent, streams output as SSE
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"
SRC_DIR = Path(__file__).resolve().parent.parent
DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"
PORT = 8765
VALIDATION_LABEL = os.getenv("VALIDATION_LABEL", "validation-agent")
HOST = os.getenv("SERVER_HOST", "127.0.0.1")
PORT = int(os.getenv("SERVER_PORT", str(PORT)))
BULK_JQL = os.getenv("JIRA_BULK_JQL", f"labels = {VALIDATION_LABEL}")
_BOARD_ID = os.getenv("JIRA_BOARD_ID", "").strip()
BOARD_ID = int(_BOARD_ID) if _BOARD_ID else None


_STARTED_AT = datetime.now(timezone.utc).isoformat()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/stream":
            self._stream_agent()
            return
        if self.path == "/health":
            # Cheap enough to poll. Tells the dashboard whether pressing Run Agent
            # would actually reach anything — a page opened from a stale or stopped
            # server looks identical to a working one until you click.
            body = json.dumps({"ok": True, "started_at": _STARTED_AT}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
            return
        routes = {
            "/": OUTPUT_DIR / "traceability-dashboard.html",
            # docs/, not output/: this page is hand-authored source, not a build
            # artifact, and output/ is gitignored — served from there it 404s for
            # anyone who clones the repo.
            "/architecture": DOCS_DIR / "architecture.html",
        }
        target = routes.get(self.path)
        if target and target.exists():
            content = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            # The dashboard is rewritten by every scan and by /clear-runs, so a cached
            # copy shows work that has already happened as if it had not. Clearing the
            # history appeared to need two clicks for exactly this reason: the first
            # reload was served from cache.
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:
        if self.path == "/clear-runs":
            self._clear_runs()
            return
        self.send_response(404)
        self.end_headers()

    def _clear_runs(self) -> None:
        """
        Forget the run history, keeping the tickets.

        Only the history goes: the ticket rows are the report's actual content and are
        rebuilt from Jira, whereas runs are a local log that nothing else can recover.
        Numbering restarts at #1, and the next scan reports no changes because the
        baseline it would have diffed against is the thing being deleted — both are
        stated in the confirmation rather than discovered afterwards.
        """
        from traceability_report import generate_dashboard, load_report, save_report

        report = load_report()
        removed = len(report.get("runs") or [])
        report["runs"] = []
        save_report(report)
        generate_dashboard(report)
        print(f"  Cleared {removed} run(s) from the history")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"cleared": removed}).encode())

    def _stream_agent(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.flush()
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(
            [sys.executable, "src/main.py", "--jql", BULK_JQL]
            + (["--board-id", str(BOARD_ID)] if BOARD_ID is not None else []),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(SRC_DIR),
            env=env,
            bufsize=1,
        )
        try:
            for line in proc.stdout:
                self.wfile.write(f"data: {json.dumps(line.rstrip())}\n\n".encode())
                self.wfile.flush()
        except BrokenPipeError:
            proc.kill()
        finally:
            proc.wait()
        try:
            self.wfile.write('data: "__done__"\n\n'.encode())
            self.wfile.flush()
        except BrokenPipeError:
            pass

    def do_OPTIONS(self) -> None:
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass  # suppress per-request logs


if __name__ == "__main__":
    # Threading, not plain HTTPServer: /stream holds a connection open for the whole
    # agent run, and a single-threaded server cannot answer anything else meanwhile —
    # so the dashboard became unloadable exactly while a scan was in progress.
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Server running at http://{HOST}:{PORT}")
    print(f"  Dashboard    → http://localhost:{PORT}/")
    print(f"  Architecture → http://localhost:{PORT}/architecture")
    print(f"  Share via tunnel: ngrok http {PORT}")
    print(f"  Press Ctrl+C to stop.")
    server.serve_forever()
