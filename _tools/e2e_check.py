import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx

PORT = 7413
DB = sys.argv[1]


def wait_up(client):
    for _ in range(50):
        try:
            if client.get("/login").status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise RuntimeError("server not up")


def run_case(receive_mode, label):
    db = f"{DB}_{label}.db"
    Path(db).unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(db + suffix).unlink(missing_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "run.py"],
        cwd="/workspace",
        env={"PATH": "/usr/bin:/bin", "DATABASE_PATH": db,
             "HOST": "127.0.0.1", "PORT": str(PORT)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{PORT}", timeout=10) as c:
            wait_up(c)
            assert c.post("/api/login", json={"username": "admin", "password": "123456"}).status_code == 200
            c.post("/api/promo/mode", json={"receive_mode": receive_mode})
            assert c.get("/api/promo/mode").json()["receive_mode"] == receive_mode
            pkg = c.post("/api/promo/packages", json={"source_repo": "源仓", "target_repo": "目标仓"}).json()["item"]
            rel = c.post(f"/api/promo/packages/{pkg['id']}/release", json={"holder": "值班甲"}).json()
            assert rel["receive_mode"] == receive_mode
            det = c.get(f"/api/promo/packages/{pkg['id']}").json()
            targets = c.get("/api/promo/target-edges").json()["items"]
            receipt = det["receipt"]
            print(f"\n[{label}] mode={receive_mode}")
            print("  封存条数:", pkg["edge_count"], " 目标边表条数:", len(targets),
                  " 快照表条数:", len(det["snapshot_edges"]))
            print("  凭证 payload_file:", repr(receipt["payload_file"]),
                  " holder:", receipt["holder"], " status:", receipt["status"])
            print("  目标边表 receive_mode 标记:", sorted({t["receive_mode"] for t in targets}))
            assert len(targets) == pkg["edge_count"]
            assert all(t["receive_mode"] == receive_mode for t in targets)
            assert det["lock"] is None
            if receive_mode == "快照表":
                assert len(det["snapshot_edges"]) == pkg["edge_count"]
                assert receipt["payload_file"] == ""
            else:
                assert det["snapshot_edges"] == []
                assert receipt["payload_file"].endswith(".json")
                mat = json.loads(Path(receipt["payload_file"]).read_text(encoding="utf-8"))
                assert mat["edge_fingerprint"] == pkg["edge_fingerprint"]
            # 全量入库已通
            run = c.post("/api/promo/ingest", json={}).json()["run"]
            assert run["status"] == "已通"
            # 重复放行中文报错
            dup = c.post(f"/api/promo/packages/{pkg['id']}/release", json={})
            assert dup.status_code == 409 and "不可重复放行" in dup.json()["error"]
        return db, pkg
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def main():
    db_a, pkg_a = run_case("快照表", "snapshot")
    db_b, pkg_b = run_case("文件凭证", "file")

    # 半包拒写：直接在快照库篡改 payload 丢一条边，再单独起服务放行
    conn = sqlite3.connect(db_a)
    edges = json.loads(conn.execute("SELECT payload FROM promo_packages WHERE id=1").fetchone()[0])
    edges.pop()
    # 先把已接收状态重置为已打包，模拟第二个包放行前被抽掉一条
    conn.execute("UPDATE promo_packages SET status='已打包', payload=? WHERE id=1",
                 (json.dumps(edges, ensure_ascii=False),))
    conn.commit()
    conn.close()
    proc = subprocess.Popen(
        [sys.executable, "run.py"], cwd="/workspace",
        env={"PATH": "/usr/bin:/bin", "DATABASE_PATH": db_a, "HOST": "127.0.0.1", "PORT": str(PORT)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{PORT}", timeout=10) as c:
            wait_up(c)
            c.post("/api/login", json={"username": "admin", "password": "123456"})
            r = c.post("/api/promo/packages/1/release", json={"holder": "值班乙"})
            print("\n[半包] 放行响应:", r.status_code, r.json())
            assert r.status_code == 409 and "半包直接拒写" in r.json()["error"]
            det = c.get("/api/promo/packages/1").json()
            assert det["receipt"] is not None  # 首个成功凭证仍在
            # 篡改目标边表制造指纹不符 → 入库被拦
            conn = sqlite3.connect(db_a)
            conn.execute(
                "INSERT INTO promo_target_edges(package_id, edge_code, upstream_code, downstream_code, "
                "transform_kind, edge_fingerprint, source_repo, receive_mode) "
                "VALUES (1,'E-INTRUDER','x','y','直抽','z','源仓','快照表')")
            conn.commit(); conn.close()
            blocked = c.post("/api/promo/ingest", json={})
            print("[篡改] 入库响应:", blocked.status_code, blocked.json())
            assert blocked.status_code == 409 and "拦住全量抓取入库" in blocked.json()["error"]
            assert c.get("/api/promo/ingest").json()["items"][0]["status"] == "已拦"
            assert c.get("/api/promo/locks").json()["items"] == []
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    print("\nALL E2E CHECKS PASSED")


if __name__ == "__main__":
    main()
