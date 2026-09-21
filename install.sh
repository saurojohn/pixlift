#!/usr/bin/env bash
# PixLift 本地安装：建 ~/bin/pixlift + ~/Applications/PixLift.app + LaunchAgent
# 无 macOS-only 假设（除了 .app bundle 和 launchd）— Linux/Windows 用户跑会有
# 友好的 "skip" 提示而不是失败。
#
# 用法：
#   ./install.sh           — 全自动安装
#   ./install.sh --uninstall — 卸载（reverse）
#   ./install.sh --no-app   — 只装 pixlift 命令，不建 .app / LaunchAgent

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"

# ---- 参数 ----
MODE="install"
BUILD_APP=1
for arg in "$@"; do
    case "$arg" in
        --uninstall) MODE="uninstall" ;;
        --no-app)    BUILD_APP=0 ;;
        --help|-h)
            sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "未知参数: $arg" >&2
            exit 1
            ;;
    esac
done

# ---- 通用路径（macOS / Linux 都兼容）----
if [ "$(uname -s)" = "Darwin" ]; then
    HOME_BIN="$HOME/bin"
    HOME_APPS="$HOME/Applications"
    LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
    OS="macos"
else
    HOME_BIN="$HOME/.local/bin"
    HOME_APPS="$HOME/.local/share/applications"  # Linux .desktop 路径（暂不建 .app）
    LAUNCH_AGENTS=""
    OS="linux"
fi

VENV_PY="$PROJECT_DIR/.venv/bin/python"

# ==== Uninstall ====
if [ "$MODE" = "uninstall" ]; then
    echo "[install] uninstalling PixLift user-level files"

    # 停服务
    if [ -x "$PROJECT_DIR/scripts/stop.sh" ]; then
        bash "$PROJECT_DIR/scripts/stop.sh" 2>/dev/null || true
    fi
    if [ "$OS" = "macos" ] && [ -f "$LAUNCH_AGENTS/com.saurojohn.pixlift.plist" ]; then
        launchctl unload "$LAUNCH_AGENTS/com.saurojohn.pixlift.plist" 2>/dev/null || true
        rm -f "$LAUNCH_AGENTS/com.saurojohn.pixlift.plist"
        echo "[install] removed launchd plist"
    fi

    # 删 ~/bin/pixlift
    if [ -f "$HOME_BIN/pixlift" ]; then
        rm -f "$HOME_BIN/pixlift"
        echo "[install] removed $HOME_BIN/pixlift"
    fi

    # 删 .app
    if [ -d "$HOME_APPS/PixLift.app" ]; then
        rm -rf "$HOME_APPS/PixLift.app"
        echo "[install] removed $HOME_APPS/PixLift.app"
    fi

    echo "[install] uninstall done."
    exit 0
fi

# ==== Install ====
echo "[install] PixLift local install"
echo "[install]   project:  $PROJECT_DIR"
echo "[install]   home bin: $HOME_BIN"
echo "[install]   OS:       $OS"

# ---- 1) 检查 venv ----
if [ ! -x "$VENV_PY" ]; then
    echo "[install] ✗ $VENV_PY 不存在" >&2
    echo "[install] 先运行：python3 -m venv .venv && source .venv/bin/activate && pip install -e ." >&2
    exit 1
fi
echo "[install] ✓ venv OK ($VENV_PY)"

# ---- 2) ~/bin ----
mkdir -p "$HOME_BIN"

# 用 sed 替换模板里的 $PROJECT_DIR
PIX_SCRIPT="$HOME_BIN/pixlift"
sed "s|\$PROJECT_DIR|$PROJECT_DIR|g" "$PROJECT_DIR/scripts/launcher/pixlift.sh.template" > "$PIX_SCRIPT"
chmod +x "$PIX_SCRIPT"
echo "[install] ✓ installed $PIX_SCRIPT"

# PATH 检查（仅 macOS；Linux .local/bin 通常已在 PATH）
if [ "$OS" = "macos" ] && ! echo "$PATH" | grep -q "$HOME_BIN"; then
    if ! grep -q "$HOME_BIN" "$HOME/.zshrc" 2>/dev/null; then
        echo "" >> "$HOME/.zshrc"
        echo "# PixLift local command" >> "$HOME/.zshrc"
        echo "export PATH=\"\$HOME/bin:\$PATH\"" >> "$HOME/.zshrc"
        echo "[install] ✓ added $HOME_BIN to ~/.zshrc PATH"
    fi
fi

# ---- 3) macOS .app + LaunchAgent ----
if [ "$OS" = "macos" ] && [ "$BUILD_APP" = "1" ]; then

    APP_DIR="$HOME_APPS/PixLift.app"
    mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"

    # Info.plist（无变量替换，全静态）
    cat > "$APP_DIR/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>PixLift</string>
    <key>CFBundleDisplayName</key><string>PixLift</string>
    <key>CFBundleIdentifier</key><string>com.saurojohn.pixlift</string>
    <key>CFBundleVersion</key><string>1.2.0</string>
    <key>CFBundleShortVersionString</key><string>1.2</string>
    <key>CFBundleExecutable</key><string>PixLiftLauncher</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>LSMinimumSystemVersion</key><string>11.0</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>NSPrincipalClass</key><string>NSApplication</string>
    <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST

    # 启动器脚本（替换 PROJECT_DIR）
    sed "s|\$PROJECT_DIR|$PROJECT_DIR|g" \
        "$PROJECT_DIR/scripts/launcher/PixLiftLauncher.template" \
        > "$APP_DIR/Contents/MacOS/PixLiftLauncher"
    chmod +x "$APP_DIR/Contents/MacOS/PixLiftLauncher"

    # 图标：从模板生成（避免硬编码路径）
    if [ ! -f "$APP_DIR/Contents/Resources/AppIcon.icns" ]; then
        "$VENV_PY" - "$PROJECT_DIR" "$APP_DIR/Contents/Resources" <<'PYEOF'
