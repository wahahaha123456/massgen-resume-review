"""上传接口校验路径测试：错误类型/空文件 → 应立即 422，不调用 LLM。"""

import sys
import urllib.request

sys.path.insert(0, r"d:\MassGen")

URL = "http://127.0.0.1:8765/review/upload"


def multipart(filename: str, content: bytes, jd: str, field: str = "file") -> bytes:
    boundary = "----massgentestboundary123"
    parts = []
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode()
    )
    parts.append(b"Content-Type: application/octet-stream\r\n\r\n")
    parts.append(content)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(b'Content-Disposition: form-data; name="jd"\r\n\r\n')
    parts.append(jd.encode("utf-8"))
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), boundary


def post(filename, content, jd="Python后端开发"):
    body, boundary = multipart(filename, content, jd)
    req = urllib.request.Request(
        URL,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


# 用例1：不支持的扩展名
code, body = post("resume.xlsx", b"fake xlsx content")
print(f"[bad-ext]  HTTP {code}: {body[:150]}")
assert code == 422 and "不支持" in body, "bad-ext 应返回 422"

# 用例2：空文件
code, body = post("resume.txt", b"")
print(f"[empty]    HTTP {code}: {body[:150]}")
assert code == 422 and "为空" in body, "empty 应返回 422"

# 用例3：损坏 PDF
code, body = post("resume.pdf", b"definitely not a pdf")
print(f"[bad-pdf]  HTTP {code}: {body[:150]}")
assert code == 422 and "PDF" in body, "bad-pdf 应返回 422"

# 用例4：缺少 jd 字段（构造一个不带 jd 的请求）
boundary = "----massgentestboundary123"
raw = (
    f"--{boundary}\r\n"
    f'Content-Disposition: form-data; name="file"; filename="r.txt"\r\n'
    f"Content-Type: application/octet-stream\r\n\r\n"
    f"{'张三' * 20}\r\n"
    f"--{boundary}--\r\n"
).encode()
req = urllib.request.Request(
    URL, data=raw,
    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        code, body = resp.status, resp.read().decode()
except urllib.error.HTTPError as e:
    code, body = e.code, e.read().decode()
print(f"[no-jd]    HTTP {code}: {body[:150]}")
assert code == 422, "缺 jd 应返回 422"

print("\nALL VALIDATION TESTS PASS（均未触发 LLM 调用）")
