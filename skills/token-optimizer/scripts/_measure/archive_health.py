"""Archive replacement health for the dashboard and doctor (see scripts/archive_verify.py)."""


def replacement_check(facade):
    """Health of archive replacements, read back from transcripts. Never raises."""
    try:
        import archive_verify
        conn = facade.sqlite3.connect(facade.TRENDS_DB.resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0)
        try:
            return archive_verify.health(conn)
        finally:
            conn.close()
    except Exception:
        return {"status": "unknown", "verified": 0, "full": 0, "full_streak": 0}
