"""Immutable identity for a qualification window; old failed evidence is retained."""
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path


def release_hash(root=None):
    root = Path(root or Path(__file__).parent)
    h = hashlib.sha256()
    for p in sorted(root.glob('*.py')):
        h.update(p.name.encode()); h.update(p.read_bytes())
    return h.hexdigest()


def register_run(db, run_id, config, release_sha256, calendar_sha256, provider):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',run_id):
        raise ValueError('invalid acceptance run ID')
    if not all(re.fullmatch(r'[0-9a-f]{64}',h) for h in (release_sha256, calendar_sha256)) or not provider:
        raise ValueError('release, calendar and provider identity required')
    db.execute('''CREATE TABLE IF NOT EXISTS acceptance_runs(
        run_id TEXT PRIMARY KEY,source_sha256 TEXT NOT NULL,config_hash TEXT NOT NULL,
        release_sha256 TEXT NOT NULL,calendar_sha256 TEXT NOT NULL,provider TEXT NOT NULL,
        started_at TEXT NOT NULL)''')
    identity=(config.source_sha256, config.hash, release_sha256, calendar_sha256, provider)
    old=db.execute('SELECT source_sha256,config_hash,release_sha256,calendar_sha256,provider FROM acceptance_runs WHERE run_id=?',(run_id,)).fetchone()
    if old and tuple(old)!=identity:
        raise ValueError('acceptance run identity changed; use a new run')
    db.execute('INSERT OR IGNORE INTO acceptance_runs VALUES(?,?,?,?,?,?,?)',
               (run_id,*identity,datetime.now(timezone.utc).isoformat()))
