import sqlite3
import os

DB_PATH = "cache.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
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
    conn.commit()
    conn.close()

class CacheService:
    @staticmethod
    def get_cached_verdict(claim_text: str):
        import hashlib
        claim_hash = hashlib.sha256(claim_text.encode()).hexdigest()
        
        conn = sqlite3.connect(DB_PATH)
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
        conn = sqlite3.connect(DB_PATH)
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
        conn = sqlite3.connect(DB_PATH)
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
