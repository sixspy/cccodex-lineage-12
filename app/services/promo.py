"""跨仓晋升放行：源仓血缘包 → 目标仓边表。

两条互斥接收方式（不可同时打开，目标仓边表内容不同）：
- 快照表：用 promo_snapshot_edges 封存整包每条边的指纹与条数，半包直接拒写；
- 文件凭证：把整包物化成一个文件，仅在旁路凭证 promo_receipts 记一行。

工程约束：
- 放行对凭证单次写入（receipts.UNIQUE(package_id)），失败统一中文报错并回滚；
- 放行经 BEGIN IMMEDIATE 排他写锁 + 单行锁表，两侧值班抢点不会双写踩凭证；
- 全量抓取入库前按整包指纹与条数核对目标仓边表，对不上直接拦住。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from app.config import Config
from app.db import get_db, immediate_tx
from app.services.errors import ServiceError

RECEIVE_MODES = ("快照表", "文件凭证")
PROMO_DIR_NAME = "promo_payloads"


# ---------- 指纹 ----------

def edge_fingerprint(edge_code: str, upstream_code: str,
                     downstream_code: str, transform_kind: str) -> str:
    raw = f"{edge_code}|{upstream_code}|{downstream_code}|{transform_kind}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def payload_fingerprint(edges: list[dict]) -> str:
    canonical = json.dumps(edges, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------- 台参数（接收方式二选一） ----------

def get_receive_mode(db_path: str | None = None) -> str:
    with get_db(db_path) as conn:
        return _mode_from_conn(conn)


def _mode_from_conn(conn) -> str:
    row = conn.execute(
        "SELECT value FROM lab_settings WHERE key='promo_receive_mode'"
    ).fetchone()
    mode = row["value"] if row else RECEIVE_MODES[0]
    if mode not in RECEIVE_MODES:
        raise ServiceError(f"接收方式非法：{mode}，只许快照表/文件凭证")
    return mode


def set_receive_mode(mode: str, db_path: str | None = None) -> dict:
    mode = (mode or "").strip()
    if mode not in RECEIVE_MODES:
        raise ServiceError("接收方式只许：快照表 或 文件凭证")
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM lab_settings WHERE key='promo_receive_mode'"
        ).fetchone()
        if not row:
            raise ServiceError("晋升接收方式参数不存在", 404)
        conn.execute(
            "UPDATE lab_settings SET value=? WHERE key='promo_receive_mode'",
            (mode,),
        )
        return {
            "key": "promo_receive_mode",
            "value": mode,
            "label": "晋升接收方式（快照表/文件凭证，二选一）",
        }


# ---------- 打包（源仓） ----------

def _published_edges(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT e.edge_code, u.node_code AS upstream_code, "
        "d.node_code AS downstream_code, e.transform_kind, e.status "
        "FROM lineage_edges e "
        "JOIN lineage_nodes u ON u.id=e.upstream_id "
        "JOIN lineage_nodes d ON d.id=e.downstream_id "
        "WHERE e.status='已发布' ORDER BY e.edge_code"
    ).fetchall()
    return [dict(r) for r in rows]


def create_package(source_repo: str = "源仓", target_repo: str = "目标仓",
                   note: str = "", db_path: str | None = None) -> dict:
    """把源仓当前全部已发布边封成一个晋升包，封存条数与整包指纹。"""
    source_repo = (source_repo or "源仓").strip()
    target_repo = (target_repo or "目标仓").strip()
    with get_db(db_path) as conn:
        raw_edges = _published_edges(conn)
        edges = [
            {
                "edge_code": e["edge_code"],
                "upstream_code": e["upstream_code"],
                "downstream_code": e["downstream_code"],
                "transform_kind": e["transform_kind"],
            }
            for e in raw_edges
        ]
        if not edges:
            raise ServiceError("源仓没有已发布边，无法打包")
        fp = payload_fingerprint(edges)
        seq = conn.execute("SELECT COUNT(*) AS c FROM promo_packages").fetchone()["c"] + 1
        code = f"PKG-{seq:04d}"
        cur = conn.execute(
            "INSERT INTO promo_packages(package_code, source_repo, target_repo, "
            "edge_count, edge_fingerprint, payload, status, note) "
            "VALUES (?,?,?,?,?,?,'已打包',?)",
            (code, source_repo, target_repo, len(edges), fp,
             json.dumps(edges, ensure_ascii=False), note or ""),
        )
        return dict(
            conn.execute(
                "SELECT * FROM promo_packages WHERE id=?", (cur.lastrowid,)
            ).fetchone()
        )


def list_packages(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT p.*, "
            "(SELECT COUNT(*) FROM promo_snapshot_edges s WHERE s.package_id=p.id) "
            "AS snapshot_count, "
            "(SELECT COUNT(*) FROM promo_target_edges t WHERE t.package_id=p.id) "
            "AS target_count "
            "FROM promo_packages p ORDER BY p.id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def _load_package(conn, package_id: int) -> sqlite3.Row:
    pkg = conn.execute(
        "SELECT * FROM promo_packages WHERE id=?", (package_id,)
    ).fetchone()
    if not pkg:
        raise ServiceError("晋升包不存在", 404)
    return pkg


def package_detail(package_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        pkg = _load_package(conn, package_id)
        data = dict(pkg)
        data["payload_edges"] = json.loads(pkg["payload"])
        data["snapshot_edges"] = [
            dict(r) for r in conn.execute(
                "SELECT * FROM promo_snapshot_edges WHERE package_id=? ORDER BY seq",
                (package_id,),
            ).fetchall()
        ]
        data["target_edges"] = [
            dict(r) for r in conn.execute(
                "SELECT * FROM promo_target_edges WHERE package_id=? ORDER BY id",
                (package_id,),
            ).fetchall()
        ]
        receipt = conn.execute(
            "SELECT * FROM promo_receipts WHERE package_id=?", (package_id,)
        ).fetchone()
        data["receipt"] = dict(receipt) if receipt else None
        lock = conn.execute(
            "SELECT * FROM promo_release_locks WHERE package_id=?", (package_id,)
        ).fetchone()
        data["lock"] = dict(lock) if lock else None
        return data


# ---------- 放行（单次写入 + 排他锁 + 二选一接收） ----------

def _payload_file_path(db_path: str | None, receipt_code: str) -> str:
    root = Path(db_path or Config.database()).resolve().parent
    out_dir = root / PROMO_DIR_NAME
    out_dir.mkdir(parents=True, exist_ok=True)
    return str(out_dir / f"{receipt_code}.json")


def _verify_full_package(conn, pkg: sqlite3.Row, edges: list[dict]) -> None:
    """整包校验：条数与指纹都对得上才放行，半包/被篡改一律拦住。"""
    if not edges:
        raise ServiceError("晋升包为空包，已拒写", 409)
    if len(edges) != int(pkg["edge_count"]):
        raise ServiceError(
            f"边条数对不上：封存 {pkg['edge_count']} 条，实际 {len(edges)} 条，半包直接拒写",
            409,
        )
    if payload_fingerprint(edges) != pkg["edge_fingerprint"]:
        raise ServiceError("整包边指纹对不上，疑似残缺或被篡改，直接拒写", 409)
    for e in edges:
        for col in ("edge_code", "upstream_code", "downstream_code", "transform_kind"):
            if not e.get(col):
                raise ServiceError(f"边 {e.get('edge_code', '?')} 字段残缺，半包直接拒写", 409)
        for code in (e["upstream_code"], e["downstream_code"]):
            node = conn.execute(
                "SELECT id FROM lineage_nodes WHERE node_code=?", (code,)
            ).fetchone()
            if not node:
                raise ServiceError(f"目标仓缺少节点 {code}，整包拒写", 409)


def release_package(package_id: int, holder: str = "值班",
                    db_path: str | None = None) -> dict:
    """服务端对放行做单次写入。

    BEGIN IMMEDIATE 取排他写锁并占单行锁，两侧值班抢点只有一个能进入；
    凭证表 package_id 唯一，任何重复/失败均回滚并以中文报错。
    """
    holder = (holder or "值班").strip() or "值班"
    materialized_file = None
    try:
        with immediate_tx(db_path) as conn:
            pkg = _load_package(conn, package_id)
            if pkg["status"] not in ("已打包", "已拒写"):
                raise ServiceError("该晋升包已放行，不可重复放行", 409)
            try:
                conn.execute(
                    "INSERT INTO promo_release_locks(package_id, holder) VALUES (?,?)",
                    (package_id, holder),
                )
            except sqlite3.IntegrityError as exc:
                raise ServiceError("已有值班持排他锁放行中，请勿抢点", 409) from exc

            edges = json.loads(pkg["payload"])
            _verify_full_package(conn, pkg, edges)
            mode = _mode_from_conn(conn)

            seq = conn.execute("SELECT COUNT(*) AS c FROM promo_receipts").fetchone()["c"] + 1
            receipt_code = f"RC-{seq:04d}"

            existing = conn.execute(
                "SELECT id FROM promo_receipts WHERE package_id=?", (package_id,)
            ).fetchone()
            if existing:
                raise ServiceError("旁路凭证已存在，单次写入拒绝重复", 409)

            if mode == "快照表":
                # 方式 A：快照表封存整包每条边指纹与条数，再整包写入目标仓边表
                for i, e in enumerate(edges, start=1):
                    fp = edge_fingerprint(
                        e["edge_code"], e["upstream_code"],
                        e["downstream_code"], e["transform_kind"],
                    )
                    conn.execute(
                        "INSERT INTO promo_snapshot_edges(package_id, edge_code, "
                        "edge_fingerprint, seq) VALUES (?,?,?,?)",
                        (package_id, e["edge_code"], fp, i),
                    )
                conn.execute(
                    "INSERT INTO promo_receipts(package_id, receipt_code, receive_mode, "
                    "source_repo, target_repo, edge_count, edge_fingerprint, "
                    "payload_file, status, holder) "
                    "VALUES (?,?,?,?,?,?,?,'','凭证已记一行',?)",
                    (package_id, receipt_code, mode, pkg["source_repo"],
                     pkg["target_repo"], len(edges), pkg["edge_fingerprint"], holder),
                )
                _write_target_edges(conn, pkg, edges, mode)
            else:
                # 方式 B：整包物化成文件，旁路凭证只记一行（不写快照表）
                path = _payload_file_path(db_path, receipt_code)
                materialized_file = path
                Path(path).write_text(
                    json.dumps(
                        {
                            "package_code": pkg["package_code"],
                            "source_repo": pkg["source_repo"],
                            "target_repo": pkg["target_repo"],
                            "edge_count": len(edges),
                            "edge_fingerprint": pkg["edge_fingerprint"],
                            "edges": edges,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                conn.execute(
                    "INSERT INTO promo_receipts(package_id, receipt_code, receive_mode, "
                    "source_repo, target_repo, edge_count, edge_fingerprint, "
                    "payload_file, status, holder) "
                    "VALUES (?,?,?,?,?,?,?,?,'凭证已记一行',?)",
                    (package_id, receipt_code, mode, pkg["source_repo"],
                     pkg["target_repo"], len(edges), pkg["edge_fingerprint"], path, holder),
                )
                _write_target_edges(conn, pkg, edges, mode)

            conn.execute(
                "UPDATE promo_packages SET status='已接收', promoted_at=datetime('now') "
                "WHERE id=?",
                (package_id,),
            )
            # 放行事务结束即释放排他锁；持锁人已固化在凭证 holder 字段
            conn.execute(
                "DELETE FROM promo_release_locks WHERE package_id=?", (package_id,)
            )
            receipt = conn.execute(
                "SELECT * FROM promo_receipts WHERE package_id=?", (package_id,)
            ).fetchone()
            return {"receipt": dict(receipt), "receive_mode": mode}
    except sqlite3.OperationalError as exc:
        raise ServiceError("放行排他锁被占用，已有值班在写入，请稍后重试", 409) from exc
    except Exception:
        # 事务已回滚；文件方式若已物化，须同步删除孤儿文件，避免旁路凭证与文件不一致
        if materialized_file:
            try:
                Path(materialized_file).unlink(missing_ok=True)
            except OSError:
                pass
        raise


def _write_target_edges(conn, pkg: sqlite3.Row, edges: list[dict], mode: str) -> None:
    """整包写入目标仓边表；半包/重复边一律回滚，不留残缺边。"""
    for e in edges:
        fp = edge_fingerprint(
            e["edge_code"], e["upstream_code"],
            e["downstream_code"], e["transform_kind"],
        )
        try:
            conn.execute(
                "INSERT INTO promo_target_edges(package_id, edge_code, upstream_code, "
                "downstream_code, transform_kind, edge_fingerprint, source_repo, receive_mode) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (pkg["id"], e["edge_code"], e["upstream_code"], e["downstream_code"],
                 e["transform_kind"], fp, pkg["source_repo"], mode),
            )
        except sqlite3.IntegrityError as exc:
            raise ServiceError(
                f"目标仓边表已存在边 {e['edge_code']}，整包拒写", 409
            ) from exc


def list_receipts(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT r.*, p.package_code FROM promo_receipts r "
            "JOIN promo_packages p ON p.id=r.package_id ORDER BY r.id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def list_target_edges(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM promo_target_edges ORDER BY source_repo, id"
        ).fetchall()
        return [dict(r) for r in rows]


def same_source_counts(db_path: str | None = None) -> list[dict]:
    """边表同源条数：按来源仓 + 接收方式聚合。"""
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT source_repo, receive_mode, COUNT(*) AS edge_count "
            "FROM promo_target_edges GROUP BY source_repo, receive_mode ORDER BY source_repo"
        ).fetchall()
        return [dict(r) for r in rows]


# ---------- 全量抓取入库（指纹/条数门禁） ----------

def run_full_ingest(db_path: str | None = None) -> dict:
    """全量抓取入库前，用最近一次接收凭证核对目标仓边表的整包指纹与条数。"""
    # 只读阶段：取凭证与目标边表，计算整包指纹/条数
    with get_db(db_path) as conn:
        receipt = conn.execute(
            "SELECT * FROM promo_receipts WHERE status='凭证已记一行' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not receipt:
            raise ServiceError("尚无接收凭证，禁止全量抓取入库", 409)
        target_rows = conn.execute(
            "SELECT edge_code, upstream_code, downstream_code, transform_kind "
            "FROM promo_target_edges WHERE package_id=? ORDER BY edge_code",
            (receipt["package_id"],),
        ).fetchall()
        actual_edges = [dict(r) for r in target_rows]
        actual_count = len(actual_edges)
        actual_fp = payload_fingerprint(actual_edges)
        expected_count = int(receipt["edge_count"])
        expected_fp = receipt["edge_fingerprint"]

    matched = actual_count == expected_count and actual_fp == expected_fp
    if matched:
        status, reason = "已通", "指纹与条数核对一致，允许全量抓取入库"
    else:
        status = "已拦"
        reason = (
            "指纹/条数对不上已拦住全量抓取入库："
            f"凭证 {expected_count} 条/{expected_fp[:12]}…，"
            f"目标边表 {actual_count} 条/{actual_fp[:12]}…"
        )

    # 独立事务落入库门禁记录并提交，保证"已拦"不会被随后的报错回滚吞掉
    with get_db(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO promo_ingest_runs(status, reason, expected_count, actual_count, "
            "expected_fingerprint, actual_fingerprint) VALUES (?,?,?,?,?,?)",
            (status, reason, expected_count, actual_count, expected_fp, actual_fp),
        )
        run = dict(
            conn.execute(
                "SELECT * FROM promo_ingest_runs WHERE id=?", (cur.lastrowid,)
            ).fetchone()
        )

    if not matched:
        raise ServiceError(reason, 409)
    return run


def list_ingest_runs(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM promo_ingest_runs ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def list_locks(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT l.*, p.package_code FROM promo_release_locks l "
            "JOIN promo_packages p ON p.id=l.package_id ORDER BY l.package_id"
        ).fetchall()
        return [dict(r) for r in rows]


def promo_state(db_path: str | None = None) -> dict:
    """晋升总览：覆盖进度显示晋升来源，边表显示同源条数。"""
    with get_db(db_path) as conn:
        mode = get_receive_mode(db_path)
        total_pkg = conn.execute(
            "SELECT COUNT(*) AS c FROM promo_packages"
        ).fetchone()["c"]
        received_pkg = conn.execute(
            "SELECT COUNT(*) AS c FROM promo_packages WHERE status='已接收'"
        ).fetchone()["c"]
        last_receipt = conn.execute(
            "SELECT r.*, p.package_code, p.promoted_at, p.source_repo, p.target_repo "
            "FROM promo_receipts r JOIN promo_packages p ON p.id=r.package_id "
            "ORDER BY r.id DESC LIMIT 1"
        ).fetchone()
        last_ingest = conn.execute(
            "SELECT * FROM promo_ingest_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        target_total = conn.execute(
            "SELECT COUNT(*) AS c FROM promo_target_edges"
        ).fetchone()["c"]
        return {
            "receive_mode": mode,
            "package_total": int(total_pkg),
            "package_received": int(received_pkg),
            "target_edge_total": int(target_total),
            "same_source_counts": same_source_counts(db_path),
            "last_receipt": dict(last_receipt) if last_receipt else None,
            "last_ingest": dict(last_ingest) if last_ingest else None,
            "locks": list_locks(db_path),
        }
