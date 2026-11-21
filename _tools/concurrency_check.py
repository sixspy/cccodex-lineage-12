import os
import sys
import tempfile
import threading

sys.path.insert(0, "/workspace")
from app.seed import seed_if_needed
from app.services import promo
from app.services.errors import ServiceError

tmp = tempfile.mkdtemp()
db = os.path.join(tmp, "conc.db")
seed_if_needed(db)
pkg = promo.create_package(db_path=db)
pid = int(pkg["id"])

results = []
lock = threading.Lock()

def worker(holder):
    try:
        r = promo.release_package(pid, holder=holder, db_path=db)
        with lock:
            results.append(("ok", holder, r["receipt"]["receipt_code"]))
    except ServiceError as e:
        with lock:
            results.append(("blocked", holder, e.message))

threads = [threading.Thread(target=worker, args=(h,)) for h in ("值班甲", "值班乙")]
for t in threads:
    t.start()
for t in threads:
    t.join()

print("并发放行结果：")
for r in results:
    print(" ", r)
ok = [r for r in results if r[0] == "ok"]
blocked = [r for r in results if r[0] == "blocked"]
print("成功数:", len(ok), " 被拦数:", len(blocked))
receipts = promo.list_receipts(db_path=db)
targets = promo.list_target_edges(db_path=db)
locks = promo.list_locks(db_path=db)
print("凭证行数:", len(receipts), " 目标边表条数:", len(targets), " 残留锁:", len(locks))
assert len(ok) == 1, "必须恰好一个值班放行成功"
assert len(blocked) == 1, "被拦方必须收到中文锁提示"
assert len(receipts) == 1, "凭证只能单次写入一行"
assert len(targets) == int(pkg["edge_count"])
assert locks == [], "放行后无锁残留"
assert any("排他锁" in r[2] for r in blocked)
print("\nCONCURRENCY CHECK PASSED")
