# Collie Web 应用部署指南

## 方式一：Nginx 反向代理（推荐用于生产环境）

### 1. 安装 Nginx
```bash
sudo apt update
sudo apt install nginx
```

### 2. 配置 Nginx
```bash
# 复制配置文件
sudo cp /media/mi/ssd/安装包/OpenCollies/web_app/nginx.conf /etc/nginx/sites-available/collie

# 编辑配置文件，修改 server_name 为你想要的域名
sudo nano /etc/nginx/sites-available/collie

# 启用配置
sudo ln -s /etc/nginx/sites-available/collie /etc/nginx/sites-enabled/

# 测试配置
sudo nginx -t

# 重启 Nginx
sudo systemctl restart nginx
```

### 3. 配置本地 hosts（内网使用）
如果是内网使用，需要在访问设备的 hosts 文件中添加：
```
<服务器IP>  collie
```

例如：
```
192.168.1.100  collie
```

然后就可以通过 http://collie 访问了。

### 4. 启动 Web 应用
```bash
cd /media/mi/ssd/安装包/OpenCollies/web_app
./start.sh
```

## 方式二：修改端口为 80（简单但不够灵活）

如果需要直接通过 IP 访问而不加端口号，可以修改 `app.py`：

```python
app.run(host='0.0.0.0', port=80, debug=False)
```

**注意**：需要使用 root 权限运行（端口 80 需要特权）

## 方式三：使用 Docker 部署（可选）

### 1. 创建 Dockerfile
```dockerfile
FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

EXPOSE 5000

CMD ["python", "app.py"]
```

### 2. 构建并运行
```bash
docker build -t collie-web .
docker run -d -p 80:5000 --name collie-web collie-web
```

## 域名配置说明

### 内网使用（无DNS服务器）
1. 在每台访问设备的 hosts 文件中添加域名映射
2. Windows: `C:\Windows\System32\drivers\etc\hosts`
3. Linux/Mac: `/etc/hosts`

### 有DNS服务器
1. 在 DNS 服务器上添加 A 记录，指向服务器 IP
2. 例如：`collie.yourcompany.com` -> `192.168.1.100`

### 公网使用
1. 购买域名并配置 DNS 解析到服务器公网IP
2. 配置 SSL 证书启用 HTTPS
3. 建议配合 Cloudflare 等服务使用

## 常用命令

```bash
# 检查 Nginx 配置
sudo nginx -t

# 重启 Nginx
sudo systemctl restart nginx

# 查看 Nginx 日志
sudo tail -f /var/log/nginx/collie_error.log

# 停止 Web 应用
pkill -f "python.*app.py"

# 查看 Web 应用日志
tail -f /media/mi/ssd/安装包/OpenCollies/web_app/app.log
```
