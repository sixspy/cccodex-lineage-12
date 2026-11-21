"""跨仓晋升放行：快照表/文件凭证二选一、半包拒写、指纹门禁、排他锁。"""
from __future__ import annotations

import json
import sqlite3

import pytest

from app.db import connect
from app.services import edges, promo
from app.services.errors import ServiceError


def test_snapshot_mode_seals_fingerprints_and_writes_target(db_path):
    pkg = promo.create_package(db_path=db_path)
    assert promo.get_receive_mode(db_path) == "快照表"
    res = promo.release_package(int(pkg["id"]), holder="值班甲", db_path=db_path)
    assert res["receive_mode"] == "快照表"

    detail = promo.package_detail(int(pkg["id"]), db_path=db_path)
    # 快照表封存整包条数与每条边指纹
    assert len(detail["snapshot_edges"]) == int(pkg["edge_count"])
    assert all(s["edge_fingerprint"] for s in detail["snapshot_edges"])
    # 目标仓边表整包吃进，来源与接收方式打标
    targets = promo.list_target_edges(db_path=db_path)
    assert len(targets) == int(pkg["edge_count"])
    assert all(t["receive_mode"] == "快照表" for t in targets)
    assert all(t["source_repo"] == "源仓" for t in targets)
    # 凭证单次写入且不含物化文件
    assert detail["receipt"]["status"] == "凭证已记一行"
    assert detail["receipt"]["payload_file"] == ""
    # 放行后锁已释放
    assert promo.list_locks(db_path=db_path) == []

    run = promo.run_full_ingest(db_path=db_path)
    assert run["status"] == "已通"


def test_half_package_is_rejected_and_target_stays_clean(db_path):
    pkg = promo.create_package(db_path=db_path)
    edges_data = json.loads(pkg["payload"])
    # 源仓放行前被篡改：丢掉一条边（半包），封存条数仍为原数
    edges_data.pop()
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE promo_packages SET payload=? WHERE id=?",
            (json.dumps(edges_data, ensure_ascii=False), pkg["id"]),
        )

    with pytest.raises(ServiceError) as exc:
        promo.release_package(int(pkg["id"]), db_path=db_path)
    assert "半包直接拒写" in exc.value.message

    detail = promo.package_detail(int(pkg["id"]), db_path=db_path)
    assert detail["snapshot_edges"] == []
    assert detail["target_edges"] == []
    assert detail["receipt"] is None
    assert promo.list_target_edges(db_path=db_path) == []
    # 半包拦截后无锁残留，可在修正后重新放行
    assert promo.list_locks(db_path=db_path) == []


def test_tampered_target_edges_block_full_ingest(db_path):
    pkg = promo.create_package(db_path=db_path)
    promo.release_package(int(pkg["id"]), db_path=db_path)
    # 目标仓边表被塞进一条无凭证的残缺/多余边，指纹与条数随之对不上
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO promo_target_edges(package_id, edge_code, upstream_code, "
            "downstream_code, transform_kind, edge_fingerprint, source_repo, receive_mode) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (pkg["id"], "E-INTRUDER", "N-ODS-ORD", "N-ADS-DASH", "直抽",
             "x", "源仓", "快照表"),
        )

    with pytest.raises(ServiceError) as exc:
        promo.run_full_ingest(db_path=db_path)
    assert "拦住全量抓取入库" in exc.value.message
    runs = promo.list_ingest_runs(db_path=db_path)
    assert runs[0]["status"] == "已拦"
    assert runs[0]["actual_count"] != runs[0]["expected_count"]


def test_file_mode_materializes_payload_and_single_receipt_row(db_path):
    promo.set_receive_mode("文件凭证", db_path=db_path)
    pkg = promo.create_package(db_path=db_path)
    res = promo.release_package(int(pkg["id"]), holder="值班乙", db_path=db_path)
    assert res["receive_mode"] == "文件凭证"

    detail = promo.package_detail(int(pkg["id"]), db_path=db_path)
    # 文件凭证方式不写快照表
    assert detail["snapshot_edges"] == []
    receipt = detail["receipt"]
    assert receipt["receive_mode"] == "文件凭证"
    assert receipt["payload_file"].endswith(".json")
    # 整包已物化成文件，内容条数与指纹可核验
    with open(receipt["payload_file"], encoding="utf-8") as fh:
        materialized = json.load(fh)
    assert materialized["edge_count"] == int(pkg["edge_count"])
    assert materialized["edge_fingerprint"] == pkg["edge_fingerprint"]
    # 目标边表按文件凭证方式打标，与快照表方式内容来源标记不同
    targets = promo.list_target_edges(db_path=db_path)
    assert all(t["receive_mode"] == "文件凭证" for t in targets)

    # 重复放行被单次写入约束挡住
    with pytest.raises(ServiceError) as exc:
        promo.release_package(int(pkg["id"]), db_path=db_path)
    assert "不可重复放行" in exc.value.message


