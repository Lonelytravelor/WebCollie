# Collie Bugreport Web Analyzer - 数据存储结构

## 目录结构

```
web_app/
├── user_data/                          # 用户数据根目录
│   ├── {IP地址}/                      # 每个IP一个目录
│   │   ├── uploads/                   # 上传的bugreport文件
│   │   │   └── {任务ID}_{文件名}
│   │   └── results/                   # 分析结果
│   │       └── {任务ID}/
│   │           ├── analysis_{时间戳}.txt      # 文本报告
│   │           ├── analysis_{时间戳}.html     # HTML报告
│   │           ├── analysis_{时间戳}_device_info.txt  # 设备信息
│   │           └── analysis_{时间戳}_meminfo_summary.txt  # 内存摘要
```

## 示例

### 1. 本地访问 (127.0.0.1)
```
user_data/
└── 127.0.0.1/
    ├── uploads/
    │   ├── 01c3d420_bugreport-yili-BP2A.250605.031.A3-2026-02-10-13-51-50.txt
    │   ├── 61c39277_bugreport-yili-BP2A.250605.031.A3-2026-01-14-17-07-43.zip
    │   └── ...
    └── results/
        ├── 01c3d420/
        │   ├── analysis_20260213_163628.txt
        │   ├── analysis_20260213_163628.html
        │   ├── analysis_20260213_163628_device_info.txt
        │   └── analysis_20260213_163628_meminfo_summary.txt
        ├── 9d7df172/
        │   ├── analysis_20260213_163712.txt
        │   ├── analysis_20260213_163712.html
        │   ├── analysis_20260213_163712_device_info.txt
        │   └── analysis_20260213_163712_meminfo_summary.txt
        └── ...
```

### 2. 局域网访问 (192.168.1.100)
```
user_data/
└── 192.168.1.100/
    ├── uploads/
    │   └── {任务ID}_{文件名}
    └── results/
        └── {任务ID}/
            ├── analysis_{时间戳}.txt
            ├── analysis_{时间戳}.html
            └── ...
```

## 文件说明

### 上传文件
- **格式**: `{任务ID}_{原始文件名}`
- **示例**: `01c3d420_bugreport-yili-BP2A.250605.031.A3-2026-02-10-13-51-50.txt`
- **位置**: `user_data/{IP}/uploads/`

### 分析结果文件
- **文本报告**: `analysis_{时间戳}.txt` - 完整的分析报告
- **HTML报告**: `analysis_{时间戳}.html` - 可视化报告
- **设备信息**: `analysis_{时间戳}_device_info.txt` - 设备信息
- **内存摘要**: `analysis_{时间戳}_meminfo_summary.txt` - 内存使用摘要

## 访问权限

每个IP只能访问自己的数据：
- `127.0.0.1` 只能看到 `user_data/127.0.0.1/` 下的数据
- `192.168.1.100` 只能看到 `user_data/192.168.1.100/` 下的数据
- 不同IP之间数据完全隔离

## 数据清理

- **保留时间**: 7天
- **清理时间**: 每天凌晨2点
- **清理内容**: 超过7天的上传文件和分析结果

## 查看数据

### 1. 查看所有IP目录
```bash
ls -la /media/mi/ssd/安装包/OpenCollies/web_app/user_data/
```

### 2. 查看特定IP的数据
```bash
ls -la /media/mi/ssd/安装包/OpenCollies/web_app/user_data/127.0.0.1/
```

### 3. 查看具体任务
```bash
ls -la /media/mi/ssd/安装包/OpenCollies/web_app/user_data/127.0.0.1/results/01c3d420/
```

### 4. 查看报告内容
```bash
cat /media/mi/ssd/安装包/OpenCollies/web_app/user_data/127.0.0.1/results/01c3d420/analysis_20260213_163628.txt | head -50
```

## 注意事项

1. **IP地址格式**:
   - IPv4: `192.168.1.100`
   - IPv6: `2001:db8::1` (会转换为 `2001_db8__1`)
   - 本地: `127.0.0.1`

2. **任务ID**:
   - 8位随机字符串（如 `01c3d420`）
   - 每个任务唯一

3. **时间戳格式**:
   - `YYYYMMDD_HHMMSS`（如 `20260213_163628`）

4. **文件大小**:
   - 上传文件可能很大（几百MB）
   - 分析结果通常几MB到几十MB