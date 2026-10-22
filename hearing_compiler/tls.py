"""HTTPS trust for the packaged app.

Bundled Python on macOS ships without a certificate bundle, so HTTPS (updates and
Gemini) would fail. On macOS, add the system's trusted roots, including any
roots an organization installed, from the Keychain.
"""
from __future__ import annotations

from functools import lru_cache
import logging
import ssl
import subprocess
import sys

KEYCHAINS = ("/System/Library/Keychains/SystemRootCertificates.keychain", "/Library/Keychains/System.keychain")


@lru_cache(maxsize=1)
def context() -> ssl.SSLContext:
    trust = ssl.create_default_context()
    if sys.platform == "darwin":
        for keychain in KEYCHAINS:
            try:
                exported = subprocess.run(["/usr/bin/security", "find-certificate", "-a", "-p", keychain],
                                          capture_output=True, text=True, timeout=30)
                if exported.returncode == 0 and "BEGIN CERTIFICATE" in exported.stdout:
                    trust.load_verify_locations(cadata=exported.stdout)
            except (OSError, subprocess.SubprocessError, ssl.SSLError, ValueError) as error:
                logging.warning("Could not load trusted certificates from %s: %s", keychain, error)
    return trust
