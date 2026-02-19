# LLM 配置和调用模块
# 支持多种 LLM 服务提供商

import os
import uuid
from pathlib import Path
from typing import Optional, Dict, Any, List

from collie_package.config_loader import load_app_settings

BASE_DIR = Path(__file__).parent.resolve()
ENV_FILE_PRIORITY = [
    BASE_DIR / '.llm.env',
    BASE_DIR / '.env.local',
    BASE_DIR / '.env',
    BASE_DIR.parent / '.env',
]
SUPPORTED_PROVIDERS = {'openai', 'azure', 'mify'}


def _unquote_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _load_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists() or not path.is_file():
        return values

    try:
        for raw_line in path.read_text(encoding='utf-8').splitlines():
            line = raw_line.strip()
            if not line or line.startswith('#'):
                continue

            if line.startswith('export '):
                line = line[len('export '):].strip()
            if '=' not in line:
                continue

            key, value = line.split('=', 1)
            key = key.strip()
            if not key:
                continue
            parsed_value = _unquote_env_value(value)
            if parsed_value == '':
                continue
            values[key] = parsed_value
    except Exception as e:
        print(f'⚠️ 读取环境变量文件失败 {path}: {e}')

    return values


def _build_llm_env_values() -> Dict[str, str]:
    # 合并顺序：低优先级 -> 高优先级，后写覆盖前写
    values: Dict[str, str] = {}
    for path in reversed(ENV_FILE_PRIORITY):
        values.update(_load_env_file(path))

    # 进程环境变量优先于 .env/.env.local，但低于 web_app/.llm.env
    values.update(os.environ)
    values.update(_load_env_file(ENV_FILE_PRIORITY[0]))
    return values


def _parse_int(value: str, default: int) -> int:
    try:
        parsed = int(str(value).strip())
        return parsed if parsed > 0 else default
    except Exception:
        return default


def _parse_bool_or_none(value: str) -> Optional[bool]:
    clean = str(value).strip().lower()
    if clean in {'1', 'true', 'yes', 'on'}:
        return True
    if clean in {'0', 'false', 'no', 'off'}:
        return False
    return None


class LLMConfig:
    """LLM 配置类"""
    def __init__(self):
        app_settings = load_app_settings()
        llm_settings = app_settings.get('llm', {})
        fallback_order = llm_settings.get('provider_fallback_order') or ['mify', 'openai', 'azure']
        if isinstance(fallback_order, str):
            fallback_order = [x.strip() for x in fallback_order.split(',') if x.strip()]
        self.provider_fallback_order = list(fallback_order)

        env = _build_llm_env_values()

        def _conf(key: str, default):
            return llm_settings.get(key, default)

        # OpenAI 配置
        openai_cfg = llm_settings.get('openai', {})
        self.openai_api_key = env.get('OPENAI_API_KEY', '') or str(openai_cfg.get('api_key', ''))
        self.openai_base_url = env.get('OPENAI_BASE_URL', '') or str(
            openai_cfg.get('base_url', 'https://api.openai.com/v1')
        )
        self.openai_model = env.get('OPENAI_MODEL', '') or str(openai_cfg.get('model', 'gpt-4o-mini'))

        # Azure OpenAI 配置
        azure_cfg = llm_settings.get('azure', {})
        self.azure_api_key = env.get('AZURE_OPENAI_API_KEY', '') or str(azure_cfg.get('api_key', ''))
        self.azure_endpoint = env.get('AZURE_OPENAI_ENDPOINT', '') or str(
            azure_cfg.get('endpoint', '')
        )
        self.azure_model = env.get('AZURE_OPENAI_MODEL', '') or str(azure_cfg.get('model', 'gpt-4'))

        # Mify 网关配置
        mify_cfg = llm_settings.get('mify', {})
        self.mify_api_key = env.get('MIFY_API_KEY', '') or str(mify_cfg.get('api_key', ''))
        self.mify_base_url = env.get('MIFY_BASE_URL', '') or str(
            mify_cfg.get('base_url', 'https://mify.mioffice.cn/gateway')
        )
        self.mify_model = env.get('MIFY_MODEL', '') or str(mify_cfg.get('model', 'mimo-v2-flash'))
        self.mify_provider_id = env.get('MIFY_PROVIDER_ID', '') or str(
            mify_cfg.get('provider_id', 'xiaomi')
        )
        timeout_cfg = mify_cfg.get('timeout_seconds', 60)
        self.mify_timeout_seconds = _parse_int(env.get('MIFY_TIMEOUT_SECONDS', ''), int(timeout_cfg or 60))
        self.mify_user_id = env.get('MIFY_USER_ID', '').strip() or str(mify_cfg.get('user_id', '')).strip()
        self.mify_conversation_id = env.get('MIFY_CONVERSATION_ID', '').strip() or str(
            mify_cfg.get('conversation_id', '')
        ).strip()
        self.mify_logging = env.get('MIFY_LOGGING', '').strip().lower() or str(
            mify_cfg.get('logging', '')
        ).strip().lower()
        reasoning_env = env.get('MIFY_REASONING_CONTENT_ENABLED', '')
        reasoning_cfg = mify_cfg.get('reasoning_content_enabled')
        self.mify_reasoning_content_enabled = _parse_bool_or_none(reasoning_env)
        if self.mify_reasoning_content_enabled is None and reasoning_cfg is not None:
            self.mify_reasoning_content_enabled = _parse_bool_or_none(str(reasoning_cfg))

        # 默认提供商
        self.default_provider = (
            env.get('LLM_PROVIDER', '') or str(_conf('default_provider', 'auto'))
        ).lower().strip()

    def is_configured(self) -> bool:
        """检查是否配置了至少一个可用 LLM 服务"""
        return bool(self.available_providers())

    def has_provider_key(self, provider: str) -> bool:
        if provider == 'openai':
            return bool(self.openai_api_key)
        if provider == 'azure':
            return bool(self.azure_api_key and self.azure_endpoint)
        if provider == 'mify':
            return bool(self.mify_api_key)
        return False

    def available_providers(self) -> List[str]:
        providers: List[str] = []
        for provider in self.provider_fallback_order:
            if self.has_provider_key(provider):
                providers.append(provider)
        return providers

    def resolve_provider(self, requested_provider: str = '') -> str:
        provider = (requested_provider or self.default_provider or 'auto').lower().strip()
        if provider in SUPPORTED_PROVIDERS and self.has_provider_key(provider):
            return provider

        for fallback_provider in self.provider_fallback_order:
            if self.has_provider_key(fallback_provider):
                return fallback_provider

        return provider


