"""上传接口端到端测试：真实 docx 简历 → 三 Agent 评审。"""

import json
import time
import urllib.request

URL = "http://127.0.0.1:8765/review/upload"
FILE = r"d:\MassGen\api\sample_resume.docx"
JD = "Python 后端开发工程师"

boundary = "----massgene2eboundary456"
with open(FILE, "rb") as f:
    file_content = f.read()

parts = [
    f"--{boundary}\r\n".encode(),
    b'Content-Disposition: form-data; name="file"; filename="sample_resume.docx"\r\n',
    b"Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\r\n\r\n",
    file_content,
    b"\r\n",
    f"--{boundary}\r\n".encode(),
    b'Content-Disposition: form-data; name="jd"\r\n\r\n',
    JD.encode("utf-8"),
    b"\r\n",
    f"--{boundary}--\r\n".encode(),
]
body = b"".join(parts)

req = urllib.request.Request(
    URL,
    data=body,
    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    method="POST",
)

print(f"uploading {FILE} ({len(file_content)} bytes)...")
t0 = time.time()
with urllib.request.urlopen(req, timeout=2500) as resp:
    status = resp.status
    data = json.loads(resp.read().decode("utf-8"))
elapsed = time.time() - t0

print(f"HTTP STATUS: {status}")
print(f"Elapsed: {elapsed:.0f}s")
print(f"task_id: {data['task_id']}")
print(f"source_filename: {data.get('source_filename')}")
print(f"winner: {data['winner']}")
print(f"reviews: {len(data['reviews'])} 份")
for r in data["reviews"]:
    print(f"  - {r['agent_id']} ({r['dimension']}): {len(r['content'])} 字")
print(f"votes: {len(data['votes'])} 条")
for v in data["votes"]:
    print(f"  - {v['voter']} -> {v['target']}")
print(f"note: {data['note']}")
print("=" * 60)
for r in data["reviews"]:
    print(f"\n##### {r['agent_id']} [{r['dimension']}] #####")
    print(r["content"][:400])

with open("api/test_upload_response.json", "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
print("\nsaved -> api/test_upload_response.json")
