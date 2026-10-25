"""SmartLab AI - Flask application factory."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from .auth import init_auth
from .config import Config
from .db import PgPool, close_db, connect, database_target, describe_database, init_schema
from .rag.embeddings import get_embedder
from .rag.engine import RAGEngine
from .rag.vector_store import get_vector_store
from .security import FileValidationError
from .services.llm import ClaudeClient

__version__ = "1.0.0"


def create_app(config_object: type[Config] | Config = Config, **overrides) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_object)
    app.config.update(overrides)
    if app.config["TRUST_PROXY_HOPS"] > 0:
        # Behind a reverse proxy remote_addr is the proxy, so every client would share one rate-limit bucket.
        hops = app.config["TRUST_PROXY_HOPS"]
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    for key in ("DATA_DIR", "UPLOAD_DIR", "INDEX_DIR"):
        app.config[key].mkdir(parents=True, exist_ok=True)
    (app.config["UPLOAD_DIR"] / "shared").mkdir(exist_ok=True)

    # Credentials (the session cookie) are allowed only for the configured origins.
    CORS(app, resources={r"/api/*": {"origins": app.config["CORS_ORIGINS"]}}, supports_credentials=True)

    llm = ClaudeClient(app.config["LLM_MODEL"], app.config["LLM_EFFORT"], app.config["LLM_ENABLED"])
    embedder = get_embedder(app.config["EMBEDDING_BACKEND"], app.config["EMBEDDING_MODEL"])
    store = get_vector_store(app.config["VECTOR_BACKEND"], embedder.dim, app.config["INDEX_DIR"])
    rag = RAGEngine(
        embedder, store, llm,
        chunk_size=app.config["CHUNK_SIZE"], chunk_overlap=app.config["CHUNK_OVERLAP"],
        top_k=app.config["TOP_K"], min_score=app.config.get("MIN_EVIDENCE_SCORE"),
    )
    app.extensions["smartlab"] = {"rag": rag, "llm": llm}

    app.logger.info("Database: %s", describe_database(app.config))
    conn = connect(database_target(app.config))
    try:
        init_schema(conn)
        rag.ensure_index(conn)
        if app.config["SEED_DEMO_DATA"]:
            from .seed import seed_agent_demo, seed_compounds, seed_documents, seed_records

            seed_records(conn)
            seed_compounds(conn)
            seed_documents(conn, rag, app.config["UPLOAD_DIR"], app.config["MAX_CONTENT_LENGTH"])
            if app.config.get("SEED_AGENT_DEMO", True):
                seed_agent_demo(conn, rag, app.config["UPLOAD_DIR"], app.config["MAX_CONTENT_LENGTH"])
    finally:
        conn.close()

    if app.config["DATABASE_ENGINE"] == "postgresql":
        app.extensions["smartlab_db_pool"] = PgPool(app.config["DATABASE_URL"], app.config["DB_POOL_MAX"])
    app.teardown_appcontext(close_db)
    init_auth(app)
    from .tenancy import init_tenancy
    init_tenancy(app)
    _register_blueprints(app)
    _register_error_handlers(app)

    @app.get("/api/health")
    def health():
        return jsonify({
            "status": "ok",
            "version": __version__,
            "database": app.config["DATABASE_ENGINE"],
            "embeddings": embedder.name,
            "vector_store": store.backend,
            "indexed_chunks": len(store),
            "llm": {"enabled": llm.available, "model": llm.model if llm.available else None},
            "min_evidence_score": rag.min_score,
            "literature_online": app.config["LITERATURE_ONLINE"],
            "max_upload_mb": app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024),
        })

    # Serve built frontend if dist folder exists (e.g. Render / production deployment)
    dist_dir = app.config.get("FRONTEND_DIST") or Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
    dist_dir = Path(dist_dir)
    if not dist_dir.is_dir():
        dist_env = os.getenv("FRONTEND_DIST")
        dist_dir = Path(dist_env) if dist_env else None

    if dist_dir and dist_dir.is_dir():
        @app.route("/", defaults={"path": ""})
        @app.route("/<path:path>")
        def serve_frontend(path: str):
            if path.startswith("api/"):
                return jsonify({"error": "Not found"}), 404
            file_target = dist_dir / path
            if path and file_target.is_file():
                resp = send_from_directory(dist_dir, path)
                if path.startswith("assets/"):  # content-hashed by Vite: cacheable forever, at the edge too
                    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
                return resp
            resp = send_from_directory(dist_dir, "index.html")
            resp.headers["Cache-Control"] = "no-cache"  # always revalidate so a new deploy is picked up
            return resp

    return app


def _register_blueprints(app: Flask) -> None:
    from .api import (agent, auth, chat, collaboration, data, documents, experiments, literature, profiles, reagents,
                      safety, search, simulation, teams, troubleshooting, vision)

    for module in (agent, auth, chat, collaboration, data, documents, experiments, literature, profiles, reagents,
                   safety, search, simulation, teams, troubleshooting, vision):
        app.register_blueprint(module.bp)
    app.register_blueprint(teams.invitations_bp)


def _register_error_handlers(app: Flask) -> None:
    from .api.common import ApiError

    @app.errorhandler(ApiError)
    def handle_api_error(err: ApiError):
        return jsonify({"error": err.message, **err.extra}), err.status

    @app.errorhandler(FileValidationError)
    def handle_file_error(err: FileValidationError):
        return jsonify({"error": err.message}), err.status

    @app.errorhandler(HTTPException)
    def handle_http(err: HTTPException):
        message = "Uploaded file is too large." if err.code == 413 else err.description
        return jsonify({"error": message}), err.code

    @app.errorhandler(Exception)
    def handle_unexpected(err: Exception):
        app.logger.exception("Unhandled error: %s", err)
        return jsonify({"error": "Internal server error."}), 500
