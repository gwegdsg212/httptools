#!/usr/bin/env bash
# 在 macOS 上打包 HTML 图片替换工具，生成 .app 应用包。
# 用法：
#   1) 将 tools 目录整个拷贝到 Mac
#   2) 打开「终端」，cd 到本目录
#   3) 执行：  bash build_app.sh
#
# 前置：Mac 已安装 Python 3.8+（建议 python.org 官方版或 Homebrew 版，自带 tkinter）。
set -euo pipefail

cd "$(dirname "$0")"

# 选择 python3
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "未找到 python3，请先安装 Python 3.8+（https://www.python.org/downloads/macos/）" >&2
    exit 1
fi

echo "==> 使用 Python: $("$PY" --version)"

# 校验 tkinter 可用
if ! "$PY" -c "import tkinter" >/dev/null 2>&1; then
    echo "当前 Python 缺少 tkinter，请改用 python.org 官方安装包，或: brew install python-tk" >&2
    exit 1
fi

echo "==> 安装打包依赖 (pyinstaller, pillow)..."
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q pyinstaller pillow

echo "==> 清理旧产物..."
rm -rf build dist

echo "==> 开始打包..."
"$PY" -m PyInstaller --noconfirm --clean HtmlBase64ImageReplacer-mac.spec

APP="dist/HTML图片替换工具.app"
if [ -d "$APP" ]; then
    # 去除 quarantine 标记，避免首次打开报“已损坏/无法验证”
    xattr -cr "$APP" 2>/dev/null || true
    echo ""
    echo "✅ 完成：$APP"
    echo "   双击即可运行。若提示“无法打开，因为来自身份不明的开发者”，"
    echo "   请在该 .app 上点右键 → 打开，或在「系统设置 → 隐私与安全性」中允许。"
else
    echo "❌ 打包失败，未找到 $APP" >&2
    exit 1
fi
