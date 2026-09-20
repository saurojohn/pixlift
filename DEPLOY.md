# PixLift 部署指南

局域网内 AI 超分辨率小工具，可部署到任意 Linux VPS（Hetzner / AWS Lightsail / DigitalOcean 等）。

## 0. 系统要求

- Linux x86_64（推荐 Ubuntu 22.04+）
- 4 GB+ RAM（PyTorch + 模型加载）
- 模型：每个 ~67 MB，总共 4 个 ~270 MB
- **GPU 可选**：有 NVIDIA GPU 配 CUDA 可大幅加速；CPU 也能跑（慢但可用）
- 磁盘：≥ 5 GB（含 PyTorch + 模型 + 临时输出）

## 1. 安装 Docker（推荐）

Ubuntu：

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # 让当前用户免 sudo 跑 docker
newgrp docker
```

macOS / Windows：装 [Docker Desktop](https://www.docker.com/products/docker-desktop)。

## 2. 准备模型

下载 4 个 Real-ESRGAN 模型到 `./models/`：

```bash
mkdir -p models && cd models

# 通用照片
curl -L -o realesrgan-x4plus.pth \
  https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth

# 动漫插画（6 RRDB blocks 小模型）
curl -L -o RealESRGAN_x4plus_anime_6B.pth \
  https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.2.4/RealESRGAN_x4plus_anime_6B.pth

# 动漫视频（轻量 SRVGG）
curl -L -o realesr-animevideov3.pth \
  https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-animevideov3.pth

# 自然纹理
curl -L -o RealESRNet_x4plus.pth \
  https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.1/RealESRNet_x4plus.pth
```

> 注意：引擎会**自动识别**多种命名（小写 `realesrgan-x4plus.pth` 或官方驼峰 `RealESRGAN_x4plus.pth` 都行）。文件名不对才会失败。

## 3. 启动

```bash
docker compose up -d
```

浏览器打开 `http://<server-ip>:8000`。

## 4. 验证

```bash
curl http://localhost:8000/api/health | jq
# 应该看到 ready=true, models 含 4 个模型，gpu=mps/cuda/cpu
```

## 5. 防火墙

只允许局域网访问：

```bash
# Ubuntu (ufw)
sudo ufw allow from 192.168.0.0/16 to any port 8000
sudo ufw deny 8000

# 或只允许特定 IP
sudo ufw allow from 192.168.1.100 to any port 8000
```

## 6. 配置

所有环境变量在 `docker-compose.yml` 的 `environment` 段，或在 `.env`：

```bash
# .env
MAX_UPLOAD_MB=50
MAX_LONG_EDGE=8192
JOB_TIMEOUT_S=300
MAX_CONCURRENT_UPSCALES=2   # CPU=1, GPU=2-4
HOST=0.0.0.0
```

重启生效：`docker compose up -d`。

## 7. 不用 Docker（原生）

不想用 Docker 也可以直接装：

```bash
git clone https://github.com/<your>/pixlift.git
cd pixlift
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .

# 启动
./scripts/start.sh        # 后台启动，pid -> pixlift.pid，日志 -> logs/pixlift.log
./scripts/stop.sh         # 停止
```

## 8. 启用 GPU（NVIDIA）

Linux + NVIDIA GPU + Docker：

```bash
# 安装 NVIDIA Container Toolkit
distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list | \
    sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# 在 docker-compose.yml 里加 deploy.resources.reservations.devices
# 或加 runtime: nvidia
```

原生安装：

```bash
# CUDA 12.x + cuDNN
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
```

## 9. 反向代理（Nginx + HTTPS）

公网/内网暴露时强烈建议加 TLS：

```nginx
server {
    listen 443 ssl;
    server_name pixlift.example.com;

    ssl_certificate /etc/letsencrypt/live/pixlift.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/pixlift.example.com/privkey.pem;

    client_max_body_size 100M;       # 大图上传

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 600s;      # 大图推理超时
        proxy_send_timeout 600s;
    }
}
```

`certbot --nginx -d pixlift.example.com` 自动配 Let's Encrypt。

## 10. 监控 / 日志

- 日志：`logs/pixlift.log`（Docker 卷挂载到 host）或 `docker logs pixlift`
- 健康检查：`/api/health`（Docker 自动）
- 进程监控：`/api/health` 返 200 = 健康

## 11. 故障排查

### `/api/health` 返 503 + `ready=false`

```bash
docker logs pixlift | tail -30
ls -la models/   # 必须有 4 个 .pth 文件
```

### OOM（内存不足）

```yaml
# docker-compose.yml
deploy:
  resources:
    limits:
      memory: 8G    # 调高
```

或减小 `MAX_CONCURRENT_UPSCALES=1` + `MAX_UPLOAD_MB=20`。

### 处理超时

```bash
JOB_TIMEOUT_S=600   # 大图（3000×3000+）需要更久
```

### macOS 上容器性能差

MPS（Apple Silicon GPU 加速）**不进 Docker**。要在 macOS 上用 GPU，直接用原生启动：

```bash
./scripts/start.sh
```

## 12. 安全

- **不内置鉴权**。默认绑定 `0.0.0.0`（LAN 监听），但**不要直接公网暴露**
- 公网访问必须加反向代理 + BasicAuth / OAuth
- `ALLOWED_ORIGINS=*` **被拒收**（PixLift 启动时校验失败）
- tmp 目录默认 10 分钟自动清理，可通过 `KEEP_TMP_HOURS` 调整