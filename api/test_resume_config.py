"""resume_review.yaml 快速模式装配的离线回归（不调 LLM）。

质量不降的硬约束：
- 3 个 agent 维度不减少、system_message 的 300 字结构要求保留
- 三个 agent 统一用 deepseek-chat（经历维度是结构化列举评审；reasoner
  实测思考链 74-134s 高方差且产出同档，是总时长决定性长板）

提速约束（对应多 agent refinement-OFF 快速模式，见 tui_modes.py）：
- 每个 agent 只答一次（max_new_answers_per_agent=1，口径含首答）
- skip_final_presentation：不再为 winner 多跑一次展示 LLM 调用
- disable_injection + defer_voting_until_all_answered：
  agent 独立并行作答，先交卷者零成本等待全员
- skip_voting：全员交卷即结束，不进投票轮（投票轮会让模型重试 + 重写答案）
- 去掉 backend.cwd：纯文本评审不需要文件系统 MCP（实测每次 run
  5-6 次 write_file/create_directory 工具调用，纯延迟 + 诱发格式漂移）
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from massgen.agent_config import AgentConfig, CoordinationConfig

CONFIG_PATH = (
    Path(__file__).resolve().parent.parent
    / "massgen" / "configs" / "resume_review.yaml"
)


@pytest.fixture(scope="module")
def raw_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture(scope="module")
def agent_config(raw_config: dict) -> AgentConfig:
    cfg = AgentConfig()
    cfg.apply_orchestrator_config(raw_config.get("orchestrator", {}))
    cfg.coordination_config = CoordinationConfig.from_dict(
        raw_config.get("orchestrator", {}).get("coordination", {})
    )
    return cfg


def test_three_dimensions_preserved(raw_config: dict) -> None:
    ids = [a["id"] for a in raw_config["agents"]]
    assert ids == ["tech_reviewer", "experience_reviewer", "improvement_reviewer"]


def test_all_agents_use_chat_for_speed(raw_config: dict) -> None:
    """三维度统一 deepseek-chat：评审是结构化短输出（评分+要点列举），
    chat 产出与 reasoner 同档；reasoner 思考链 74-134s 是决定性时长板。"""
    models = {a["id"]: a["backend"]["model"] for a in raw_config["agents"]}
    assert models == {
        "tech_reviewer": "deepseek-chat",
        "experience_reviewer": "deepseek-chat",
        "improvement_reviewer": "deepseek-chat",
    }


def test_system_messages_keep_300_char_contract(raw_config: dict) -> None:
    for agent in raw_config["agents"]:
        msg = agent["system_message"]
        assert "300" in msg and "第一行就是正文" in msg, agent["id"]


def test_no_backend_cwd_means_no_filesystem_tools(raw_config: dict) -> None:
    for agent in raw_config["agents"]:
        assert "cwd" not in agent["backend"], (
            f"{agent['id']} 仍配置 cwd，会注入 filesystem MCP 工具，拖慢且诱发写文件行为"
        )


def test_one_answer_per_agent(agent_config: AgentConfig) -> None:
    assert agent_config.max_new_answers_per_agent == 1


def test_skip_final_presentation(agent_config: AgentConfig) -> None:
    assert agent_config.skip_final_presentation is True


def test_independent_parallel_first_round(agent_config: AgentConfig) -> None:
    assert agent_config.disable_injection is True
    assert agent_config.defer_voting_until_all_answered is True


def test_skip_voting_ends_right_after_all_answered(agent_config: AgentConfig) -> None:
    """全员交卷即结束，不再 restart 进投票轮。

    实测（log_20260926_131812）：仅 defer 不加 skip_voting 时，全员交卷后
    三个 agent 仍被拉进投票轮，模型调不对 vote 工具（agent_id 格式错误
    重试、buffer 注入），又各重写一遍答案，白耗 ~43s（117s -> 160s）。
    skip_voting=True 时 _coordination_complete 在全员有答案后立即返回 True；
    无投票时 winner 由 determine_final_agent_from_votes 兜底为首个有答案的
    agent，三份原答案照常产出。
    """
    assert agent_config.skip_voting is True


def test_no_orchestration_restarts(agent_config: AgentConfig) -> None:
    assert agent_config.coordination_config.max_orchestration_restarts == 0


def test_coordination_block_has_no_unknown_keys(
    raw_config: dict, caplog: pytest.LogCaptureFixture
) -> None:
    """runtime 字段（voting_sensitivity 等）必须放在 orchestrator 顶层；
    放 coordination: 下会被 CoordinationConfig 当未知键忽略（静默默认）。"""
    coord = raw_config.get("orchestrator", {}).get("coordination", {})
    with caplog.at_level(logging.WARNING, logger="massgen.agent_config"):
        CoordinationConfig.from_dict(coord)
    assert not [r for r in caplog.records if "Unknown orchestrator.coordination" in r.message]
