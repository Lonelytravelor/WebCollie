# LLM 配置说明

## 功能说明

新增 AI 对比分析功能，可以选择两份已完成的分析报告，使用 LLM（大语言模型）自动分析两份报告的差异，包括：
- 整体性能对比
- 内存使用差异
- 启动时间对比
- 后台驻留情况
- 优化建议

## 支持的 LLM 提供商

### 1. OpenAI（推荐）

```bash
# 设置环境变量
export OPENAI_API_KEY="your-api-key"
export OPENAI_MODEL="gpt-4o-mini"  # 可选，默认使用 gpt-4o-mini
```

如果使用第三方 OpenAI 代理：
```bash
export OPENAI_BASE_URL="https://your-proxy.com/v1"
```

### 2. Azure OpenAI

```bash
export AZURE_OPENAI_API_KEY="your-azure-key"
export AZURE_OPENAI_ENDPOINT="https://your-resource.openai.azure.com"
export AZURE_OPENAI_MODEL="gpt-4"
export LLM_PROVIDER="azure"
```

### 3. 其他提供商

也支持智谱 AI、文心一言等国内大模型，配置方式类似：
```bash
export ZHIPU_API_KEY="your-zhipu-key"
export LLM_PROVIDER="zhipu"
```

## 配置步骤

### 推荐：使用固定配置文件（优先级最高）

1. 在 `web_app` 目录创建专用配置文件：
   ```bash
   cd /media/mi/ssd/安装包/OpenCollies/web_app
   cp .llm.env.example .llm.env
   ```
2. 编辑 `.llm.env`，填写你要使用的 provider、model、api key。
3. 重启服务生效。

Mify 对齐网关文档的关键项（可选）：
- `MIFY_PROVIDER_ID` -> `X-Model-Provider-Id`
- `MIFY_USER_ID` -> `X-User-Id`
- `MIFY_CONVERSATION_ID` -> `X-Conversation-Id`
- `MIFY_LOGGING` -> `X-Model-Logging`（`none`/`dw`/`all`）
- `MIFY_REASONING_CONTENT_ENABLED` -> `mify_extra.reasoning_content_enabled`

当前读取优先级（高 -> 低）：

1. `web_app/.llm.env`
2. 进程环境变量（`export XXX=...`）
3. `web_app/.env.local`
4. `web_app/.env`
5. 项目根目录 `.env`

> 建议把稳定配置放在 `web_app/.llm.env`，可避免不同终端/服务启动方式导致环境变量丢失。

1. **获取 API Key**
   - OpenAI: https://platform.openai.com/api-keys
   - Azure: Azure Portal -> OpenAI Service -> Keys and Endpoint

2. **设置环境变量**
   
   临时设置（当前终端会话）：
   ```bash
   export OPENAI_API_KEY="sk-..."
   ```
   
   永久设置（添加到 ~/.bashrc 或 ~/.zshrc）：
   ```bash
   echo 'export OPENAI_API_KEY="sk-..."' >> ~/.bashrc
   source ~/.bashrc
   ```

3. **重启服务**
   ```bash
   pkill -f "python.*app.py"
   cd /media/mi/ssd/安装包/OpenCollies/web_app
   python3 app.py
   ```

## 使用说明

1. 完成至少两份 bugreport 分析
2. 在"分析历史"区域点击"启用对比模式"
3. 选择两份已完成的报告（勾选复选框）
4. 点击"AI对比分析"按钮
5. 等待 30-60 秒，查看 AI 生成的对比分析报告

## 注意事项

1. **API 费用**: 对比分析会消耗 LLM API 的 tokens，请确保账户有足够的余额
2. **数据长度**: 由于 API 限制，每份报告只取前 15000 个字符进行分析
3. **隐私**: 报告内容会发送到 LLM 服务提供商，请确保不包含敏感信息
4. **错误处理**: 如果 LLM 调用失败，会显示错误信息，请检查 API Key 和网络连接

## 故障排除

### 错误：未配置 LLM API 密钥

**原因**: 没有设置 OPENAI_API_KEY 或其他 LLM 密钥

**解决**: 
```bash
export OPENAI_API_KEY="your-api-key"
```

### 错误：OpenAI API 调用失败

**原因**: API Key 无效、网络问题或余额不足

**解决**:
1. 检查 API Key 是否正确
2. 测试网络连接：`curl https://api.openai.com/v1/models -H "Authorization: Bearer $OPENAI_API_KEY"`
3. 检查账户余额：https://platform.openai.com/usage

### 对比结果为空或不准确

**原因**: 报告内容过长被截断，或 LLM 理解有误

**解决**:
- 这是正常现象，LLM 只能分析报告的主要部分
- 如需详细对比，建议分别查看两份报告的完整内容

## 技术细节

- **使用模型**: 默认使用 gpt-4o-mini（性价比高）
- **Token 消耗**: 每次对比约消耗 3000-5000 tokens
- **响应时间**: 通常 30-60 秒，取决于报告复杂度和网络状况
- **并发限制**: 每次只能进行一个对比任务
