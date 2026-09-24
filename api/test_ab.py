"""长简历 A/B 对比测试（同一个脚本跑两次，命令行参数区分输出文件）。"""

import json
import sys
import time
import urllib.request

RESUME = (
    "李四，2022年某大学软件工程专业毕业，3年后端开发经验。"
    "技能：Python、Django、FastAPI、MySQL、Redis、Docker、K8s、Celery、RabbitMQ。"
    "项目经历：1. 某电商平台订单系统（2022-2023），负责订单模块开发，"
    "用 Django + Celery 实现异步订单处理，QPS 从 200 提升到 800。"
    "2. 某 SaaS 系统重构（2023-2024），主导将单体架构迁移到微服务，"
    "用 FastAPI 重写核心接口，P95 响应从 500ms 降到 120ms。"
    "3. 某数据平台（2024-2025），负责实时数据处理，用 Kafka + Flink 处理日均 1 亿条数据。"
)
JD = "高级 Python 后端开发工程师，要求 3 年以上经验，熟悉微服务、高并发、分布式系统。"

tag = sys.argv[1] if len(sys.argv) > 1 else "run"
url = "http://127.0.0.1:8765/review"
payload = {"resume": RESUME, "jd": JD}
req = urllib.request.Request(
    url, data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"}, method="POST",
)

print(f"[{tag}] Sending long-resume review request...")
t0 = time.time()
with urllib.request.urlopen(req, timeout=2500) as resp:
    body = json.loads(resp.read().decode("utf-8"))
elapsed = time.time() - t0

print(f"[{tag}] ELAPSED_SECONDS: {elapsed:.0f}")
print(f"[{tag}] winner: {body['winner']}")
for r in body["reviews"]:
    print(f"[{tag}]   {r['agent_id']}: {len(r['content'])} 字")
print(f"[{tag}] votes: {len(body['votes'])}")
for v in body["votes"]:
    print(f"[{tag}]   {v['voter']} -> {v['target']} (round {v['coordination_round']})")
print("=" * 70)
for r in body["reviews"]:
    print(f"\n##### {r['agent_id']} [{r['dimension']}] ({len(r['content'])}字) #####")
    print(r["content"])
print("=" * 70)
for v in body["votes"]:
    print(f"\n[{v['voter']} -> {v['target']}] round {v['coordination_round']}")
    print(v["reason"][:500])

out = f"api/test_ab_{tag}.json"
body["_elapsed_seconds"] = round(elapsed)
with open(out, "w", encoding="utf-8") as f:
    json.dump(body, f, ensure_ascii=False, indent=2)
print(f"\nsaved -> {out}")
