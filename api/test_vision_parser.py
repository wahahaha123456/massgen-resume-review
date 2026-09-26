"""离线对比：PyPDF2 纯文本解析 vs qwen3-vl 视觉解析。

用法：
    uv run python api/test_vision_parser.py

会生成一份带表格/分栏布局的文本型 PDF 简历，
分别用两条路径解析，打印耗时/字数/内容对比。
只调一次视觉模型（约 1 页图片，成本很低）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from api.resume_parser import extract_pdf
from api.vision_parser import VisionParseError, extract_via_vision, pdf_to_images

HERE = Path(__file__).resolve().parent
PDF_PATH = HERE / "_vision_test_resume.pdf"


def make_table_layout_pdf(path: Path) -> None:
    """画一份左右双栏简历（左侧栏 + 右正文），内容按 y 扫描顺序写入。

    真实简历最常见的布局；PyPDF2 会按内容流顺序把左右两栏逐行交错抽出。
    """
    import pymupdf as fitz

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # A4
    g = fitz.Rect(45, 45, 205, 800)   # 左栏（侧栏）
    m = fitz.Rect(225, 45, 550, 800)  # 右栏（正文）

    # 按视觉 y 扫描顺序交错写入左右栏 —— 模拟 Word/浏览器导出的内容流
    bands: list[tuple[str, str]] = [
        (
            "CONTACT\nwangwu@example.com\n+86 138-0000-1234\ngithub.com/wangwu\nShanghai",
            "Wang Wu\nPython Backend Engineer\n"
            "3 years experience in high-concurrency backend systems.",
        ),
        (
            "SKILLS\nPython, Go\nDjango, FastAPI, Gin\nMySQL, Redis, Kafka\n"
            "Docker, Kubernetes\nCelery, CI/CD",
            "EXPERIENCE\n2023.07 - now  Acme Tech / Backend Engineer\n"
            "Order API: QPS 200 -> 800, P95 500ms -> 120ms\n"
            "2022.06 - 2023.03  Globex Inc. / Backend Intern\n"
            "Data pipeline: 100M events/day, Kafka + Celery",
        ),
        (
            "EDUCATION\n2019.09 - 2023.06\nB.E. Computer Science\nEast China University\nGPA 3.7/4.0\n\n"
            "LANGUAGES\nEnglish CET-6\nMandarin native",
            "PROJECTS\nShort-link Service: 6-char base62 IDs, Redis cache,\n"
            "30k QPS, 99.9% availability\n"
            "Resume Reviewer: multi-agent LLM system, 3 reviewers\n"
            "+ voting, FastAPI async task queue",
        ),
    ]

    y_top = 45.0
    for left, right in bands:
        # 每个 band 先写左栏再写右栏，制造内容流上的左右交错
        lrect = fitz.Rect(g.x0, y_top, g.x1, y_top + 220)
        rrect = fitz.Rect(m.x0, y_top, m.x1, y_top + 220)
        page.insert_textbox(lrect, left, fontsize=9, fontname="helv", lineheight=1.5)
        page.insert_textbox(rrect, right, fontsize=9, fontname="helv", lineheight=1.5)
        y_top += 240

    # 侧栏底色（浅灰），纯视觉，不影响文本流
    page.draw_rect(fitz.Rect(0, 0, 220, 842), color=None, fill=(0.96, 0.96, 0.96), overlay=0)

    doc.save(str(path))
    doc.close()


def main() -> None:
    load_dotenv()
    print(f"生成表格布局测试 PDF: {PDF_PATH}")
    make_table_layout_pdf(PDF_PATH)
    data = PDF_PATH.read_bytes()
    print(f"PDF 大小: {len(data) / 1024:.1f} KB\n")

    # ---- 路径 1：PyPDF2 ----
    t0 = time.perf_counter()
    text_pypdf = extract_pdf(data)
    t_pypdf = time.perf_counter() - t0
    print("=" * 70)
    print(f"【PyPDF2】耗时 {t_pypdf:.2f}s，{len(text_pypdf)} 字符")
    print("-" * 70)
    print(text_pypdf)

    # ---- 路径 2：视觉 ----
    print("\n" + "=" * 70)
    images = pdf_to_images(data)
    print(f"渲染 {len(images)} 页 PNG，首页 {len(images[0]) / 1024:.0f} KB")
    t0 = time.perf_counter()
    try:
        text_vision = extract_via_vision(images)
    except VisionParseError as exc:
        print(f"视觉解析失败：{exc}")
        return
    t_vision = time.perf_counter() - t0
    print(f"【qwen3-vl】耗时 {t_vision:.1f}s，{len(text_vision)} 字符")
    print("-" * 70)
    print(text_vision)

    # ---- 结构化对比 ----
    print("\n" + "=" * 70)
    print("对比要点（人工核对）：")
    print(f"1. PyPDF2 行数 {len(text_pypdf.splitlines())} / 视觉行数 {len(text_vision.splitlines())}")
    checks = ["QPS", "Kafka", "base62", "GPA 3.7", "30k QPS"]
    for kw in checks:
        print(
            f"2. 关键信息 '{kw}': "
            f"PyPDF2={'有' if kw in text_pypdf else '无'} / "
            f"视觉={'有' if kw in text_vision else '无'}"
        )


if __name__ == "__main__":
    main()
