"""多模态简历解析（PyPDF2 的补充路径，尚未接入主流程）。

路径：PDF --PyMuPDF 渲染--> 每页 PNG --qwen3-vl 视觉理解--> 结构化文本。
解决表格/分栏布局在 PyPDF2 纯文本提取中的乱序、串行问题。

模型：百炼 DashScope 的 qwen3-vl-32b-thinking（OpenAI 兼容端点）。
鉴权：环境变量 DASHSCOPE_API_KEY。
"""

from __future__ import annotations

import base64
import io
import os

import httpx
from openai import OpenAI

DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
VISION_MODEL = "qwen3-vl-32b-thinking"

RENDER_DPI = 150  # 简历 150dpi 清晰度与体积的平衡点
MAX_PAGES = 5  # 简历一般 1-2 页，5 页封顶防 token 爆炸
REQUEST_TIMEOUT_SECONDS = 180  # thinking 模型可能较慢

_EXTRACTION_PROMPT = (
    "你是简历信息提取器。请阅读这张简历图片，完整提取其中的全部信息，"
    "严格保持原有结构与顺序，用 Markdown 输出，要求：\n"
    "1. 按「个人信息 / 教育背景 / 专业技能 / 工作或实习经历 / 项目经历 / 其他」分节；\n"
    "2. 表格、分栏中的内容必须按语义正确归位，不要按视觉位置逐列拼接；\n"
    "3. 保留公司、学校、岗位、时间、技术名词、数字指标等原文，不要翻译、不要补充、不要评价；\n"
    "4. 图片中没有的信息不要编造；\n"
    "5. 只输出提取结果本身，不要输出思考过程、说明或寒暄。"
)


class VisionParseError(RuntimeError):
    """多模态解析失败（渲染失败 / API 错误 / 空结果）。"""


def pdf_to_images(data: bytes, dpi: int = RENDER_DPI, max_pages: int = MAX_PAGES) -> list[bytes]:
    """用 PyMuPDF 把 PDF 每页渲染成 PNG，返回 PNG 字节列表。"""
    import pymupdf as fitz  # PyMuPDF（1.24+ 新包名），延迟导入

    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    images: list[bytes] = []

    try:
        document = fitz.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise VisionParseError(f"PDF 无法渲染（可能已损坏或加密）：{exc}") from exc

    try:
        for index, page in enumerate(document):
            if index >= max_pages:
                break
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            images.append(pixmap.tobytes("png"))
    finally:
        document.close()

    if not images:
        raise VisionParseError("PDF 没有可渲染的页面")
    return images


def _build_messages(images: list[bytes]) -> list[dict]:
    content: list[dict] = [{"type": "text", "text": _EXTRACTION_PROMPT}]
    for png in images:
        b64 = base64.b64encode(png).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            }
        )
    return [{"role": "user", "content": content}]


def extract_via_vision(
    images: list[bytes],
    *,
    api_key: str | None = None,
    model: str = VISION_MODEL,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> str:
    """调用 qwen3-vl 视觉模型提取简历文本，返回结构化 Markdown。"""
    key = api_key or os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not key:
        raise VisionParseError("缺少 DASHSCOPE_API_KEY 环境变量")
    if not images:
        raise VisionParseError("没有可识别的图片")

    client = OpenAI(
        api_key=key,
        base_url=DASHSCOPE_BASE_URL,
        http_client=httpx.Client(timeout=timeout),
    )

    try:
        response = client.chat.completions.create(
            model=model,
            messages=_build_messages(images),
            temperature=0,
        )
    except Exception as exc:
        raise VisionParseError(f"视觉模型调用失败：{type(exc).__name__}: {exc}") from exc

    text = (response.choices[0].message.content or "").strip()
    if not text:
        raise VisionParseError("视觉模型返回为空")
    return text


def parse_pdf_vision(data: bytes) -> str:
    """组合入口：PDF bytes -> 渲染 -> 视觉提取 -> 文本（供后续接入主流程使用）。"""
    images = pdf_to_images(data)
    return extract_via_vision(images)
