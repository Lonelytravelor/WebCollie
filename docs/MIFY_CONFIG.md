# Mify 大模型推理网关 API 配置方案

## 方案一：OpenAI 兼容格式（推荐）

如果 Mify 网关支持标准 OpenAI API 格式，可以直接使用现有配置：

### 1. 设置环境变量
```bash
export OPENAI_API_KEY="your_mify_api_key"
export OPENAI_BASE_URL="https://mify.mioffice.cn/gateway"
export OPENAI_MODEL="gpt-4o-mini"  # 或其他支持的模型
```

### 2. 修改启动脚本
```bash
# 在 start.sh 中添加
export OPENAI_API_KEY="your_mify_api_key"
export OPENAI_BASE_URL="https://mify.mioffice.cn/gateway"
```

### 3. 测试连接
```bash
curl -X POST https://mify.mioffice.cn/gateway/v1/chat/completions \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "测试连接"}]
  }'
```

## 方案二：自定义 API 格式

如果 Mify 网关使用自定义格式，需要修改 `llm_client.py`：

### 1. 查看 API 文档获取以下信息：
- API 基础 URL
- 认证方式（Header 名称）
- 请求体结构
- 响应体结构

### 2. 修改 `llm_client.py` 的 `_call_mify` 方法

## 方案三：快速配置

### 1. 获取 API Key
访问 Mify 控制台：https://mify.mioffice.cn
- 登录你的账号
- 进入 API 管理
- 创建 API Key

### 2. 配置环境变量
```bash
# 临时配置
export MIFY_API_KEY="your_api_key"
export MIFY_BASE_URL="https://mify.mioffice.cn/gateway"
export MIFY_MODEL="gpt-4o-mini"

# 永久配置（添加到 ~/.bashrc）
echo 'export MIFY_API_KEY="your_api_key"' >> ~/.bashrc
echo 'export MIFY_BASE_URL="https://mify.mioffice.cn/gateway"' >> ~/.bashrc
echo 'export MIFY_MODEL="gpt-4o-mini"' >> ~/.bashrc
source ~/.bashrc
```

### 3. 重启服务
```bash
cd /media/mi/ssd/安装包/OpenCollies/web_app
./start.sh
```

## 常见 Mify 网关配置

### 1. 如果 Mify 是 OpenAI 兼容的
```python
# llm_client.py 中的配置
self.openai_base_url = os.getenv('OPENAI_BASE_URL', 'https://mify.mioffice.cn/gateway')
self.openai_model = os.getenv('OPENAI_MODEL', 'gpt-4o-mini')
```

### 2. 如果 Mify 有自定义端点
```python
# 需要修改 _call_mify 方法
def _call_mify(self, prompt: str) -> str:
    import requests
    
    url = f"{self.config.mify_base_url}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {self.config.mify_api_key}",
        "Content-Type": "application/json"
    }
    data = {
        "model": self.config.mify_model,
        "messages": [
            {"role": "system", "content": "你是一位专业的 Android 性能分析专家"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7,
        "max_tokens": 4000
    }
    
    response = requests.post(url, headers=headers, json=data, timeout=60)
    response.raise_for_status()
    result = response.json()
    return result["choices"][0]["message"]["content"]
```

## 测试步骤

### 1. 测试 API 连接
```bash
# 使用 curl 测试
curl -X POST https://mify.mioffice.cn/gateway/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [
      {"role": "user", "content": "你好，这是一个测试"}
    ]
  }'
```

### 2. 测试 Python 连接
```python
import openai
import os

client = openai.OpenAI(
    api_key=os.getenv("MIFY_API_KEY"),
    base_url=os.getenv("MIFY_BASE_URL")
)

response = client.chat.completions.create(
    model=os.getenv("MIFY_MODEL", "gpt-4o-mini"),
    messages=[{"role": "user", "content": "测试连接"}]
)

print(response.choices[0].message.content)
```

## 故障排除

### 1. 认证失败
- 检查 API Key 是否正确
- 确认 API Key 有权限访问该模型
- 检查 Authorization 头格式

### 2. 模型不存在
- 确认模型名称是否正确
- 查看 Mify 支持的模型列表
- 尝试其他模型名称

### 3. 网络问题
- 检查网络连接
- 确认防火墙设置
- 检查代理配置

## 下一步

请提供以下信息，我来帮你完成具体配置：

1. **Mify API 文档中的关键信息**：
   - API 基础 URL
   - 认证方式
   - 支持的模型列表
   - 请求/响应格式示例

2. **你的 API Key 格式**：
   - 是否是 `Bearer xxx` 格式？
   - 还是其他格式？

3. **测试结果**：
   - curl 请求是否成功？
   - 返回什么错误信息？

有了这些信息，我可以立即帮你修改代码并测试。