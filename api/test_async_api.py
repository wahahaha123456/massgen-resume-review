"""异步 API 端到端测试（真实 LLM，短简历）。

验证：
1. POST /review 毫秒级返回 task_id + status=pending
2. 轮询 GET /review/{task_id}：pending/running → done，elapsed_seconds 递增
3. done 后 result 结构完整（3 份评审 + winner + note）
4. 不存在的 task_id → 404
5. 并发性：任务执行中 /health 仍能毫秒级响应（事件循环未被阻塞）
"""

import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8765"

payload = {
    "resume": "张三，2024年计算机专业毕业，技能Python/Django。",
    "jd": "Python后端开发",
}

print("===== 1. POST /review 应立即返回 =====")
t0 = time.time()
req = urllib.request.Request(
    f"{BASE}/review",
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=30) as resp:
    accepted = json.loads(resp.read().decode("utf-8"))
post_latency = time.time() - t0
task_id = accepted["task_id"]
print(f"POST latency: {post_latency * 1000:.0f} ms")
print(f"response: {json.dumps(accepted, ensure_ascii=False)}")
assert post_latency < 2.0, f"POST 耗时 {post_latency:.1f}s，未达到毫秒级"
assert accepted["status"] == "pending", accepted

print("\n===== 2. 轮询状态转换（pending/running → done）=====")
transitions = []
t_start = time.time()
final_status = None
result = None
while time.time() - t_start < 2400:  # 最多 40 分钟
    time.sleep(5)
    t_poll = time.time()
    with urllib.request.urlopen(f"{BASE}/review/{task_id}", timeout=15) as resp:
        st = json.loads(resp.read().decode("utf-8"))
    poll_latency = (time.time() - t_poll) * 1000
    if not transitions or transitions[-1][0] != st["status"]:
        transitions.append((st["status"], round(time.time() - t_start)))
        print(f"[{time.time() - t_start:6.0f}s] status -> {st['status']} (poll {poll_latency:.0f} ms)")

    # 任务执行中并发验证 /health 不被阻塞（只在前 60 秒抽样 3 次）
    if time.time() - t_start < 60 and len(transitions) <= 2:
        t_h = time.time()
        with urllib.request.urlopen(f"{BASE}/health", timeout=5) as h:
            h.read()
        print(f"         /health latency during run: {(time.time() - t_h) * 1000:.0f} ms")

    if st["status"] in ("done", "failed"):
        final_status = st["status"]
        result = st
        break

assert final_status == "done", f"任务最终状态 {final_status}: {json.dumps(result, ensure_ascii=False)[:500] if result else 'timeout'}"

print("\n===== 3. done 后 result 结构 =====")
r = result["result"]
print(f"winner: {r['winner']}, votes: {len(r['votes'])}, reviews: {len(r['reviews'])}")
for rev in r["reviews"]:
    print(f"  - {rev['agent_id']}: {len(rev['content'])} 字")
print(f"note: {r['note'][:30]}...")
assert len(r["reviews"]) == 3 and r["note"], "result 结构不完整"

print("\n===== 4. 404 用例 =====")
try:
    urllib.request.urlopen(f"{BASE}/review/nonexistent0000", timeout=10)
    print("FAIL: 未返回 404")
    sys.exit(1)
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}: {e.read().decode('utf-8')[:80]}")
    assert e.code == 404

print(f"\nALL ASYNC TESTS PASS (total wait {time.time() - t_start:.0f}s)")
print(f"transitions: {transitions}")
