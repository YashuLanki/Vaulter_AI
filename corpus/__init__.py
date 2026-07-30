"""
corpus/
-------
Read-only access to the firm's SharePoint document library, synced locally
by OneDrive (`config.CORPUS_DIR` -- the "Vaulter LLC - shaw" folder).

This package replaces the old `ingestion/` + `analysis/rag_engine.py` stack.
The previous design copied documents into `data/watched_folder/`, extracted
them, chunked them, embedded them, and stored the vectors in ChromaDB so they
could be searched semantically. That existed to work around small context
windows. The library is already on disk and Claude can read the documents
directly, so all of that machinery was a copy of something the filesystem
already had.

Two constraints shape everything here:

1. **Scope.** Every path is resolved against CORPUS_DIR and rejected if it
   escapes. The OneDrive account root one level up holds the individual's own
   Desktop, Documents, and Teams chat files; nothing in this system may read
   them. See `resolve_in_corpus`.

2. **Hydration.** The library is synced as OneDrive Files On-Demand
   placeholders. Names and folder structure are free to read; opening a file
   downloads it. So search works on paths and filenames only, and content is
   read one deliberately-chosen file at a time. See `index.py`'s header.
"""

from corpus.index import (  # noqa: F401
    CorpusUnavailable,
    OutsideCorpus,
    build_index,
    index_age,
    is_online_only,
    list_dir,
    resolve_in_corpus,
    search,
)
from corpus.extract import (  # noqa: F401
    SUPPORTED_EXTENSIONS,
    is_supported,
    read_document,
)
