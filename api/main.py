"""简历多智能体评审 API（异步任务版）。

启动：
    cd d:\\MassGen
    uv run uvicorn api.main:app --host 127.0.0.1 --port 8765

端点：
    GET  /health             健康检查
    POST /review             文本简历 + 岗位描述 → 毫秒级返回 task_id，后台执行评审
    POST /review/upload      上传简历文件（pdf/docx/txt/md）+ 岗位描述 → 立即返回 task_id
    GET  /review/{task_id}   轮询任务状态：pending | running | done | failed
    GET  /static/index.html  前端页面（/ 自动跳转）

任务状态存进程内存，服务重启即失效（个人项目可接受，不引入 Redis/Celery）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from api.massgen_runner import NOTE, run_resume_review
from api.resume_parser import ResumeParseError, parse_resume_file

app = FastAPI(
    title="MassGen 多智能体简历评审 API",
    version="0.2.0",
    description="三个 DeepSeek agent 分别从技术、经历、改进建议维度评审简历。"
    "提交后立即返回 task_id，通过 GET /review/{task_id} 轮询进度。",
)

# 本地开发：允许所有来源（前端后续单独部署在其他端口）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- 数据模型 ----------


class ReviewRequest(BaseModel):
    resume: str = Field(..., min_length=10, description="简历全文（纯文本）")
    jd: Optional[str] = Field(None, description="目标岗位名称或岗位描述（选填，留空则由模型从简历推断）")


class ReviewItem(BaseModel):
    agent_id: str
    dimension: str
    content: str


class VoteItem(BaseModel):
    voter: str
    target: str
    reason: str
    coordination_round: int | None = None


class ReviewResponse(BaseModel):
    task_id: str
    jd: str
    reviews: list[ReviewItem]
    votes: list[VoteItem]
    winner: str
    note: str
    log_dir: str
    exit_code: int
    source_filename: str | None = None
    # "user"=用户指定 JD；"inferred"=留空由模型推断。_execute_and_store 会显式赋值，
    # 这里的默认值只是兜底，旧客户端不读此字段不受影响
    jd_source: str = "user"


class TaskAccepted(BaseModel):
    task_id: str
    status: str
    detail: str = "评审已提交，用 GET /review/{task_id} 每 5 秒轮询进度"


class TaskStatusResponse(BaseModel):
    task_id: str
    status: str  # pending | running | done | failed
    result: ReviewResponse | None = None
    error: str | None = None
    elapsed_seconds: int


# ---------- 任务存储与后台执行 ----------

# task_id -> {"status", "result", "error", "created_at"}
_tasks: dict[str, dict] = {}

# asyncio 事件循环只对 Task 持弱引用，必须自持强引用防止任务被 GC 中途回收
_background_tasks: set[asyncio.Task] = set()


def spawn_background(coro) -> asyncio.Task:
    """创建后台任务并保存强引用，完成后自动释放。"""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _new_task() -> str:
    task_id = uuid.uuid4().hex
    _tasks[task_id] = {
        "status": "pending",
        "result": None,
        "error": None,
        "created_at": time.monotonic(),
    }
    return task_id


async def _execute_and_store(
    task_id: str, resume: str, jd: Optional[str], source_filename: str | None = None
) -> None:
    """后台执行评审：状态机 pending → running → done/failed。

    异常全部落进任务条目，不向外抛（后台任务无人 await，抛出只会变成
    "Task exception was never retrieved" 噪音）。
    """
    entry = _tasks[task_id]
    entry["status"] = "running"
    # 不传 / 空串 / 全空格统一归一为「未指定」，走模型推断分支
    jd_text = (jd or "").strip()
    jd_source = "user" if jd_text else "inferred"
    try:
        if len(resume.strip()) < 10:
            raise ValueError("解析出的简历文本不足 10 个字，请检查文件内容")
        # run_resume_review 内部用 asyncio.create_subprocess_exec 等待子进程，
        # 等待期间事件循环可继续处理其他请求，无需再包 asyncio.to_thread
        result = await run_resume_review(task_id=task_id, resume=resume, jd=jd_text)
        result["source_filename"] = source_filename
        result["jd_source"] = jd_source  # 显式赋值，不依赖模型默认值
        entry["result"] = ReviewResponse(**result)
        entry["status"] = "done"
    except Exception as exc:  # noqa: BLE001
        entry["error"] = f"{type(exc).__name__}: {exc}"
        entry["status"] = "failed"


# ---------- 端点 ----------


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/review", response_model=TaskAccepted)
async def create_review(req: ReviewRequest) -> TaskAccepted:
    task_id = _new_task()
    spawn_background(_execute_and_store(task_id, req.resume, req.jd))
    return TaskAccepted(task_id=task_id, status="pending")


@app.post("/review/upload", response_model=TaskAccepted)
async def create_review_from_upload(
    file: UploadFile = File(..., description="简历文件（.pdf/.docx/.txt/.md，≤10MB）"),
    jd: Optional[str] = Form(None, description="目标岗位名称或岗位描述（选填，留空则由模型从简历推断）"),
) -> TaskAccepted:
    data = await file.read()
    try:
        # 文件解析是毫秒级操作，同步做掉：坏文件在这里就 422，不进任务队列
        resume_text = parse_resume_file(file.filename or "", data)
    except ResumeParseError as exc:
        # 422：文件本身有问题（类型/损坏/无文本），区别于评审执行失败的 failed 状态
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    task_id = _new_task()
    spawn_background(
        _execute_and_store(task_id, resume_text, jd, source_filename=file.filename)
    )
    return TaskAccepted(task_id=task_id, status="pending")


@app.get("/review/{task_id}", response_model=TaskStatusResponse)
async def get_review(task_id: str) -> TaskStatusResponse:
    entry = _tasks.get(task_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="task_id 不存在或结果已随服务重启清除")
    return TaskStatusResponse(
        task_id=task_id,
        status=entry["status"],
        result=entry["result"],
        error=entry["error"],
        elapsed_seconds=int(time.monotonic() - entry["created_at"]),
    )


# ---------- 静态文件 ----------

_STATIC_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/static/index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765)
