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
            metadata_json TEXT,
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
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS factcheck_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_hash TEXT NOT NULL UNIQUE,
            post_text TEXT,
            extracted_claim TEXT,
            claim_status TEXT,
            system_verdict TEXT,
            verdict_markdown TEXT,
            selected_evidence_url TEXT,
            selected_evidence_title TEXT,
            selected_evidence_snippet TEXT,
            all_evidence_json TEXT,
            notes TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute("PRAGMA table_info(claim_cache)")
    existing_claim_cache_columns = {row[1] for row in cursor.fetchall()}
    if "metadata_json" not in existing_claim_cache_columns:
        cursor.execute("ALTER TABLE claim_cache ADD COLUMN metadata_json TEXT")
    conn.commit()
    conn.close()


class CacheService:
    @staticmethod
    def get_cached_verdict(claim_text: str):
        import hashlib
        claim_hash = hashlib.sha256(claim_text.encode()).hexdigest()

        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT verdict_markdown, evidence_json, metadata_json FROM claim_cache WHERE claim_hash = ?",
            (claim_hash,),
        )
        result = cursor.fetchone()
        conn.close()
        if not result:
            return None
        verdict = result[0]
        evidence_json = result[1]
        metadata_json = result[2] if len(result) > 2 else None
        try:
            import json
            evidence = json.loads(evidence_json) if evidence_json else []
        except Exception:
            evidence = []
        try:
            import json
            metadata = json.loads(metadata_json) if metadata_json else {}
        except Exception:
            metadata = {}
        return {"verdict_markdown": verdict, "evidence_links": evidence, "metadata": metadata}

    @staticmethod
    def save_to_cache(claim_text: str, verdict_markdown: str, evidence_links=None, metadata=None):
        import hashlib
        claim_hash = hashlib.sha256(claim_text.encode()).hexdigest()
        import json
        evidence_json = json.dumps(evidence_links or [])
        metadata_json = json.dumps(metadata or {})
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO claim_cache
            (claim_hash, claim_text, verdict_markdown, evidence_json, metadata_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (claim_hash, claim_text, verdict_markdown, evidence_json, metadata_json)
        )
        conn.commit()
        conn.close()

    @staticmethod
    def list_cache():
        import json
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT claim_text, verdict_markdown, evidence_json, metadata_json, timestamp
            FROM claim_cache
            ORDER BY timestamp DESC
            """
        )
        rows = cursor.fetchall()
        conn.close()
        out = []
        for claim_text, verdict, evidence_json, metadata_json, ts in rows:
            try:
                evidence = json.loads(evidence_json) if evidence_json else []
            except Exception:
                evidence = []
            try:
                metadata = json.loads(metadata_json) if metadata_json else {}
            except Exception:
                metadata = {}
            out.append({
                "claim_text": claim_text,
                "verdict_markdown": verdict,
                "evidence_links": evidence,
                "metadata": metadata,
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


class ReviewService:
    @staticmethod
    def save_review(
        post_text: str,
        extracted_claim: str = "",
        claim_status: str = "",
        system_verdict: str = "",
        verdict_markdown: str = "",
        selected_evidence_url: str = "",
        selected_evidence_title: str = "",
        selected_evidence_snippet: str = "",
        all_evidence=None,
        notes: str = "",
    ):
        import hashlib
        import json

        post_hash = hashlib.sha256((post_text or "").encode()).hexdigest()
        all_evidence_json = json.dumps(all_evidence or [])
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO factcheck_reviews (
                post_hash, post_text, extracted_claim, claim_status, system_verdict,
                verdict_markdown, selected_evidence_url, selected_evidence_title,
                selected_evidence_snippet, all_evidence_json, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(post_hash) DO UPDATE SET
                post_text=excluded.post_text,
                extracted_claim=excluded.extracted_claim,
                claim_status=excluded.claim_status,
                system_verdict=excluded.system_verdict,
                verdict_markdown=excluded.verdict_markdown,
                selected_evidence_url=excluded.selected_evidence_url,
                selected_evidence_title=excluded.selected_evidence_title,
                selected_evidence_snippet=excluded.selected_evidence_snippet,
                all_evidence_json=excluded.all_evidence_json,
                notes=excluded.notes,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                post_hash,
                post_text,
                extracted_claim,
                claim_status,
                system_verdict,
                verdict_markdown,
                selected_evidence_url,
                selected_evidence_title,
                selected_evidence_snippet,
                all_evidence_json,
                notes,
            ),
        )
        conn.commit()
        conn.close()
        return post_hash

    @staticmethod
    def list_reviews(query: str = ""):
        import json

        conn = _get_connection()
        cursor = conn.cursor()

        normalized_query = (query or "").strip()
        if normalized_query:
            like = f"%{normalized_query}%"
            cursor.execute(
                """
                SELECT id, post_hash, post_text, extracted_claim, claim_status, system_verdict,
                       verdict_markdown, selected_evidence_url, selected_evidence_title,
                       selected_evidence_snippet, all_evidence_json, notes, created_at, updated_at
                FROM factcheck_reviews
                WHERE post_text LIKE ?
                   OR extracted_claim LIKE ?
                   OR system_verdict LIKE ?
                   OR selected_evidence_url LIKE ?
                   OR selected_evidence_title LIKE ?
                   OR notes LIKE ?
                ORDER BY updated_at DESC, created_at DESC, id DESC
                """,
                (like, like, like, like, like, like),
            )
        else:
            cursor.execute(
                """
                SELECT id, post_hash, post_text, extracted_claim, claim_status, system_verdict,
                       verdict_markdown, selected_evidence_url, selected_evidence_title,
                       selected_evidence_snippet, all_evidence_json, notes, created_at, updated_at
                FROM factcheck_reviews
                ORDER BY updated_at DESC, created_at DESC, id DESC
                """
            )

        rows = cursor.fetchall()
        conn.close()
        out = []
        for row in rows:
            try:
                all_evidence = json.loads(row[10]) if row[10] else []
            except Exception:
                all_evidence = []
            out.append({
                "id": row[0],
                "post_hash": row[1],
                "post_text": row[2] or "",
                "extracted_claim": row[3] or "",
                "claim_status": row[4] or "",
                "system_verdict": row[5] or "",
                "verdict_markdown": row[6] or "",
                "selected_evidence_url": row[7] or "",
                "selected_evidence_title": row[8] or "",
                "selected_evidence_snippet": row[9] or "",
                "all_evidence": all_evidence,
                "notes": row[11] or "",
                "created_at": row[12] or "",
                "updated_at": row[13] or "",
            })
        return out
