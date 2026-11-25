"""观点与讨论系统配置。"""
from __future__ import annotations

from pydantic import Field

from maibot_sdk import PluginConfigBase


class OpinionSettings(PluginConfigBase):
    """插件配置；不含 disagree_probability 一类会扭曲目标的开关。"""

    config_version: str = Field(default="0.1.0", description="配置结构版本；缺少或清空时插件拒绝启用")

    enabled: bool = Field(default=False, description="总开关；首次部署建议关闭，完成离线验收后再开启")
    shadow_mode: bool = Field(default=True, description="试运行：只记录判断，不改写回复")
    bot_account_id: str = Field(default="", description="机器人自己的 QQ 号，用于区分平台、账号与场景")
    private_user_ids: list[str] = Field(default_factory=list, description="允许使用的私聊 QQ 号，留空表示不启用私聊")
    group_ids: list[str] = Field(default_factory=list, description="允许使用的群号，留空表示不在群聊启用")
    webui_test_prefix: str = Field(default="", description="仅用于本地验收的 WebUI 身份前缀；留空则不启用网页会话")

    enable_web_evidence: bool = Field(default=False, description="是否采纳宿主工具结果作为证据；需先通过适配验收")
    search_per_turn_max: int = Field(default=2, ge=0, le=5, description="每轮最多搜索次数（仅记录与提示，实际由 Planner 执行）")
    read_per_turn_max: int = Field(default=2, ge=0, le=5, description="每轮最多正文读取次数（同上）")
    analysis_output_tokens_max: int = Field(default=800, ge=300, le=1500, description="单次结构化分析的最大输出 token")
    analysis_task: str = Field(default="utils", description="结构化分析使用的 MaiBot 模型任务")
    analysis_timeout_seconds: float = Field(default=120.0, ge=10.0, le=120.0, description="后台分析总等待时间，含候选模型切换")
    analysis_min_interval_seconds: int = Field(default=20, ge=5, le=300, description="同一会话两次分析的间隔下限")

    default_criteria: list[str] = Field(
        default_factory=lambda: ["稳定性", "关键决策", "团队贡献", "精彩操作"],
        description="评价赛事/表现的默认关注点，可被用户明确指定的标准覆盖",
    )
    discussion_review_after_turns: int = Field(default=3, ge=2, le=6, description="第几轮开始检查是否还有信息增量")
    no_new_information_limit: int = Field(default=2, ge=1, le=4, description="连续几轮没有新信息就允许保留分歧并收尾")
    repeat_window_turns: int = Field(default=2, ge=1, le=4, description="最近几轮说过的理由不再换个说法重复")

    topic_active_ttl_hours: int = Field(default=24, ge=1, le=168, description="活跃讨论状态保留时长")
    closed_topic_retention_days: int = Field(default=7, ge=1, le=30, description="已结束话题保留天数")
    audit_retention_days: int = Field(default=14, ge=1, le=90, description="审计元数据保留天数")
    evidence_retention_days: int = Field(default=3, ge=1, le=30, description="证据缓存保留天数")
    max_prompt_chars: int = Field(default=900, ge=300, le=2000, description="注入提示块的最大字符数")


class OpinionConfig(PluginConfigBase):
    """顶层配置。"""

    plugin: OpinionSettings = Field(default_factory=OpinionSettings)
