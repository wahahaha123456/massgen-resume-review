# 多 Agent 简历评审系统

基于 [MassGen](README_MASSGEN.md) 二次开发的简历智能评审工具：三个 AI Agent 从**技术、经历、改进建议**三个维度并行评审简历，通过多轮协商与投票产出最终结论，附带投票记录与置信度信号。

## 功能

- **三维度评审**：技术栈匹配 / 项目经历评估 / 改进建议（各限 300 字，防冗余）
- **投票共识**：Agent 交叉投票选出最佳答案，投票理由公开可见
- **文件上传**：支持 PDF / Word / TXT / Markdown 简历（≤10MB，自动解析文本）
- **JD 选填**：不填求职意向时，由 AI 从简历内容自动推断
- **异步任务**：提交后立即返回 task_id，前端轮询进度，不阻塞操作
- **输出清洗**：自动剥离 Agent 内部协作元信息（工作区路径、决策日志等），只留评审正文

## 架构

```
浏览器 (api/index.html)
    ↓ HTTP 轮询
FastAPI (api/main.py, 端口 8765)
    ↓ asyncio.create_subprocess_exec
MassGen CLI (uv run massgen --automation)
    ↓ 编排
三个 Agent (DeepSeek API)
    ↓ 答案落盘
.massgen/massgen_logs/*/answer.txt + vote.json
    ↓ 解析 + 清洗
聚合 JSON（reviews / votes / winner / jd_source）
```

## 二次开发点（相对上游 MassGen）

| 改动 | 位置 | 说明 |
|---|---|---|
| 评审编排配置 | `massgen/configs/resume_review.yaml` | 三 Agent 角色 + system_message 源头约束 + 快速协调参数 |
| FastAPI 异步层 | `api/main.py` | 任务字典 + 后台任务防 GC + 状态轮询 |
| 输出解析清洗 | `api/massgen_runner.py` | final/ 优先读取 + 围栏优先清洗器（11 份回归 fixtures） |
| 文件解析 | `api/resume_parser.py` | PyPDF2 / python-docx 延迟加载，GBK 回退 |
| 前端 | `api/index.html` | 纯 HTML/CSS/JS，无框架依赖 |
| Bug 修复 ×3 | `massgen/cli/backends.py` 等 | api_key 透传、Windows 快照 dirs_exist_ok |
| 性能优化 | coordination 参数调优 | A/B 实测：**347s vs 768s（-55%），质量持平** |

## 实测数据

| 指标 | 数值 |
|---|---|
| 单次评审耗时 | 5-13 分钟（3 Agent × 1-2 轮协调） |
| 接口响应（提交） | 53ms（异步） |
| 事件循环阻塞 | 0ms（`/health` 在评审中延迟 <25ms） |
| 清洗器回归测试 | 13 个用例，11 份真实答案 fixtures |
| 边界处理 | .exe 拒绝 / 空文件 422 / >10MB 拒绝 |

## 快速开始

```bash
# 1. 安装依赖（需要 uv）
uv pip install -e .

# 2. 配置 API Key（项目根目录 .env）
#    DEEPSEEK_API_KEY=sk-xxx

# 3. 启动服务（Windows 可直接双击 start.bat）
uv run uvicorn api.main:app --host 0.0.0.0 --port 8765 --reload

# 4. 浏览器打开
#    http://127.0.0.1:8765/
```

运行测试：

```bash
uv run pytest api/test_cleaner_fixtures.py -v   # 清洗器回归（离线）
uv run pytest api/test_parser.py -v             # 文件解析
```

## 截图

<!-- TODO: 待补充 -->
- 首页输入界面：`docs/screenshots/input.png`（占位）
- 评审结果页：`docs/screenshots/result.png`（占位）

## 已知限制

- 任务状态存内存，服务重启后丢失（个人项目可接受）
- 单次评审消耗约 3-5 万 token（DeepSeek API）
- PDF 表格型简历可能解析乱序（文本型 PDF 正常）
- 仅支持 DeepSeek / 阿里百炼模型（上游支持更多，但未实测）

## 致谢

本项目基于 [MassGen](https://github.com/Leezekun/MassGen)（多 Agent 编排框架）二次开发。上游 README 见 [README_MASSGEN.md](README_MASSGEN.md)。
