"""POST /review 端到端测试：真实调用三智能体评审，结果落盘 JSON。"""

import json
import urllib.request

url = "http://127.0.0.1:8765/review"
payload = {
    "jd": "Python 后端开发工程师",
    "resume": (
        "张三，2024年某大学计算机专业毕业。技能：Python、Django、MySQL、Redis、Docker。"
        "项目经历：1. 校园二手交易平台（2023），负责后端开发，用 Django 实现用户系统和商品管理，"
        "日均活跃用户约500人。2. 某公司实习（2024.3-2024.6），参与内部管理系统开发，"
        "用 Python 写数据清洗脚本，处理约10万条数据。"
    ),
}

req = urllib.request.Request(
    url,
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)

with urllib.request.urlopen(req, timeout=2500) as resp:
    status = resp.status
    body = json.loads(resp.read().decode("utf-8"))

print(f"HTTP STATUS: {status}")
print(f"task_id: {body['task_id']}")
print(f"winner: {body['winner']}")
print(f"reviews: {len(body['reviews'])} 份")
for r in body["reviews"]:
    print(f"  - {r['agent_id']} ({r['dimension']}): {len(r['content'])} 字")
print(f"votes: {len(body['votes'])} 条")
for v in body["votes"]:
    print(f"  - {v['voter']} -> {v['target']} (round {v['coordination_round']})")
print(f"note: {body['note']}")
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

with open("api/test_response.json", "w", encoding="utf-8") as f:
    json.dump(body, f, ensure_ascii=False, indent=2)
print("\nsaved -> api/test_response.json")
