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
        cursor.execute("SELECT verdict_markdown FROM claim_cache WHERE claim_hash = ?", (claim_hash,))
        result = cursor.fetchone()
        conn.close()
        
        return result[0] if result else None

    @staticmethod
    def save_to_cache(claim_text: str, verdict_markdown: str):
        import hashlib
        claim_hash = hashlib.sha256(claim_text.encode()).hexdigest()
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO claim_cache (claim_hash, claim_text, verdict_markdown) VALUES (?, ?, ?)",
            (claim_hash, claim_text, verdict_markdown)
        )
        conn.commit()
        conn.close()
