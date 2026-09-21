# PixLift

局域网内使用的 AI 图片像素提升（超分辨率）Web 应用。打开浏览器 → 拖入图片 → 选择模型和倍率 → 下载结果。

底层用 [Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN) 的 Python 包 + PyTorch 推理。自动选择 MPS（macOS Apple Silicon）/ CUDA（NVIDIA）/ CPU 后端。

> ⚠️ **仅供内网使用**。不内置鉴权；不要把端口直接暴露到公网。

---

## 系统要求

- Python 3.10+（开发环境为 3.14）
- 8 GB+ 内存
- macOS Apple Silicon（M1/M2/M3/M4）— MPS 自动加速
- NVIDIA GPU + CUDA — 自动加速
- 无 GPU 时回退 CPU（慢但能用）
- 磁盘：PyTorch ~1 GB + 模型 ~67 MB/个

---

## 安装

### 1. 克隆与 Python 依赖

```bash
git clone <repo> pixlift && cd pixlift
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .
```

> 依赖里有 `torch`、`realesrgan`、`basicsr`、`opencv-python-headless`、`scipy`、`tqdm`。
> 首次安装 PyTorch 会下载约 1 GB。

### 2. 下载模型

首次启动会自动从 GitHub release 下载 `realesrgan-x4plus.pth` 到 `models/`（约 67 MB）。
也可手动下载：

```bash
mkdir -p models
# 从 https://github.com/xinntao/Real-ESRGAN/releases 下载
# realesrgan-x4plus.pth / realesrgan-x4plus-anime.pth / realesr-animevideov3.pth / realesrnet-x4plus.pth
# 放到 models/
```

支持的模型：

| 文件 | 用途 |
|---|---|
| `realesrgan-x4plus.pth` | 通用照片（默认） |
| `realesrgan-x4plus-anime.pth` | 动漫插画 |
| `realesr-animevideov3.pth` | 动漫·快 |
| `realesrnet-x4plus.pth` | 自然纹理·柔和 |

只放哪些就加载哪些；不存在的模型会在 `/api/health` 里被标记为不可用。

### 3. 启动

```bash
python run.py
```

默认监听 `http://0.0.0.0:8000`。浏览器打开即可使用。

也可直接：

```bash
uvicorn pixlift.app:app --host 0.0.0.0 --port 8000
```

API 文档：`http://localhost:8000/docs`。

### 4. macOS 一键本地安装（可选）

把 PixLift 装成 macOS 应用 + 开机自启 + `pixlift` 命令：

```bash
./install.sh           # 安装：~/bin/pixlift + ~/Applications/PixLift.app + LaunchAgent
./install.sh --uninstall  # 卸载
./install.sh --no-app   # 只装 pixlift 命令（不建 .app / LaunchAgent）
```

装完后：
- 双击 `~/Applications/PixLift.app` 图标 → 浏览器打开
- 终端跑 `pixlift status / open / logs / restart` 控制服务
- 开机自动启动 + 进程崩溃自动重启（launchd KeepAlive）

Linux 用户跑 `./install.sh` 会跳过 .app 和 LaunchAgent 部分，只装 `~/.local/bin/pixlift`。

---

## 模型

| 模型 | 适用 | 备注 |
|---|---|---|
| `realesrgan-x4plus` | 通用照片 | 默认推荐 |
| `realesrgan-x4plus-anime` | 动漫插画 | 线稿友好 |
| `realesr-animevideov3` | 动漫·快 | 速度优先 |
| `realesrnet-x4plus` | 自然纹理 | 更柔和 |

倍率：2× / 3× / 4×。

输出格式：PNG（无损默认）/ JPG（更小）/ WebP（更小）。

---

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8000` | 监听端口 |
| `LOG_LEVEL` | `info` | uvicorn 日志级别 |
| `MAX_UPLOAD_MB` | `20` | 单图最大 MB |
| `MAX_LONG_EDGE` | `4096` | 输入长边最大像素 |
| `SYNC_THRESHOLD_BYTES` | `2097152` | < 该值走同步路径（直接返回 PNG） |
| `JOB_TIMEOUT_S` | `120` | 单 job 最长执行秒数 |
| `KEEP_TMP_HOURS` | `0` | tmp 目录保留时长；0 = 默认 10 分钟 |
| `MAX_JOBS` | `50` | 内存中保留 job 数量上限 |
| `MODEL_DIR` | `models` | 模型缓存目录 |
| `TMP_DIR` | `tmp` | 临时输出目录 |
| `ALLOWED_ORIGINS` | — | CORS 允许来源（逗号分隔） |

---

## API 速查

```bash
# 健康
curl http://localhost:8000/api/health

# 上传（小图同步）
curl -F image=@photo.png \
     -F model=realesrgan-x4plus \
     -F scale=4 \
     -F format=png \
     http://localhost:8000/api/upscale \
     -o out.png

# 上传（大图异步，返回 job_id）
JOB=$(curl -s -F image=@big.jpg \
        -F model=realesrgan-x4plus \
        -F scale=4 \
        -F format=png \
        http://localhost:8000/api/upscale | jq -r .job_id)

# 轮询
while [ "$(curl -s http://localhost:8000/api/jobs/$JOB | jq -r .status)" != "done" ]; do
  sleep 0.5
done

# 下载
curl http://localhost:8000/api/jobs/$JOB/download -o out.png
```

---

## 开发

```bash
# 测试
pytest tests/ -v

# 启动脚本
./scripts/start.sh    # 后台启动，写 pid 到 pixlift.pid，日志到 logs/pixlift.log
./scripts/stop.sh     # 停止
```

---

## 性能参考（M1 Pro, MPS）

| 输入 | 倍率 | 模型 | 大约耗时 |
|---|---|---|---|
| 256×256 | 4× | realesrgan-x4plus | 5-8 秒 |
| 512×512 | 4× | realesrgan-x4plus | 1-2 秒 |
| 1920×1080 | 4× | realesrgan-x4plus | 5-8 秒 |
| 3000×3000 | 4× | realesrgan-x4plus | 12-18 秒 |

NVIDIA GPU 通常更快（CUDA）。CPU 模式 4× 3000² 大约 1-3 分钟。

---

## 故障排查

### `/api/health` 返 503 且 `ready=false`

常见原因：

- PyTorch 未正确安装 → `python -c "import torch; print(torch.__version__)"`
- MPS 不可用 → macOS 需 12.3+，且 PyTorch ≥ 1.12
- 模型缺失 → 检查 `models/` 目录

### 模型下载失败（首次运行）

```bash
# 手动下载到 MODEL_DIR（默认 ./models）
# 从 https://github.com/xinntao/Real-ESRGAN/releases 拉对应 .pth
```

### 处理超时（>120s）

```bash
export JOB_TIMEOUT_S=300
python run.py
```

### PyTorch 安装失败

- 走国内镜像：`pip install torch -i https://pypi.tuna.tsinghua.edu.cn/simple`
- 或去 https://pytorch.org/get-started/locally/ 选对应 CUDA 版本

---

## 安全说明

- 不内置鉴权 / 用户系统
- 文件保存到 `tmp/<job-id>/`，默认 10 分钟自动清理
- CORS 默认仅同源（不开）
- 不要把端口暴露到公网；如必须公网访问，自加 reverse proxy + auth

---

## License

MIT

## Credits

- [Real-ESRGAN](https://github.com/xinntao/Real-ESRGAN) — 模型与训练代码
- [PyReal-ESRGAN](https://github.com/ai-forever/Real-ESRGAN) — Python 推理包
- [PyTorch](https://pytorch.org/) — 推理框架（含 MPS / CUDA / CPU 后端）