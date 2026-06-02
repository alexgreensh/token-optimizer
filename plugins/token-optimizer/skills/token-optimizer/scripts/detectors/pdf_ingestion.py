"""PDF/Image ingestion detector for Token Optimizer.

Detects expensive binary file reads (PDFs, images, Office docs) and warns
about token cost. Used inline by read_cache.py hook.
"""

# Subset of read_cache.py:BINARY_EXTENSIONS that triggers token-cost warnings.
EXPENSIVE_BINARY = frozenset({
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
    ".docx", ".xlsx", ".pptx",
})

# Tokens per MB: documents are denser, media is lighter
_DOCUMENT_TOKENS_PER_MB = 2500
_MEDIA_TOKENS_PER_MB = 1500
_DOCUMENT_EXTS = frozenset({".pdf", ".docx", ".xlsx", ".pptx"})

_PDF_SUGGESTION = "Extract text first (`pdftotext`), read specific pages (pages: param), or summarize externally."
_DEFAULT_SUGGESTION = "Consider extracting text content first, or describe what you need from this file."


def detect_pdf_ingestion_inline(file_path, file_size_bytes, ext):
    """Inline check for read_cache.py hook.

    Args:
        file_path: Path to the file being read
        file_size_bytes: File size in bytes
        ext: File extension (lowercase, with dot)

    Returns:
        dict with name/confidence/evidence/savings_tokens/suggestion, or None
    """
    if ext not in EXPENSIVE_BINARY:
        return None

    # Skip tiny files (< 1KB) - not worth warning
    if file_size_bytes < 1024:
        return None

    size_mb = file_size_bytes / (1024 * 1024)
    tpm = _DOCUMENT_TOKENS_PER_MB if ext in _DOCUMENT_EXTS else _MEDIA_TOKENS_PER_MB
    est_tokens = int(size_mb * tpm)
    suggestion = _PDF_SUGGESTION if ext == ".pdf" else _DEFAULT_SUGGESTION

    return {
        "name": "pdf_ingestion",
        "confidence": 0.9,
        "evidence": f"{ext} file, {size_mb:.1f}MB, ~{est_tokens:,} estimated tokens",
        "savings_tokens": est_tokens,
        "suggestion": suggestion,
        "occurrence_count": 1,
    }
