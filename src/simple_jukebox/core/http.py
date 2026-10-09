"""Shared HTTPS context for every urllib request the app makes.

A PyInstaller-frozen app on macOS can end up with a `ssl` module whose
default CA certificate path was baked in at build time — wherever the
build machine's Python installation kept its cert bundle. That path
simply doesn't exist on an end user's machine, so every HTTPS request
fails with a certificate-verify error, which every caller here treats
the same as "offline" and silently swallows. Explicitly pointing at
certifi's bundled, portable CA file sidesteps this regardless of which
machine built or is running the app.
"""
from __future__ import annotations

import ssl

import certifi

SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