class LLMClient:
    """LLM 客户端"""
    def __init__(self, config: Optional[LLMConfig] = None):
        self.config = config or LLMConfig()
        self.provider = self.config.default_provider

    def reload_config(self) -> None:
        """每次调用前刷新配置，避免服务启动后环境变量变更不生效。"""
        current_provider = (self.provider or '').lower().strip()
        self.config = LLMConfig()
        if current_provider in {'', 'auto'}:
            self.provider = self.config.default_provider
        else:
            self.provider = current_provider
        
    def compare_reports(self, report1: str, report2: str, 
                       report1_meta: Dict[str, Any], 
                       report2_meta: Dict[str, Any],
                       meminfo1: str = "",
                       meminfo2: str = "",
                       txt1: str = "",
                       txt2: str = "") -> str:
        """
        对比两份 bugreport 分析报告
        
        Args:
            report1: 第一份报告内容 (HTML)
            report2: 第二份报告内容 (HTML)
            report1_meta: 第一份报告的元数据
            report2_meta: 第二份报告的元数据
            meminfo1: 第一份报告的内存摘要
            meminfo2: 第二份报告的内存摘要
            txt1: 第一份报告的文本内容（补充）
            txt2: 第二份报告的文本内容（补充）
            txt2: 第二份报告的文本内容（补充）
            
        Returns:
            LLM 生成的对比分析结果
        """
        prompt = self._build_comparison_prompt(report1, report2, 
                                               report1_meta, report2_meta,
                                               meminfo1, meminfo2,
                                               txt1, txt2)
        return self._call_llm(prompt)

    def interpret_report(
        self,
        report_html: str,
        report_meta: Dict[str, Any],
        meminfo: str = '',
        txt: str = '',
        bugreport_context: str = '',
    ) -> str:
        """
        对单份报告进行 AI 智能解读（总结 + 评估）。
        """
        prompt = self._build_interpret_prompt(
            report_html=report_html,
            meta=report_meta,
            meminfo=meminfo,
            txt=txt,
            bugreport_context=bugreport_context,
        )
        return self._call_llm(prompt)

    def _build_interpret_prompt(
        self,
        report_html: str,
        meta: Dict[str, Any],
        meminfo: str = '',
        txt: str = '',
        bugreport_context: str = '',
    ) -> str:
        prompt = f"""你是一位专业的 Android 性能分析专家。请对下面这份 bugreport 分析结果做“AI智能解读”，输出总结与评估。

## 报告基本信息
- 文件名: {meta.get('filename', '未知')}
- 分析时间: {meta.get('created_at', '未知')}
- 场景: {meta.get('scene', '未知')}

## HTML 报告内容（主数据源）
{report_html[:30000]}

## meminfo 摘要（补充）
{meminfo[:5000]}

## TXT 报告（补充）
{txt[:10000]}

## 被杀时刻原始 bugreport 片段（补充）
{bugreport_context[:20000]}

## 输出要求（Markdown）
请使用中文，并按以下结构输出：

### 1. 一句话结论
给出整体结论（优秀/良好/一般/较差）并说明主因。

### 2. 核心指标摘要
用表格列出你能从报告中提取到的关键指标（例如前1-前5驻留率、平均驻留、查杀次数、热启动等）。
如果缺失请写“未找到”。

### 3. 关键发现（3-6条）
- 从驻留表现、查杀分布、内存状态中提炼重点
- 标注“证据来源”是 HTML / meminfo / TXT / 原始bugreport

### 3.1 原始 bugreport 证据推断
- 如果提供了“被杀时刻原始 bugreport 片段”，请基于原始日志自行推断可能的查杀原因（例如 lowmemorykiller、am_kill、killinfo 字段、memfree/psi/thrashing 等）
- 给出“推断结论 + 对应原始行证据（可引用行号）”
- 若原始片段不足以判断，请明确写“证据不足”

### 4. 风险评估
从以下维度给出简评（高/中/低）：
- 后台驻留风险
- 内存压力风险
- 波动/回归风险

### 5. 优化建议（按优先级）
给出 P0/P1/P2 建议，每条建议包含：
- 目标问题
- 建议动作
- 预期收益

### 6. 复测建议
给出下一轮验证建议（场景、指标、判定标准）。

注意：
- 优先提取可量化数据，避免空泛描述；
- 不确定的数据不要编造，明确写“未找到”；
- 当 HTML 与原始bugreport冲突时，优先以原始bugreport为准并说明冲突点。
"""
        return prompt
    
    def _build_comparison_prompt(self, report1: str, report2: str,
                                 meta1: Dict[str, Any], 
                                 meta2: Dict[str, Any],
                                 meminfo1: str = "",
                                 meminfo2: str = "",
                                 txt1: str = "",
                                 txt2: str = "") -> str:
        """构建对比提示词 - 重点关注驻留率和关键指标"""
        prompt = f"""你是一位专业的 Android 性能分析专家。我需要你对比两份 bugreport 分析报告，重点关注应用驻留能力和内存状态。

## 报告基本信息

### 报告 A
- 文件名: {meta1.get('filename', '未知')}
- 分析时间: {meta1.get('created_at', '未知')}

### 报告 B
- 文件名: {meta2.get('filename', '未知')}
- 分析时间: {meta2.get('created_at', '未知')}

## 报告 A 内容 (HTML) - 提取关键数据

从 HTML 中提取以下数据：
1. 前1-前5驻留率、平均驻留
2. 启动全部进程数、启动主进程数、启动高亮主进程数
3. 第二轮热启动数
4. LMK 触发次数、查杀次数
5. 高亮主进程启动/驻留表（每个应用的冷启、热启、驻留次数）
6. 是否有游戏应用保活

HTML 内容：
{report1[:30000]}

## 报告 B 内容 (HTML)
{report2[:30000]}

## 内存摘要 A (meminfo_summary)
{meminfo1[:5000]}

## 内存摘要 B (meminfo_summary)
{meminfo2[:5000]}

## 文本分析报告 A (txt - 补充数据源)
{txt1[:10000]}

## 文本分析报告 B (txt - 补充数据源)
{txt2[:10000]}

## 请按以下格式提供对比分析（使用 Markdown 表格）

### 1. 核心驻留指标对比
| 指标 | 报告A | 报告B | 变化 |
|------|-------|-------|------|
| 前1驻留率 | | | |
| 前2驻留率 | | | |
| 前3驻留率 | | | |
| 前4驻留率 | | | |
| 前5驻留率 | | | |
| 平均驻留 | | | |
| 启动全部进程数 | | | |
| 启动高亮主进程数 | | | |
| 第二轮热启动数 | | | |

### 2. 驻留策略分析
- 分析两个设备的驻留策略是否一致
- 报告A（测试机）是否更保护前五应用？
- 报告B（对比机）是否固定保活指定应用？
- 两者策略差异点

### 3. 驻留利用率分析
- 平均后台驻留数量及占比
- 第二轮真实热启动数量及占比
- 驻留利用率 = 驻留数 / 启动数

### 4. 内存优先级占用对比 (meminfo)
从 meminfo 中提取各优先级（Foreground, Visible, Perceptible, Service, Cached, Free 等）的内存占用进行对比：
| 内存级别 | 报告A (MB) | 报告B (MB) |
|----------|------------|------------|
| Total | | |
| Free | | |
| Cached | | |
| SwapFree | | |
| Zram | | |
| 其他关键指标 | | |

### 5. TOP 应用内存占用对比
从 meminfo 中提取占用内存最多的应用进行对比：
| 排名 | 报告A 应用 | 报告A PSS(K) | 报告B 应用 | 报告B PSS(K) |
|------|------------|--------------|------------|--------------|

### 6. 游戏应用分析
- 两个报告中是否有游戏应用？
- 游戏应用的保活状态如何？

### 7. 内存状态详细对比
| 内存状态 | 报告A | 报告B |
|----------|-------|-------|
| memfree | | |
| swapfree | | |
| zram | | |
| file | | |
| buffers | | |
| cached | | |

### 8. 查杀分布对比
- LMK 查杀次数
- 一体化查杀次数
- 查杀时的平均 memfree
- 查杀分布（哪个进程被查杀最多）

### 9. 详细应用驻留对比
列出变化明显的应用（驻留增加或减少超过 10% 的应用）

### 10. 性能评估与建议
- 整体驻留能力评估
- 内存压力评估
- 优化建议

请用中文回答，首先从 HTML 和 meminfo 中提取数值填入表格。如果找不到请标注"未找到"。"""
        return prompt
    
    def _call_llm(self, prompt: str) -> str:
        """调用 LLM API"""
        self.reload_config()

        if not self.config.is_configured():
            return (
                '错误：未配置 LLM API 密钥。请优先在 web_app/.llm.env 中配置，'
                '或设置 OPENAI_API_KEY / MIFY_API_KEY / AZURE_OPENAI_API_KEY。'
            )

        try:
            requested_provider = (self.provider or self.config.default_provider or 'auto').lower().strip()
            provider = self.config.resolve_provider(requested_provider)
            self.provider = provider

            if provider == 'openai':
                return self._call_openai(prompt)
            if provider == 'azure':
                return self._call_azure(prompt)
            if provider == 'mify':
                return self._call_mify(prompt)

            if requested_provider and requested_provider not in {'auto'}:
                return f'错误：不支持的 LLM 提供商: {requested_provider}'
            return '错误：未找到可用的 LLM 提供商。'
        except Exception as e:
            return f"LLM 调用失败: {str(e)}"
    
    def _call_openai(self, prompt: str) -> str:
        """调用 OpenAI API"""
        if not self.config.openai_api_key:
            return '错误：当前使用 OpenAI，但未配置 OPENAI_API_KEY。'

        try:
            import openai
            client = openai.OpenAI(
                api_key=self.config.openai_api_key,
                base_url=self.config.openai_base_url
            )
            
            response = client.chat.completions.create(
                model=self.config.openai_model,
                messages=[
                    {"role": "system", "content": "你是一位专业的 Android 性能分析专家，擅长分析 bugreport 日志并提供优化建议。"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=4000
            )
            
            # 检查响应格式
            if hasattr(response, 'choices') and response.choices:
                return response.choices[0].message.content or ''
            elif hasattr(response, 'choices') and len(response.choices) > 0:
                return response.choices[0].message.content or ''
            elif isinstance(response, str):
                # 如果返回的是字符串，直接返回
                return response
            else:
                return f"API 返回格式异常: {type(response)}"
        except Exception as e:
            return f"OpenAI API 调用失败: {str(e)}"
    
    def _call_azure(self, prompt: str) -> str:
        """调用 Azure OpenAI API"""
        if not self.config.azure_api_key or not self.config.azure_endpoint:
            return '错误：当前使用 Azure OpenAI，但未配置 AZURE_OPENAI_API_KEY 或 AZURE_OPENAI_ENDPOINT。'

        try:
            import openai
            client = openai.AzureOpenAI(
                api_key=self.config.azure_api_key,
                azure_endpoint=self.config.azure_endpoint,
                api_version="2024-02-01"
            )
            
            response = client.chat.completions.create(
                model=self.config.azure_model,
                messages=[
                    {"role": "system", "content": "你是一位专业的 Android 性能分析专家，擅长分析 bugreport 日志并提供优化建议。"},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,
                max_tokens=4000
            )
            
            # 检查响应格式
            if hasattr(response, 'choices') and response.choices:
                return response.choices[0].message.content or ''
            elif hasattr(response, 'choices') and len(response.choices) > 0:
                return response.choices[0].message.content or ''
            elif isinstance(response, str):
                # 如果返回的是字符串，直接返回
                return response
            else:
                return f"API 返回格式异常: {type(response)}"
        except Exception as e:
            return f"Azure OpenAI API 调用失败: {str(e)}"
    
    def _call_mify(self, prompt: str) -> str:
        """调用 Mify 网关 API"""
        if not self.config.mify_api_key:
            return '错误：当前使用 Mify，但未配置 MIFY_API_KEY。'

        try:
            import requests
            def _build_chat_url(base_url: str) -> str:
                clean = base_url.strip().rstrip('/')
                if clean.endswith('/v1'):
                    return f'{clean}/chat/completions'
                return f'{clean}/v1/chat/completions'

            configured_base = (self.config.mify_base_url or '').strip()
            candidate_bases = []
            if configured_base:
                candidate_bases.append(configured_base)

            # 官方网关经常返回 CAS 登录页；内网直连地址作为稳定兜底
            fallback_base = 'http://model.mify.ai.srv'
            if fallback_base not in candidate_bases:
                candidate_bases.append(fallback_base)

            request_id = str(uuid.uuid4())
            headers = {
                'Authorization': f'Bearer {self.config.mify_api_key}',
                'X-Model-Provider-Id': self.config.mify_provider_id,
                'X-Model-Request-Id': request_id,
                'Content-Type': 'application/json',
            }
            if self.config.mify_user_id:
                headers['X-User-Id'] = self.config.mify_user_id
            if self.config.mify_conversation_id:
                headers['X-Conversation-Id'] = self.config.mify_conversation_id
            if self.config.mify_logging in {'none', 'dw', 'all'}:
                headers['X-Model-Logging'] = self.config.mify_logging

            data = {
                'model': self.config.mify_model or 'mimo-v2-flash',
                'messages': [
                    {'role': 'system', 'content': '你是一位专业的 Android 性能分析专家，擅长分析 bugreport 日志并提供优化建议。'},
                    {'role': 'user', 'content': prompt},
                ],
                'temperature': 0.7,
                'max_tokens': 4000,
            }
            if self.config.mify_reasoning_content_enabled is not None:
                data['mify_extra'] = {
                    'reasoning_content_enabled': self.config.mify_reasoning_content_enabled
                }

            last_error = ''
            for base in candidate_bases:
                url = _build_chat_url(base)
                try:
                    response = requests.post(
                        url,
                        headers=headers,
                        json=data,
                        timeout=self.config.mify_timeout_seconds,
                    )
                except Exception as e:
                    last_error = f'{base} 请求异常: {str(e)}'
                    continue

                content_type = (response.headers.get('content-type') or '').lower()
                body_text = response.text or ''

                if response.status_code != 200:
                    last_error = f'{base} 返回 {response.status_code}: {body_text[:500]}'
                    continue

                # 命中登录页/错误页（HTML）时回退到下一个地址
                body_head = body_text[:200].lower()
                if 'text/html' in content_type or '<!doctype html' in body_head or '<html' in body_head:
                    last_error = (
                        f'{base} 返回 HTML 页面（疑似登录页或网关错误页），'
                        f'content-type={content_type}'
                    )
                    continue

                try:
                    result = response.json()
                except Exception:
                    last_error = (
                        f'{base} 返回非 JSON，content-type={content_type}，'
                        f'响应前200字符: {body_text[:200]}'
                    )
                    continue

                if 'choices' in result and result['choices']:
                    return result['choices'][0]['message']['content']
                if 'error' in result:
                    last_error = f"{base} API 错误: {result['error']}"
                    continue
                last_error = f'{base} API 返回格式异常: {result}'

            return f'Mify API 调用失败: {last_error or "未知错误"}'
        except Exception as e:
            return f"Mify API 调用失败: {str(e)}"


# 全局 LLM 客户端实例
llm_client = LLMClient()
