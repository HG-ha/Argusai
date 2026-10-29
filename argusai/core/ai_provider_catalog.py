"""Direct-connect AI provider presets used by Settings.

Only endpoints that ArgusAI can call directly through OpenAI Chat Completions
or Anthropic Messages are included. OAuth, Bedrock, Gemini-native,
OpenAI Responses-only providers and protocol-conversion proxies are outside
this catalog.

Provider ids are persisted in ``settings.json``. Keep existing ids stable.
Credentials never belong in this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ProviderProtocol = Literal["openai", "anthropic"]
ModelListStrategy = Literal["api", "preset", "manual"]

CATEGORY_OFFICIAL = "official"
CATEGORY_CN_OFFICIAL = "cn_official"
CATEGORY_AGGREGATOR = "aggregator"
CATEGORY_THIRD_PARTY = "third_party"
CATEGORY_CUSTOM = "custom"

CATEGORY_ORDER = [
    CATEGORY_OFFICIAL,
    CATEGORY_CN_OFFICIAL,
    CATEGORY_AGGREGATOR,
    CATEGORY_THIRD_PARTY,
    CATEGORY_CUSTOM,
]

CATEGORY_LABELS: dict[str, str] = {
    CATEGORY_OFFICIAL: "国际官方",
    CATEGORY_CN_OFFICIAL: "国内官方",
    CATEGORY_AGGREGATOR: "聚合平台",
    CATEGORY_THIRD_PARTY: "第三方服务",
    CATEGORY_CUSTOM: "自定义",
}

CUSTOM_PROVIDER_ID = "custom"

_OPENAI = "openai"
_ANTHROPIC = "anthropic"

_GPT_CONTEXT = 400_000
_GPT_OUTPUT = 128_000
_CLAUDE_CONTEXT = 1_000_000
_CLAUDE_OUTPUT = 64_000
_GLM_CONTEXT = 1_000_000
_GLM_OUTPUT = 131_072


@dataclass(frozen=True)
class ProviderAuth:
    """How a provider expects its API key."""

    scheme: Literal["bearer", "x-api-key"]
    header: str


@dataclass(frozen=True)
class ProviderPreset:
    """Immutable defaults for one directly callable provider."""

    id: str
    name: str
    category: str
    region: str
    protocol: ProviderProtocol
    base_url: str
    auth: ProviderAuth
    website_url: str
    api_key_url: str
    default_model: str
    suggested_models: tuple[str, ...] = ()
    model_list_strategy: ModelListStrategy = "preset"
    context_window: int = 131_072
    max_tokens: int = 16_384
    modality_hints: dict[str, bool] = field(default_factory=dict)
    aliases: tuple[str, ...] = ()
    featured: bool = False
    requires_base_url_edit: bool = False
    note: str = ""

    @property
    def models_supported(self) -> bool:
        """Compatibility property for older callers."""

        return self.model_list_strategy == "api"

    @property
    def console_url(self) -> str:
        """Compatibility property for the existing settings bridge."""

        return self.api_key_url or self.website_url


PROVIDER_PRESETS: dict[str, ProviderPreset] = {}


def _register(
    provider_id: str,
    name: str,
    category: str,
    region: str,
    protocol: ProviderProtocol,
    base_url: str,
    default_model: str,
    *suggested_models: str,
    website_url: str,
    api_key_url: str = "",
    model_list_strategy: ModelListStrategy = "preset",
    context_window: int = 131_072,
    max_tokens: int = 16_384,
    image_in: bool = False,
    video_in: bool = False,
    audio_in: bool = False,
    aliases: tuple[str, ...] = (),
    featured: bool = False,
    requires_base_url_edit: bool = False,
    note: str = "",
) -> None:
    if provider_id in PROVIDER_PRESETS:
        raise ValueError(f"duplicate AI provider id: {provider_id}")
    models = tuple(dict.fromkeys(model for model in (default_model, *suggested_models) if model))
    auth = (
        ProviderAuth(scheme="x-api-key", header="x-api-key")
        if protocol == _ANTHROPIC
        else ProviderAuth(scheme="bearer", header="Authorization")
    )
    PROVIDER_PRESETS[provider_id] = ProviderPreset(
        id=provider_id,
        name=name,
        category=category,
        region=region,
        protocol=protocol,
        base_url=base_url,
        auth=auth,
        website_url=website_url,
        api_key_url=api_key_url or website_url,
        default_model=default_model,
        suggested_models=models,
        model_list_strategy=model_list_strategy,
        context_window=context_window,
        max_tokens=max_tokens,
        modality_hints={
            key: enabled
            for key, enabled in {
                "image_in": image_in,
                "video_in": video_in,
                "audio_in": audio_in,
            }.items()
            if enabled
        },
        aliases=aliases,
        featured=featured,
        requires_base_url_edit=requires_base_url_edit,
        note=note,
    )


# International official APIs.
_register(
    "anthropic", "Anthropic", CATEGORY_OFFICIAL, "global", _ANTHROPIC,
    "https://api.anthropic.com/v1", "claude-sonnet-5",
    "claude-opus-4-8", "claude-haiku-4-5-20251001",
    website_url="https://www.anthropic.com/api",
    api_key_url="https://console.anthropic.com/settings/keys",
    context_window=_CLAUDE_CONTEXT, max_tokens=_CLAUDE_OUTPUT,
    image_in=True, featured=True, aliases=("Claude", "Claude API"),
)
_register(
    "openai", "OpenAI", CATEGORY_OFFICIAL, "global", _OPENAI,
    "https://api.openai.com/v1", "gpt-5.5", "gpt-5.5-pro",
    website_url="https://platform.openai.com/",
    api_key_url="https://platform.openai.com/api-keys",
    model_list_strategy="api", context_window=1_000_000, max_tokens=128_000,
    image_in=True, audio_in=True, featured=True, aliases=("GPT", "ChatGPT"),
)
_register(
    "google-openai", "Google Gemini (OpenAI 兼容)", CATEGORY_OFFICIAL, "global", _OPENAI,
    "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    website_url="https://ai.google.dev/gemini-api/docs/openai",
    api_key_url="https://aistudio.google.com/apikey",
    context_window=1_048_576, max_tokens=65_536,
    image_in=True, video_in=True, audio_in=True, featured=True,
    aliases=("Gemini", "Google AI Studio"),
)

# China-region official and first-party coding plans.
_register(
    "volcengine-agentplan", "火山引擎 Agent Plan", CATEGORY_CN_OFFICIAL, "cn", _OPENAI,
    "https://ark.cn-beijing.volces.com/api/coding/v3", "ark-code-latest",
    website_url="https://www.volcengine.com/product/ark",
    api_key_url="https://console.volcengine.com/ark",
    context_window=256_000, max_tokens=65_536, aliases=("火山方舟", "Ark Coding"),
)
_register(
    "byteplus-agentplan", "BytePlus ModelArk Coding", CATEGORY_OFFICIAL, "apac", _OPENAI,
    "https://ark.ap-southeast.bytepluses.com/api/coding/v3", "ark-code-latest",
    website_url="https://www.byteplus.com/en/product/modelark",
    api_key_url="https://console.byteplus.com/ark",
    context_window=256_000, max_tokens=65_536, aliases=("BytePlus", "ModelArk"),
)
_register(
    "doubao-seed", "豆包 Seed", CATEGORY_CN_OFFICIAL, "cn", _OPENAI,
    "https://ark.cn-beijing.volces.com/api/v3", "doubao-seed-2-1-pro-260628",
    website_url="https://www.volcengine.com/product/doubao",
    api_key_url="https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey",
    model_list_strategy="api", context_window=256_000, max_tokens=65_536,
    image_in=True, aliases=("豆包", "Doubao", "火山方舟"),
)
_register(
    "deepseek", "DeepSeek 深度求索", CATEGORY_CN_OFFICIAL, "cn", _OPENAI,
    "https://api.deepseek.com/v1", "deepseek-flash", "deepseek-v4-pro",
    website_url="https://platform.deepseek.com/",
    api_key_url="https://platform.deepseek.com/api_keys",
    model_list_strategy="api", context_window=1_000_000, max_tokens=393_216,
    image_in=True, featured=True, aliases=("深度求索",),
)
_register(
    "zhipu-glm", "智谱 GLM（中国）", CATEGORY_CN_OFFICIAL, "cn", _OPENAI,
    "https://open.bigmodel.cn/api/paas/v4", "glm-5.2", "glm-5.2[1m]",
    website_url="https://open.bigmodel.cn/",
    api_key_url="https://open.bigmodel.cn/usercenter/apikeys",
    context_window=_GLM_CONTEXT, max_tokens=_GLM_OUTPUT,
    image_in=True, featured=True, aliases=("智谱", "BigModel", "Z.AI"),
)
_register(
    "zhipu-glm-global", "Z.AI GLM（国际）", CATEGORY_OFFICIAL, "global", _OPENAI,
    "https://api.z.ai/api/paas/v4", "glm-5.2", "glm-5.2[1m]",
    website_url="https://z.ai/",
    api_key_url="https://z.ai/manage-apikey/apikey-list",
    context_window=_GLM_CONTEXT, max_tokens=_GLM_OUTPUT,
    image_in=True, aliases=("Zhipu GLM en", "ZAI"),
)
_register(
    "ali-bailian", "阿里云百炼", CATEGORY_CN_OFFICIAL, "cn", _OPENAI,
    "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3.7-plus",
    "qwen3.8-max-preview",
    website_url="https://bailian.console.aliyun.com/",
    api_key_url="https://bailian.console.aliyun.com/#/api-key",
    model_list_strategy="api", context_window=1_000_000, max_tokens=65_536,
    image_in=True, video_in=True, featured=True,
    aliases=("Bailian", "DashScope", "通义千问", "Qwen"),
)
_register(
    "kimi", "Kimi 开放平台", CATEGORY_CN_OFFICIAL, "cn", _OPENAI,
    "https://api.moonshot.cn/v1", "kimi-k3",
    "kimi-k2.7-code", "kimi-k2.7-code-highspeed",
    website_url="https://platform.kimi.com/",
    api_key_url="https://platform.kimi.com/console/api-keys",
    model_list_strategy="api", context_window=1_000_000, max_tokens=131_072,
    image_in=True, video_in=True, featured=True, aliases=("Moonshot", "月之暗面"),
)
_register(
    "kimi-coding", "Kimi For Coding", CATEGORY_CN_OFFICIAL, "cn", _ANTHROPIC,
    "https://api.kimi.com/coding/v1", "kimi-for-coding",
    website_url="https://www.kimi.com/code/",
    api_key_url="https://platform.kimi.com/console/api-keys",
    context_window=262_144, max_tokens=65_536, aliases=("Kimi Code",),
)
_register(
    "stepfun", "阶跃星辰 Step Plan（中国）", CATEGORY_CN_OFFICIAL, "cn", _OPENAI,
    "https://api.stepfun.com/step_plan/v1", "step-3.5-flash-2603", "step-3.5-flash",
    website_url="https://platform.stepfun.com/step-plan",
    api_key_url="https://platform.stepfun.com/interface-key",
    context_window=262_144, max_tokens=131_072, aliases=("阶跃星辰", "StepFun"),
)
_register(
    "stepfun-global", "StepFun Step Plan（国际）", CATEGORY_OFFICIAL, "global", _OPENAI,
    "https://api.stepfun.ai/step_plan/v1", "step-3.5-flash-2603", "step-3.5-flash",
    website_url="https://platform.stepfun.ai/step-plan",
    api_key_url="https://platform.stepfun.ai/interface-key",
    context_window=262_144, max_tokens=131_072, aliases=("StepFun en",),
)
_register(
    "kat-coder", "KAT-Coder", CATEGORY_CN_OFFICIAL, "cn", _OPENAI,
    "https://vanchin.streamlake.ai/api/gateway/v1/endpoints/{ENDPOINT_ID}/openai",
    "KAT-Coder-Pro",
    website_url="https://console.streamlake.ai/",
    api_key_url="https://console.streamlake.ai/console/api-key",
    context_window=256_000, max_tokens=65_536, requires_base_url_edit=True,
    aliases=("StreamLake", "Vanchin"),
    note="选择后请把 URL 中的 {ENDPOINT_ID} 替换为控制台创建的推理端点 ID。",
)
_register(
    "longcat", "LongCat", CATEGORY_CN_OFFICIAL, "cn", _OPENAI,
    "https://api.longcat.chat/v1", "LongCat-Flash-Chat",
    website_url="https://longcat.chat/platform",
    api_key_url="https://longcat.chat/platform/api_keys",
    model_list_strategy="api", context_window=131_072, max_tokens=32_768,
    aliases=("美团 LongCat",),
)
_register(
    "minimax", "MiniMax（中国）", CATEGORY_CN_OFFICIAL, "cn", _OPENAI,
    "https://api.minimaxi.com/v1", "MiniMax-M2.7",
    website_url="https://platform.minimaxi.com/",
    api_key_url="https://platform.minimaxi.com/user-center/basic-information/interface-key",
    model_list_strategy="api", context_window=204_800, max_tokens=131_072,
    aliases=("海螺", "MiniMax CN"),
)
_register(
    "minimax-global", "MiniMax（国际）", CATEGORY_OFFICIAL, "global", _OPENAI,
    "https://api.minimax.io/v1", "MiniMax-M2.7",
    website_url="https://platform.minimax.io/",
    api_key_url="https://platform.minimax.io/user-center/basic-information/interface-key",
    model_list_strategy="api", context_window=204_800, max_tokens=131_072,
    aliases=("MiniMax en",),
)
_register(
    "bailing", "BaiLing 百灵", CATEGORY_CN_OFFICIAL, "cn", _OPENAI,
    "https://api.tbox.cn/v1", "Ling-2.5-1T",
    website_url="https://alipaytbox.yuque.com/sxs0ba/ling/get_started",
    context_window=131_072, max_tokens=32_768, aliases=("百灵", "TBox"),
)
_register(
    "xiaomi-mimo", "小米 MiMo", CATEGORY_CN_OFFICIAL, "cn", _OPENAI,
    "https://api.xiaomimimo.com/v1", "mimo-v2.5-pro", "mimo-v2.5",
    website_url="https://platform.xiaomimimo.com/",
    api_key_url="https://platform.xiaomimimo.com/#/console/api-keys",
    model_list_strategy="api", context_window=1_048_576, max_tokens=131_072,
    aliases=("Xiaomi MiMo", "小米大模型"),
)
_register(
    "xiaomi-mimo-token-plan-cn", "小米 MiMo Token Plan", CATEGORY_CN_OFFICIAL, "cn", _OPENAI,
    "https://token-plan-cn.xiaomimimo.com/v1", "mimo-v2.5-pro", "mimo-v2.5",
    website_url="https://platform.xiaomimimo.com/#/token-plan",
    api_key_url="https://platform.xiaomimimo.com/#/console/plan-manage",
    context_window=1_048_576, max_tokens=131_072, aliases=("MiMo 套餐",),
)
_register(
    "baidu-qianfan", "百度千帆", CATEGORY_CN_OFFICIAL, "cn", _OPENAI,
    "https://qianfan.baidubce.com/v2", "ernie-5.1",
    "ernie-5.0-thinking-latest",
    website_url="https://cloud.baidu.com/product/wenxinworkshop",
    api_key_url="https://console.bce.baidu.com/qianfan/ais/console/applicationConsole/application",
    model_list_strategy="api", context_window=128_000, max_tokens=65_536,
    image_in=True, aliases=("Qianfan", "文心一言", "ERNIE"),
)
_register(
    "siliconflow-cn", "硅基流动（中国）", CATEGORY_CN_OFFICIAL, "cn", _OPENAI,
    "https://api.siliconflow.cn/v1", "deepseek-ai/DeepSeek-V4-Pro",
    "deepseek-ai/DeepSeek-V4-Flash",
    website_url="https://siliconflow.cn/",
    api_key_url="https://cloud.siliconflow.cn/account/ak",
    model_list_strategy="api", context_window=1_048_576, max_tokens=393_216,
    image_in=True, aliases=("SiliconFlow", "硅基流动"),
)
_register(
    "siliconflow-global", "SiliconFlow（国际）", CATEGORY_OFFICIAL, "global", _OPENAI,
    "https://api.siliconflow.com/v1", "deepseek-ai/DeepSeek-V4-Pro",
    "deepseek-ai/DeepSeek-V4-Flash",
    website_url="https://siliconflow.com/",
    api_key_url="https://cloud.siliconflow.com/account/ak",
    model_list_strategy="api", context_window=1_048_576, max_tokens=393_216,
    image_in=True, aliases=("SiliconFlow global",),
)

# Aggregators. URLs intentionally omit referral parameters.
_register(
    "shengsuanyun", "胜算云 Shengsuanyun", CATEGORY_AGGREGATOR, "cn", _ANTHROPIC,
    "https://router.shengsuanyun.com/api/v1", "anthropic/claude-sonnet-5",
    "anthropic/claude-opus-4.8",
    website_url="https://www.shengsuanyun.com/",
    context_window=_CLAUDE_CONTEXT, max_tokens=_CLAUDE_OUTPUT, image_in=True,
)
_register(
    "qiniu", "七牛云 AI", CATEGORY_AGGREGATOR, "cn", _OPENAI,
    "https://api.qnaigc.com/v1", "gpt-5.5",
    website_url="https://www.qiniu.com/products/ai",
    context_window=_GPT_CONTEXT, max_tokens=_GPT_OUTPUT, image_in=True,
    aliases=("Qiniu", "七牛"),
)
_register(
    "fennoai", "FennoAI", CATEGORY_AGGREGATOR, "global", _OPENAI,
    "https://api.fenno.ai/v1", "gpt-5.5",
    website_url="https://api.fenno.ai/",
    context_window=_GPT_CONTEXT, max_tokens=_GPT_OUTPUT, image_in=True,
)
_register(
    "zetaapi", "ZetaAPI", CATEGORY_AGGREGATOR, "global", _OPENAI,
    "https://api.zetaapi.ai/v1", "gpt-5.5",
    website_url="https://zetaapi.ai/",
    context_window=_GPT_CONTEXT, max_tokens=_GPT_OUTPUT, image_in=True,
)
_register(
    "teamorouter", "TeamoRouter", CATEGORY_AGGREGATOR, "global", _OPENAI,
    "https://api.teamorouter.com/v1", "gpt-5.5",
    website_url="https://teamorouter.com/",
    context_window=_GPT_CONTEXT, max_tokens=_GPT_OUTPUT, image_in=True,
)
_register(
    "amux", "Amux", CATEGORY_AGGREGATOR, "global", _OPENAI,
    "https://api.amux.ai/v1", "gpt-5.5",
    website_url="https://amux.ai/",
    context_window=_GPT_CONTEXT, max_tokens=_GPT_OUTPUT, image_in=True,
)
_register(
    "code0", "Code0", CATEGORY_AGGREGATOR, "global", _OPENAI,
    "https://code0.ai/v1", "gpt-5.5",
    website_url="https://code0.ai/",
    context_window=_GPT_CONTEXT, max_tokens=_GPT_OUTPUT, image_in=True,
)
_register(
    "nekocode", "NekoCode", CATEGORY_AGGREGATOR, "global", _OPENAI,
    "https://nekocode.ai/v1", "gpt-5.5",
    website_url="https://nekocode.ai/",
    context_window=_GPT_CONTEXT, max_tokens=_GPT_OUTPUT, image_in=True,
)
_register(
    "ccsub", "CCSub", CATEGORY_AGGREGATOR, "global", _OPENAI,
    "https://www.ccsub.net/v1", "gpt-5.5",
    website_url="https://www.ccsub.net/",
    context_window=_GPT_CONTEXT, max_tokens=_GPT_OUTPUT, image_in=True,
)
_register(
    "subrouter", "SubRouter", CATEGORY_AGGREGATOR, "global", _OPENAI,
    "https://subrouter.ai/v1", "gpt-5.5",
    website_url="https://subrouter.ai/",
    context_window=_GPT_CONTEXT, max_tokens=_GPT_OUTPUT, image_in=True,
)
_register(
    "unity2", "Unity2.ai", CATEGORY_AGGREGATOR, "global", _OPENAI,
    "https://api.unity2.ai/v1", "gpt-5.5",
    website_url="https://unity2.ai/",
    context_window=_GPT_CONTEXT, max_tokens=_GPT_OUTPUT, image_in=True,
)
_register(
    "modelscope", "ModelScope 魔搭", CATEGORY_AGGREGATOR, "cn", _OPENAI,
    "https://api-inference.modelscope.cn/v1", "ZhipuAI/GLM-5.2",
    website_url="https://modelscope.cn/",
    api_key_url="https://modelscope.cn/my/myaccesstoken",
    model_list_strategy="api", context_window=_GLM_CONTEXT, max_tokens=_GLM_OUTPUT,
    aliases=("魔搭",),
)
_register(
    "opencode-go", "OpenCode Go", CATEGORY_AGGREGATOR, "global", _OPENAI,
    "https://opencode.ai/zen/go/v1", "glm-5.2", "kimi-k2.7-code",
    "deepseek-v4-pro", "deepseek-v4-flash", "mimo-v2.5-pro",
    website_url="https://opencode.ai/go",
    context_window=262_144, max_tokens=131_072, image_in=True,
)
_register(
    "aihubmix", "AiHubMix", CATEGORY_AGGREGATOR, "global", _ANTHROPIC,
    "https://aihubmix.com/v1", "claude-sonnet-5", "claude-opus-4-8",
    website_url="https://aihubmix.com/",
    api_key_url="https://aihubmix.com/token",
    context_window=_CLAUDE_CONTEXT, max_tokens=_CLAUDE_OUTPUT, image_in=True,
)
_register(
    "cherryin", "CherryIN", CATEGORY_AGGREGATOR, "global", _ANTHROPIC,
    "https://open.cherryin.net/v1", "anthropic/claude-sonnet-5",
    "anthropic/claude-opus-4-8",
    website_url="https://open.cherryin.ai/",
    api_key_url="https://open.cherryin.ai/console/token",
    context_window=_CLAUDE_CONTEXT, max_tokens=_CLAUDE_OUTPUT, image_in=True,
)
_register(
    "dmxapi", "DMXAPI", CATEGORY_AGGREGATOR, "cn", _ANTHROPIC,
    "https://www.dmxapi.cn/v1", "claude-sonnet-5", "claude-opus-4-8",
    website_url="https://www.dmxapi.cn/",
    context_window=_CLAUDE_CONTEXT, max_tokens=_CLAUDE_OUTPUT, image_in=True,
)
_register(
    "openrouter", "OpenRouter", CATEGORY_AGGREGATOR, "global", _OPENAI,
    "https://openrouter.ai/api/v1", "anthropic/claude-sonnet-5",
    "openai/gpt-5.5", "google/gemini-3.6-flash", "z-ai/glm-5.2",
    website_url="https://openrouter.ai/",
    api_key_url="https://openrouter.ai/settings/keys",
    model_list_strategy="api", context_window=_CLAUDE_CONTEXT,
    max_tokens=_CLAUDE_OUTPUT, image_in=True, featured=True,
)
_register(
    "therouter", "TheRouter", CATEGORY_AGGREGATOR, "global", _OPENAI,
    "https://api.therouter.ai/v1", "anthropic/claude-sonnet-5",
    "openai/gpt-5.5", "google/gemini-3.6-flash", "z-ai/glm-5.2",
    website_url="https://therouter.ai/",
    api_key_url="https://dashboard.therouter.ai/",
    context_window=_GPT_CONTEXT, max_tokens=_GPT_OUTPUT, image_in=True,
)
_register(
    "novita", "Novita AI", CATEGORY_AGGREGATOR, "global", _OPENAI,
    "https://api.novita.ai/openai", "zai-org/glm-5.2",
    website_url="https://novita.ai/",
    api_key_url="https://novita.ai/settings/key-management",
    context_window=_GLM_CONTEXT, max_tokens=_GLM_OUTPUT,
)
_register(
    "nvidia-nim", "NVIDIA NIM", CATEGORY_AGGREGATOR, "global", _OPENAI,
    "https://integrate.api.nvidia.com/v1", "moonshotai/kimi-k2.6",
    website_url="https://build.nvidia.com/",
    api_key_url="https://build.nvidia.com/settings/api-keys",
    model_list_strategy="api", context_window=262_144, max_tokens=65_536,
    image_in=True, aliases=("Nvidia", "Build NVIDIA"),
)
_register(
    "pipellm", "PIPELLM", CATEGORY_AGGREGATOR, "global", _ANTHROPIC,
    "https://cc-api.pipellm.ai", "claude-sonnet-5", "claude-opus-4-8",
    "claude-haiku-4-5-20251001",
    website_url="https://code.pipellm.ai/",
    context_window=_CLAUDE_CONTEXT, max_tokens=_CLAUDE_OUTPUT, image_in=True,
)
_register(
    "runapi", "RunAPI", CATEGORY_AGGREGATOR, "global", _ANTHROPIC,
    "https://runapi.co", "claude-sonnet-5", "claude-opus-4-8", "claude-haiku-4-5",
    website_url="https://runapi.co/",
    context_window=_CLAUDE_CONTEXT, max_tokens=_CLAUDE_OUTPUT, image_in=True,
)
_register(
    "atlascloud", "AtlasCloud", CATEGORY_AGGREGATOR, "global", _OPENAI,
    "https://api.atlascloud.ai/v1", "zai-org/glm-5.2",
    website_url="https://www.atlascloud.ai/",
    api_key_url="https://www.atlascloud.ai/console/coding-plan",
    context_window=_GLM_CONTEXT, max_tokens=_GLM_OUTPUT,
)

# Smaller direct third-party services.
_register(
    "packycode", "PackyCode", CATEGORY_THIRD_PARTY, "global", _ANTHROPIC,
    "https://www.packyapi.com/v1", "claude-sonnet-5", "claude-opus-4-8",
    website_url="https://www.packyapi.com/",
    context_window=_CLAUDE_CONTEXT, max_tokens=_CLAUDE_OUTPUT, image_in=True,
)
_register(
    "apikey-fun", "APIKEY.FUN", CATEGORY_THIRD_PARTY, "global", _ANTHROPIC,
    "https://api.apikey.fun/v1", "claude-sonnet-5", "claude-opus-4-8",
    "claude-haiku-4-5",
    website_url="https://apikey.fun/",
    context_window=_CLAUDE_CONTEXT, max_tokens=_CLAUDE_OUTPUT, image_in=True,
)
_register(
    "apinebula", "APINebula", CATEGORY_THIRD_PARTY, "global", _OPENAI,
    "https://apinebula.com/v1", "gpt-5.5",
    website_url="https://apinebula.com/",
    context_window=_GPT_CONTEXT, max_tokens=_GPT_OUTPUT, image_in=True,
)
_register(
    "cubence", "Cubence", CATEGORY_THIRD_PARTY, "global", _ANTHROPIC,
    "https://api.cubence.com/v1", "claude-sonnet-5", "claude-opus-4-8",
    website_url="https://cubence.com/",
    context_window=_CLAUDE_CONTEXT, max_tokens=_CLAUDE_OUTPUT, image_in=True,
)
_register(
    "aigocode", "AIGoCode", CATEGORY_THIRD_PARTY, "global", _ANTHROPIC,
    "https://api.aigocode.com", "claude-sonnet-5", "claude-opus-4-8",
    website_url="https://aigocode.com/",
    context_window=_CLAUDE_CONTEXT, max_tokens=_CLAUDE_OUTPUT, image_in=True,
)
_register(
    "aicodemirror", "AICodeMirror", CATEGORY_THIRD_PARTY, "global", _ANTHROPIC,
    "https://api.aicodemirror.com/api/claudecode", "claude-sonnet-5",
    "claude-opus-4-8",
    website_url="https://www.aicodemirror.com/",
    context_window=_CLAUDE_CONTEXT, max_tokens=_CLAUDE_OUTPUT, image_in=True,
)
_register(
    "claudecn", "ClaudeCN", CATEGORY_THIRD_PARTY, "cn", _ANTHROPIC,
    "https://claudecn.top", "claude-sonnet-5", "claude-opus-4-8", "claude-haiku-4-5",
    website_url="https://claudecn.top/",
    context_window=_CLAUDE_CONTEXT, max_tokens=_CLAUDE_OUTPUT, image_in=True,
)
_register(
    "crazyrouter", "CrazyRouter", CATEGORY_THIRD_PARTY, "cn", _ANTHROPIC,
    "https://cn.crazyrouter.com", "claude-sonnet-5", "claude-opus-4-8",
    website_url="https://www.crazyrouter.com/",
    context_window=_CLAUDE_CONTEXT, max_tokens=_CLAUDE_OUTPUT, image_in=True,
)
_register(
    "sssaicode", "SSSAiCode", CATEGORY_THIRD_PARTY, "apac", _ANTHROPIC,
    "https://node-hk.sssaicodeapi.com/api/v1", "claude-sonnet-5", "claude-opus-4-8",
    website_url="https://sssaicodeapi.com/",
    context_window=_CLAUDE_CONTEXT, max_tokens=_CLAUDE_OUTPUT, image_in=True,
)
_register(
    "micu", "Micu", CATEGORY_THIRD_PARTY, "global", _ANTHROPIC,
    "https://www.micuapi.ai/v1", "claude-sonnet-5", "claude-opus-4-8",
    website_url="https://www.micuapi.ai/",
    context_window=_CLAUDE_CONTEXT, max_tokens=_CLAUDE_OUTPUT, image_in=True,
)
_register(
    "etok", "ETok.ai", CATEGORY_THIRD_PARTY, "global", _ANTHROPIC,
    "https://api.etok.ai/v1", "claude-sonnet-5", "claude-opus-4-8",
    website_url="https://etok.ai/",
    context_window=_CLAUDE_CONTEXT, max_tokens=_CLAUDE_OUTPUT, image_in=True,
)


def list_provider_presets() -> list[ProviderPreset]:
    """Return deterministic category/name ordering for the settings UI."""

    return sorted(
        PROVIDER_PRESETS.values(),
        key=lambda preset: (
            CATEGORY_ORDER.index(preset.category),
            not preset.featured,
            preset.name.casefold(),
        ),
    )


def get_provider_preset(provider_id: str) -> ProviderPreset | None:
    if not provider_id or provider_id == CUSTOM_PROVIDER_ID:
        return None
    return PROVIDER_PRESETS.get(provider_id)


def find_provider_by_url(base_url: str) -> ProviderPreset | None:
    """Match a unique exact URL for legacy settings migration."""

    if not base_url:
        return None
    normalized = base_url.rstrip("/").casefold()
    matches = [
        preset
        for preset in PROVIDER_PRESETS.values()
        if preset.base_url
        and not preset.requires_base_url_edit
        and preset.base_url.rstrip("/").casefold() == normalized
    ]
    return matches[0] if len(matches) == 1 else None


def resolve_provider_id_for(endpoint: object) -> str:
    """Resolve an endpoint's stable preset id without mutating it."""

    if isinstance(endpoint, dict):
        explicit = str(endpoint.get("provider_id") or "").strip()
        base_url = str(endpoint.get("base_url") or "")
    else:
        explicit = str(getattr(endpoint, "provider_id", "") or "").strip()
        base_url = str(getattr(endpoint, "base_url", "") or "")
    if explicit and explicit != CUSTOM_PROVIDER_ID:
        return explicit if get_provider_preset(explicit) else CUSTOM_PROVIDER_ID
    preset = find_provider_by_url(base_url)
    return preset.id if preset else CUSTOM_PROVIDER_ID


def is_custom_provider(provider_id: str) -> bool:
    return provider_id in (None, "", CUSTOM_PROVIDER_ID)