import sys
from pathlib import Path
project_dir, resources_dir = sys.argv[1], sys.argv[2]
resources_dir = Path(resources_dir)
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("[install] skip icon (Pillow not available)")
    sys.exit(0)

SIZE = 1024
img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)
for y in range(SIZE):
    t = y / SIZE
    r = int(30 + (88 - 30) * t); g = int(35 + (60 - 35) * t); b = int(80 + (180 - 80) * t)
    draw.line([(0, y), (SIZE, y)], fill=(r, g, b, 255))
mask = Image.new("L", (SIZE, SIZE), 0)
ImageDraw.Draw(mask).rounded_rectangle([(0, 0), (SIZE, SIZE)], radius=220, fill=255)
out = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
out.paste(img, (0, 0), mask)
img, draw = out, ImageDraw.Draw(out)
font = None
for fp in ["/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/SFNS.ttf"]:
    try: font = ImageFont.truetype(fp, 600); break
    except Exception: continue
font = font or ImageFont.load_default()
arrow = "↗"
bbox = draw.textbbox((0, 0), arrow, font=font); tw, th = bbox[2]-bbox[0], bbox[3]-bbox[1]
ax = (SIZE - tw) // 2 - bbox[0]; ay = (SIZE - th) // 2 - bbox[1] - 60
draw.text((ax, ay), arrow, font=font, fill=(255, 255, 255, 255))
font2 = None
for fp in ["/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/SFNS.ttf"]:
    try: font2 = ImageFont.truetype(fp, 200); break
    except Exception: continue
font2 = font2 or ImageFont.load_default()
label = "4x"
bbox = draw.textbbox((0, 0), label, font=font2); tw2, th2 = bbox[2]-bbox[0], bbox[3]-bbox[1]
lx = (SIZE - tw2) // 2 - bbox[0]; ly = ay + (bbox[3]-bbox[1]) + 40
draw.rounded_rectangle(
    [lx-60, ly-30, lx+tw2+60, ly+th2+30], radius=80, fill=(255, 255, 255, 230))
draw.text((lx, ly), label, font=font2, fill=(50, 60, 120, 255))
png_1024 = resources_dir / "icon_1024.png"
img.save(png_1024, "PNG")
# 用 sips 生成所有 sizes + iconutil 打包 icns
import subprocess
iconset = resources_dir / "icon.iconset"
iconset.mkdir()
for size, names in [(16, ["icon_16x16.png"]), (32, ["icon_16x16@2x.png", "icon_32x32.png"]),
                     (64, ["icon_32x32@2x.png"]), (128, ["icon_128x128.png"]),
                     (256, ["icon_128x128@2x.png", "icon_256x256.png"]),
                     (512, ["icon_256x256@2x.png", "icon_512x512.png"]),
                     (1024, ["icon_512x512@2x.png"])]:
    png_path = resources_dir / f"icon_{size}.png"
    subprocess.run(["sips", "-z", str(size), str(size), str(png_1024),
                    "--out", str(png_path)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for name in names:
        subprocess.run(["cp", str(png_path), str(iconset / name)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
subprocess.run(["iconutil", "-c", "icns", str(iconset),
                "-o", str(resources_dir / "AppIcon.icns")], check=True)
# 清理中间文件
for p in resources_dir.glob("icon_*.png"):
    p.unlink()
# iconset 含 macOS 自动生成的 .DS_Store，shutil.rmtree 才能清干净
import shutil
shutil.rmtree(iconset)
print("[install] ✓ generated AppIcon.icns")
PYEOF
    fi

    # LaunchAgent plist
    LOG_DIR="$PROJECT_DIR/logs"
    mkdir -p "$LOG_DIR"
    mkdir -p "$LAUNCH_AGENTS"
    PLIST="$LAUNCH_AGENTS/com.saurojohn.pixlift.plist"
    sed -e "s|\$PROJECT_DIR|$PROJECT_DIR|g" \
        -e "s|\$VENV_PYTHON|$VENV_PY|g" \
        -e "s|\$LOG_DIR|$LOG_DIR|g" \
        "$PROJECT_DIR/scripts/launcher/com.saurojohn.pixlift.plist.template" > "$PLIST"
    # 注册（如果之前已 load 过，先 unload）
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
    echo "[install] ✓ installed LaunchAgent → $PLIST"

    # 注册到 Launch Services（Finder/Dock/Launchpad 能找到）
    /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
        -f "$APP_DIR" >/dev/null 2>&1 || true
    echo "[install] ✓ installed $APP_DIR"
    echo "[install]   双击图标 / Launchpad 搜 PixLift / 从 Dock 启动"
fi

echo ""
echo "[install] ✓ PixLift installed. 试用："
echo "         pixlift status"
echo "         pixlift open"