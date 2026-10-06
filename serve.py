"""
serve.py — production entrypoint using the Waitress WSGI server.

Waitress is a pure-Python, production-grade WSGI server that runs the same on
Windows, Linux, and macOS. Put a reverse proxy (nginx, Azure App Service, etc.)
in front of it for TLS termination and set TRUST_PROXY=true.
"""

from __future__ import annotations

import logging

from waitress import serve

import config
from app import app, APP_VERSION

if __name__ == "__main__":
    logging.getLogger("transition-agent").info(
        "Transition Agent v%s serving on http://%s:%s (threads=%s)",
        APP_VERSION, config.HOST, config.PORT, config.WSGI_THREADS,
    )
    serve(
        app,
        host=config.HOST,
        port=config.PORT,
        threads=config.WSGI_THREADS,
        connection_limit=config.WSGI_CONNECTION_LIMIT,
        channel_timeout=config.WSGI_CHANNEL_TIMEOUT,
        ident="TransitionAgent",
    )
