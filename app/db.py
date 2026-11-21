import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS lineage_nodes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node_code TEXT NOT NULL UNIQUE,
  table_name TEXT NOT NULL,
  layer TEXT NOT NULL CHECK (layer IN ('ODS','DWD','DWS','ADS')),
  owner_team TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN ('登记','在用','停用')),
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lineage_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  edge_code TEXT NOT NULL UNIQUE,
  upstream_id INTEGER NOT NULL REFERENCES lineage_nodes(id),
  downstream_id INTEGER NOT NULL REFERENCES lineage_nodes(id),
  transform_kind TEXT NOT NULL DEFAULT '直抽',
  status TEXT NOT NULL CHECK (status IN ('草稿','已发布','已作废')),
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(upstream_id, downstream_id)
);

CREATE TABLE IF NOT EXISTS impact_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_code TEXT NOT NULL UNIQUE,
  root_node_id INTEGER NOT NULL REFERENCES lineage_nodes(id),
  status TEXT NOT NULL CHECK (status IN ('待执行','执行中','成功','失败')),
  affected_count INTEGER NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT '',
  started_at TEXT,
  finished_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS impact_hits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES impact_jobs(id),
  node_id INTEGER NOT NULL REFERENCES lineage_nodes(id),
  hop INTEGER NOT NULL DEFAULT 0,
  UNIQUE(job_id, node_id)
);

CREATE TABLE IF NOT EXISTS notify_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL REFERENCES impact_jobs(id),
  node_id INTEGER NOT NULL REFERENCES lineage_nodes(id),
  channel TEXT NOT NULL DEFAULT '站内',
  status TEXT NOT NULL CHECK (status IN ('待发','已发','失败')),
  body TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS coverage_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  summary TEXT NOT NULL,
  node_count INTEGER NOT NULL,
  edge_count INTEGER NOT NULL,
  orphan_count INTEGER NOT NULL,
  uncovered_edges INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS lab_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  label TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS promo_packages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  package_code TEXT NOT NULL UNIQUE,
  source_repo TEXT NOT NULL,
  target_repo TEXT NOT NULL,
  edge_count INTEGER NOT NULL,
  edge_fingerprint TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('已打包','已放行','已接收','已拒写')),
  note TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL DEFAULT 'admin',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  promoted_at TEXT
);

CREATE TABLE IF NOT EXISTS promo_receipts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  package_id INTEGER NOT NULL UNIQUE REFERENCES promo_packages(id),
  receipt_code TEXT NOT NULL UNIQUE,
  receive_mode TEXT NOT NULL CHECK (receive_mode IN ('快照表','文件凭证')),
  source_repo TEXT NOT NULL,
  target_repo TEXT NOT NULL,
  edge_count INTEGER NOT NULL,
  edge_fingerprint TEXT NOT NULL,
  payload_file TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN ('凭证已记一行','已拦')),
  holder TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS promo_snapshot_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  package_id INTEGER NOT NULL REFERENCES promo_packages(id),
  edge_code TEXT NOT NULL,
  edge_fingerprint TEXT NOT NULL,
  seq INTEGER NOT NULL,
  UNIQUE(package_id, edge_code)
);

CREATE TABLE IF NOT EXISTS promo_target_edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  package_id INTEGER NOT NULL REFERENCES promo_packages(id),
  edge_code TEXT NOT NULL UNIQUE,
  upstream_code TEXT NOT NULL,
  downstream_code TEXT NOT NULL,
  transform_kind TEXT NOT NULL,
  edge_fingerprint TEXT NOT NULL,
  source_repo TEXT NOT NULL,
  receive_mode TEXT NOT NULL CHECK (receive_mode IN ('快照表','文件凭证')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS promo_ingest_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  status TEXT NOT NULL CHECK (status IN ('已通','已拦')),
  reason TEXT NOT NULL DEFAULT '',
  expected_count INTEGER NOT NULL,
  actual_count INTEGER NOT NULL,
  expected_fingerprint TEXT NOT NULL,
  actual_fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS promo_release_locks (
  package_id INTEGER PRIMARY KEY REFERENCES promo_packages(id),
  holder TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL：读写可并发，写仍为单写者，BEGIN IMMEDIATE 的排他语义不变
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_schema(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def immediate_tx(db_path: str | None = None):
    """立即写事务：BEGIN IMMEDIATE 抢先获取写锁，提交或回滚后释放。

    用于晋升放行等必须排他的单次写入；抢不到写锁会抛 sqlite3.OperationalError。
    """
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
