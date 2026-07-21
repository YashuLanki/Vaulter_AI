"""
ingestion/chunker.py
--------------------
Splits extracted file text into overlapping chunks for storage in ChromaDB.

Chunk size is dynamic — it automatically adjusts based on document page count
so small documents get precise small chunks and large documents get bigger
contextual chunks.

Why chunking matters:
  - Claude can only search and retrieve focused pieces of text, not entire documents
  - Overlapping chunks preserve context across boundaries
  - Dynamic sizing ensures optimal retrieval regardless of document size
"""

from config import get_chunk_settings


def chunk_text(text: str, page_count: int = 1) -> list[str]:
    """
    Split text into overlapping chunks with dynamic sizing.

    Chunk size is automatically determined by page count:
      1-10 pages   → chunk_size=500,  overlap=50
      11-50 pages  → chunk_size=800,  overlap=100
      51-100 pages → chunk_size=1200, overlap=150
      100+ pages   → chunk_size=1500, overlap=200

    Args:
        text:       The full extracted text from a file
        page_count: Number of pages in the document (used to pick chunk size)

    Returns:
        List of non-empty text chunks
    """
    chunk_size, overlap = get_chunk_settings(page_count)
    return _split_text(text, chunk_size, overlap)


def _split_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    """
    Internal splitting logic.

    Strategy:
      1. First tries to split on paragraph boundaries (double newlines)
      2. For paragraphs longer than chunk_size, falls back to character splitting
      3. Each new chunk starts with the last `overlap` characters of the previous
         chunk to preserve context across boundaries
    """
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks = []
    current_chunk = ""

    for para in paragraphs:
        # Paragraph is too long on its own — hard split it
        if len(para) > chunk_size:
            if current_chunk:
                chunks.append(current_chunk.strip())
                current_chunk = ""
            for i in range(0, len(para), chunk_size - overlap):
                chunks.append(para[i : i + chunk_size].strip())

        # Paragraph fits in the current chunk
        elif len(current_chunk) + len(para) + 2 <= chunk_size:
            current_chunk += ("\n\n" if current_chunk else "") + para

        # Paragraph doesn't fit — flush current chunk and start a new one
        else:
            if current_chunk:
                chunks.append(current_chunk.strip())
            overlap_text = current_chunk[-overlap:] if len(current_chunk) > overlap else current_chunk
            candidate = overlap_text + "\n\n" + para
            if len(candidate) <= chunk_size:
                current_chunk = candidate
            else:
                # para itself fits within chunk_size (the outer if above
                # only hard-splits paragraphs LARGER than chunk_size), but
                # prepending the overlap-carry would push this one over
                # the cap -- drop the overlap just for this boundary
                # rather than silently violate the configured chunk size.
                current_chunk = para

    # Don't forget the last chunk
    if current_chunk.strip():
        chunks.append(current_chunk.strip())

    return [c for c in chunks if c]
