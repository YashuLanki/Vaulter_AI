"""
ingestion/registry.py
---------------------
Tracks which files have already been ingested using SHA-256 file hashing.
Prevents duplicate documents from being stored in ChromaDB.
Now also records property, state, and category for each ingested file.
"""

import hashlib
from datetime import datetime
from pathlib import Path

import safe_io
from config import REGISTRY_FILE


def load_registry() -> dict:
    """Load the ingestion registry from disk. Returns empty dict if none
    exists or if it's corrupt (e.g. a crash mid-write) -- corruption is
    logged as a warning rather than crashing every future ingestion."""
    return safe_io.load_json(REGISTRY_FILE)


def save_registry(registry: dict):
    """Save the ingestion registry to disk atomically (temp file + rename,
    so a crash mid-write can't leave a truncated/corrupt file)."""
    safe_io.save_json_atomic(REGISTRY_FILE, registry)


def get_file_hash(path: Path) -> str:
    """
    Compute a SHA-256 hash of a file's contents.
    Used to detect duplicate files regardless of filename.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def is_already_ingested(file_hash: str) -> bool:
    """Return True if this file hash exists in the registry."""
    registry = load_registry()
    return file_hash in registry


def record_ingestion(
    file_hash: str,
    filename: str,
    chunks: int,
    pages: int,
    ocr_used: bool,
    property_name: str = "unknown",
    state: str = "unknown",
    category: str = "unknown",
):
    """Add a successfully ingested file to the registry. Uses a file lock
    around the read-modify-write so two files finishing ingestion at
    close to the same moment (e.g. a startup batch scan racing a live
    watcher event) can't have one's record silently overwrite the
    other's."""
    entry = {
        "filename":     filename,
        "ingested_at":  datetime.now().isoformat(),
        "chunks":       chunks,
        "pages":        pages,
        "ocr_used":     ocr_used,
        "property":     property_name,
        "state":        state,
        "category":     category,
    }
    safe_io.locked_json_update(REGISTRY_FILE, lambda current: {**current, file_hash: entry})
