"""ShardVault web application.

Serves the dashboard and a small JSON API:

    GET    /api/status                  disks, files and erasure-coding health
    POST   /api/files                   upload a file (multipart field "file")
    GET    /api/files/<key>             download a file (?inline=1 to display it)
    POST   /api/files/<key>/verify      re-read a file and compare its SHA-256
    DELETE /api/files/<key>             delete a file
    POST   /api/disks/<n>/destroy       erase everything on disk n
    POST   /api/repair                  ask MinIO to rebuild missing pieces now
    GET    /healthz                     liveness check
"""

from __future__ import annotations

import logging
import os

from flask import Flask, Response, jsonify, render_template, request

from vault import NotFound, Settings, Vault, VaultUnavailable, safe_key

MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "100"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("shardvault")


def create_app(vault: Vault | None = None) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
    app.vault = vault or Vault(Settings.from_env())

    @app.errorhandler(VaultUnavailable)
    def unavailable(exc):
        return jsonify(error="unavailable", message=str(exc)), 503

    @app.errorhandler(NotFound)
    def not_found(exc):
        return jsonify(error="not_found", message="That file or disk does not exist."), 404

    @app.errorhandler(413)
    def too_large(_exc):
        return jsonify(error="too_large", message=f"Files can be up to {MAX_UPLOAD_MB} MB."), 413

    @app.get("/")
    def index():
        s = app.vault.settings
        return render_template("index.html", console_url=s.console_url, disk_count=s.disk_count, max_mb=MAX_UPLOAD_MB)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/api/status")
    def status():
        return jsonify(app.vault.status())

    @app.post("/api/files")
    def upload():
        file = request.files.get("file")
        if file is None or not file.filename:
            return jsonify(error="no_file", message="Choose a file to upload."), 400
        result = app.vault.upload(file.stream, file.filename, file.mimetype)
        log.info("Uploaded %s (%d bytes)", result["key"], result["size"])
        return jsonify(result), 201

    @app.get("/api/files/<key>")
    def download(key):
        obj = app.vault.open(safe_key(key))
        inline = request.args.get("inline") == "1"
        disposition = "inline" if inline else f'attachment; filename="{safe_key(key)}"'
        return Response(
            obj["Body"].iter_chunks(1024 * 256),
            content_type=obj.get("ContentType") or "application/octet-stream",
            headers={
                "Content-Length": str(obj["ContentLength"]),
                "Content-Disposition": disposition,
                "Cache-Control": "no-store",
            },
        )

    @app.post("/api/files/<key>/verify")
    def verify(key):
        result = app.vault.verify(safe_key(key))
        log.info("Verified %s: intact=%s in %.3fs", key, result["intact"], result["seconds"])
        return jsonify(result)

    @app.delete("/api/files/<key>")
    def delete(key):
        app.vault.delete(safe_key(key))
        log.info("Deleted %s", key)
        return jsonify(deleted=safe_key(key))

    @app.post("/api/disks/<int:number>/destroy")
    def destroy(number):
        removed = app.vault.destroy_disk(number)
        log.warning("Destroyed disk %d (%d top-level entries removed)", number, removed)
        return jsonify(disk=number, removed=removed)

    @app.post("/api/repair")
    def repair():
        result = app.vault.repair()
        log.info("Repair started: %s", result)
        return jsonify(started=True, details=result)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
