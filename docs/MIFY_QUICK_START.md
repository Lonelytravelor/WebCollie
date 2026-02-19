# Mify API 配置说明

## 快速配置步骤

### 1. 获取 API Key
访问 Mify 控制台：https://mify.mioffice.cn
- 登录你的账号
- 进入 "API 管理" 或 "密钥管理"
- 创建新的 API Key
- 复制 API Key

### 2. 设置环境变量

**临时配置（当前终端会话）：**
```bash
export MIFY_API_KEY="your_api_key"
export MIFY_BASE_URL="https://mify.mioffice.cn/gateway"
export MIFY_MODEL="gpt-4o-mini"
```

**永久配置（添加到 ~/.bashrc）：**
```bash
echo 'export MIFY_API_KEY="your_api_key"' >> ~/.bashrc
echo 'export MIFY_BASE_URL="https://mify.mioffice.cn/gateway"' >> ~/.bashrc
echo 'export MIFY_MODEL="gpt-4o-mini"' >> ~/.bashrc
source ~/.bashrc
```

### 3. 修改启动脚本

编辑 `/media/mi/ssd/安装包/OpenCollies/web_app/start.sh`：

```bash
# 在文件开头添加
export MIFY_API_KEY="your_api_key"
export MIFY_BASE_URL="https://mify.mioffice.cn/gateway"
export MIFY_MODEL="gpt-4o-mini"
```

### 4. 重启服务

```bash
cd /media/mi/ssd/安装包/OpenCollies/web_app
./start.sh
```

## 测试配置

### 1. 命令行测试

```bash
curl -X POST https://mify.mioffice.cn/gateway/v1/chat/completions \
  -H "Authorization: Bearer $MIFY_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [
      {"role": "user", "content": "你好，这是一个测试"}
    ]
  }'
```

### 2. Python 测试

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

## 支持的模型

根据 Mify 网关的常见配置，可能支持以下模型：

- `gpt-4o-mini` - 推荐，性价比高
- `gpt-4o` - 更强的推理能力
- `qwen-72b` - 阿里通义千问
- `deepseek-v2` - 深度求索
- `glm-4` - 智谱 AI

## 常见问题

### 1. 认证失败 (401 Unauthorized)
**原因**: API Key 无效或格式错误
**解决**: 
- 检查 API Key 是否正确
- 确认 Authorization 头格式为 `Bearer xxx`
- 检查 API Key 是否有权限访问该模型

### 2. 模型不存在 (404 Model not found)
**原因**: 模型名称错误或无权限
**解决**:
- 查看 Mify 支持的模型列表
- 尝试其他模型名称
- 联系管理员确认权限

### 3. 速率限制 (429 Too Many Requests)
**原因**: 请求频率过高
**解决**:
- 降低请求频率
- 联系管理员提高配额
- 使用队列处理请求

### 4. 网络连接失败
**原因**: 网络问题或防火墙
**解决**:
- 检查网络连接
- 确认防火墙允许访问 `mify.mioffice.cn`
- 检查代理设置

## 安全建议

1. **不要将 API Key 提交到代码仓库**
2. **使用环境变量存储敏感信息**
3. **定期轮换 API Key**
4. **限制 API Key 的使用范围**

## 联系方式

如有问题，请联系：
- Mify 网关管理员
- 小米内部技术支持