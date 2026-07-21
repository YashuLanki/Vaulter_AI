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


def _entries_for(registry: dict, file_hash: str) -> list:
    """Normalise a registry value to a list of ingestion records.

    Older registries stored one dict per hash (a hash could only ever be
    ingested once, full stop). Values are now a list, because the exact
    same physical file legitimately gets dropped into more than one
    property's folder (e.g. a shared county plat map or boilerplate legal
    doc) and each property needs its own tagged copy in ChromaDB. Reading
    an old single-dict entry as a 1-item list keeps existing registries
    valid without a destructive migration.
    """
    value = registry.get(file_hash)
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def is_already_ingested(file_hash: str, property_name: str = None, state: str = None) -> bool:
    """
    Return True if this exact file has already been ingested for this
    property. Dedup is scoped per (hash, property) rather than per hash
    alone -- otherwise the same physical file shared across two different
    properties would be silently skipped (and never moved out of
    watched_folder) the second time, instead of being ingested and tagged
    for the new property.

    If property_name/state are omitted, falls back to "ingested for ANY
    property" (used by callers that only care about hash existence).
    """
    registry = load_registry()
    entries = _entries_for(registry, file_hash)
    if not entries:
        return False
    if property_name is None:
        return True
    prop_l  = property_name.lower()
    state_l = (state or "").lower()
    return any(
        e.get("property", "").lower() == prop_l and e.get("state", "").lower() == state_l
        for e in entries
    )


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
    """Add a successfully ingested file to the registry. Appends to the
    hash's entry list rather than overwriting, so the same file ingested
    for a second property keeps the first property's record instead of
    clobbering it. Uses a file lock around the read-modify-write so two
    files finishing ingestion at close to the same moment (e.g. a startup
    batch scan racing a live watcher event) can't have one's record
    silently overwrite the other's."""
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

    def _update(current: dict) -> dict:
        existing = _entries_for(current, file_hash)
        return {**current, file_hash: existing + [entry]}

    safe_io.locked_json_update(REGISTRY_FILE, _update)