def test_receive_modes_are_mutually_exclusive_and_validated(db_path):
    assert promo.get_receive_mode(db_path) == "快照表"
    with pytest.raises(ServiceError):
        promo.set_receive_mode("双写", db_path=db_path)
    promo.set_receive_mode("文件凭证", db_path=db_path)
    assert promo.get_receive_mode(db_path) == "文件凭证"
    promo.set_receive_mode("快照表", db_path=db_path)
    assert promo.get_receive_mode(db_path) == "快照表"


def test_concurrent_release_lock_prevents_double_receipt(db_path):
    pkg = promo.create_package(db_path=db_path)
    # 模拟两侧值班抢点：一侧已持排他写锁且在事务中，另一侧立即被挡
    holder_conn = connect(db_path)
    holder_conn.execute("BEGIN IMMEDIATE")
    holder_conn.execute(
        "INSERT INTO promo_release_locks(package_id, holder) VALUES (?,?)",
        (pkg["id"], "值班甲"),
    )
    try:
        with pytest.raises(ServiceError) as exc:
            promo.release_package(int(pkg["id"]), holder="值班乙", db_path=db_path)
        assert "排他锁" in exc.value.message
    finally:
        holder_conn.execute("ROLLBACK")
        holder_conn.close()

    # 锁释放后只有一次放行能落凭证
    res = promo.release_package(int(pkg["id"]), holder="值班甲", db_path=db_path)
    assert res["receipt"]["holder"] == "值班甲"
    receipts = promo.list_receipts(db_path=db_path)
    assert len(receipts) == 1
    assert len(promo.list_target_edges(db_path=db_path)) == int(pkg["edge_count"])


def test_release_failure_chinese_error_and_rollback(db_path):
    pkg = promo.create_package(db_path=db_path)
    # 目标仓缺节点：整包拒写，凭证与边表均无残留
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE lineage_nodes SET node_code='N-RENAMED' WHERE node_code='N-ODS-ORD'"
        )
    with pytest.raises(ServiceError) as exc:
        promo.release_package(int(pkg["id"]), db_path=db_path)
    assert "整包拒写" in exc.value.message
    detail = promo.package_detail(int(pkg["id"]), db_path=db_path)
    assert detail["receipt"] is None
    assert detail["target_edges"] == []
    assert promo.list_locks(db_path=db_path) == []


def test_same_source_counts_and_promo_state(db_path):
    pkg = promo.create_package(source_repo="源仓A", db_path=db_path)
    promo.release_package(int(pkg["id"]), db_path=db_path)
    counts = promo.same_source_counts(db_path=db_path)
    assert counts == [{
        "source_repo": "源仓A",
        "receive_mode": "快照表",
        "edge_count": int(pkg["edge_count"]),
    }]
    state = promo.promo_state(db_path=db_path)
    assert state["package_received"] == 1
    assert state["last_receipt"]["source_repo"] == "源仓A"
    assert state["target_edge_total"] == int(pkg["edge_count"])


def test_ingest_without_receipt_blocked(db_path):
    with pytest.raises(ServiceError) as exc:
        promo.run_full_ingest(db_path=db_path)
    assert "禁止全量抓取入库" in exc.value.message
    assert promo.list_ingest_runs(db_path=db_path) == []


def test_file_mode_failure_removes_orphan_payload(db_path):
    promo.set_receive_mode("文件凭证", db_path=db_path)
    pkg = promo.create_package(db_path=db_path)
    # 目标仓缺节点 → 文件方式整包拒写，物化文件须随事务回滚一并清理
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE lineage_nodes SET node_code='N-RENAMED' WHERE node_code='N-ODS-ORD'"
        )
    with pytest.raises(ServiceError) as exc:
        promo.release_package(int(pkg["id"]), db_path=db_path)
    assert "整包拒写" in exc.value.message
    from app.services.promo import PROMO_DIR_NAME
    from pathlib import Path
    payload_dir = Path(db_path).parent / PROMO_DIR_NAME
    assert not payload_dir.exists() or not list(payload_dir.glob("*.json"))
    assert promo.list_receipts(db_path=db_path) == []
