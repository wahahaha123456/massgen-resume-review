"""快速配置对比测试：与 12 分钟基线相同的短简历，记录耗时与结果。"""

import json
import time
import urllib.request

url = "http://127.0.0.1:8765/review"
payload = {
    "resume": "张三，2024年计算机专业毕业，技能Python/Django。",
    "jd": "Python后端开发",
}
req = urllib.request.Request(
    url,
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)

print("Sending request (fast config: lenient / 1 answer / 0 restarts)...")
t0 = time.time()
with urllib.request.urlopen(req, timeout=2500) as resp:
    status = resp.status
    body = json.loads(resp.read().decode("utf-8"))
elapsed = time.time() - t0

print(f"HTTP STATUS: {status}")
print(f"ELAPSED_SECONDS: {elapsed:.0f}")
print(f"task_id: {body['task_id']}")
print(f"winner: {body['winner']}")
print(f"reviews: {len(body['reviews'])}")
for r in body["reviews"]:
    print(f"  - {r['agent_id']}: {len(r['content'])} 字")
print(f"votes: {len(body['votes'])}")
for v in body["votes"]:
    print(f"  - {v['voter']} -> {v['target']} (round {v['coordination_round']})")
print(f"log_dir: {body['log_dir']}")
print("=" * 60)
for r in body["reviews"]:
    print(f"\n##### {r['agent_id']} [{r['dimension']}] #####")
    print(r["content"])
print("=" * 60)
print("VOTE REASONS:")
for v in body["votes"]:
    print(f"\n[{v['voter']} -> {v['target']}]")
    print(v["reason"])

with open("api/test_fast_config.json", "w", encoding="utf-8") as f:
    json.dump(body, f, ensure_ascii=False, indent=2)
print("\nsaved -> api/test_fast_config.json")
