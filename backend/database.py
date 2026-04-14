import sqlite3
import os

DB_PATH = "cache.db"


def _get_connection():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = _get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS claim_cache (
            claim_hash TEXT PRIMARY KEY,
            claim_text TEXT,
            verdict_markdown TEXT,
            evidence_json TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS curated_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL UNIQUE,
            title TEXT,
            source TEXT,
            claim_summary TEXT,
            verdict TEXT,
            notes TEXT,
            tags TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()


class CacheService:
    @staticmethod
    def get_cached_verdict(claim_text: str):
        import hashlib
        claim_hash = hashlib.sha256(claim_text.encode()).hexdigest()

        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT verdict_markdown, evidence_json FROM claim_cache WHERE claim_hash = ?", (claim_hash,))
        result = cursor.fetchone()
        conn.close()
        if not result:
            return None
        verdict = result[0]
        evidence_json = result[1]
        try:
            import json
            evidence = json.loads(evidence_json) if evidence_json else []
        except Exception:
            evidence = []
        return {"verdict_markdown": verdict, "evidence_links": evidence}

    @staticmethod
    def save_to_cache(claim_text: str, verdict_markdown: str, evidence_links=None):
        import hashlib
        claim_hash = hashlib.sha256(claim_text.encode()).hexdigest()
        import json
        evidence_json = json.dumps(evidence_links or [])
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO claim_cache (claim_hash, claim_text, verdict_markdown, evidence_json) VALUES (?, ?, ?, ?)",
            (claim_hash, claim_text, verdict_markdown, evidence_json)
        )
        conn.commit()
        conn.close()

    @staticmethod
    def list_cache():
        import json
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT claim_text, verdict_markdown, evidence_json, timestamp FROM claim_cache ORDER BY timestamp DESC")
        rows = cursor.fetchall()
        conn.close()
        out = []
        for claim_text, verdict, evidence_json, ts in rows:
            try:
                evidence = json.loads(evidence_json) if evidence_json else []
            except Exception:
                evidence = []
            out.append({
                "claim_text": claim_text,
                "verdict_markdown": verdict,
                "evidence_links": evidence,
                "timestamp": ts
            })
        return out


class CuratedEvidenceService:
    @staticmethod
    def add_entry(url: str, title: str = "", source: str = "", claim_summary: str = "", verdict: str = "", notes: str = "", tags=None):
        import json
        conn = _get_connection()
        cursor = conn.cursor()
        tags_json = json.dumps(tags or [])
        cursor.execute(
            """
            INSERT INTO curated_evidence (url, title, source, claim_summary, verdict, notes, tags)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                title=excluded.title,
                source=excluded.source,
                claim_summary=excluded.claim_summary,
                verdict=excluded.verdict,
                notes=excluded.notes,
                tags=excluded.tags
            """,
            (url, title, source, claim_summary, verdict, notes, tags_json),
        )
        conn.commit()
        conn.close()

    @staticmethod
    def list_entries():
        import json
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, url, title, source, claim_summary, verdict, notes, tags, created_at
            FROM curated_evidence
            ORDER BY created_at DESC, id DESC
            """
        )
        rows = cursor.fetchall()
        conn.close()
        out = []
        for row in rows:
            tags = []
            try:
                tags = json.loads(row[7]) if row[7] else []
            except Exception:
                tags = []
            out.append({
                "id": row[0],
                "url": row[1],
                "title": row[2] or "",
                "source": row[3] or "",
                "claim_summary": row[4] or "",
                "verdict": row[5] or "",
                "notes": row[6] or "",
                "tags": tags,
                "created_at": row[8],
            })
        return out
