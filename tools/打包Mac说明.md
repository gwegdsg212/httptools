# 打包成 Mac 可执行软件（.app）

## 为什么不能在 Windows 上直接打包 Mac 版？
PyInstaller **不支持跨平台打包**——Windows 上只能产出 `.exe`，Mac 版（`.app`）必须**在 macOS 电脑上**打包。
本目录已准备好 Mac 打包所需的全部文件，拿到 Mac 上执行即可。

## 步骤（在一台 Mac 上操作）

1. **拷贝文件**：把整个 `tools` 目录拷到 Mac（U 盘 / 网盘 / 局域网均可）。至少需要：
   - `html_base64_image_replacer.py`（主程序）
   - `HtmlBase64ImageReplacer-mac.spec`（Mac 打包配置）
   - `build_app.sh`（一键打包脚本）

2. **确认 Python**：Mac 需装有 Python 3.8+，且自带 `tkinter`。
   - 推荐用 [python.org 官方安装包](https://www.python.org/downloads/macos/)（自带 tkinter）。
   - 若用 Homebrew：`brew install python python-tk`

3. **执行打包**：打开「终端」，进入该目录后运行：
   ```bash
   cd /path/to/tools
   bash build_app.sh
   ```

4. **取结果**：成功后在 `dist/HTML图片替换工具.app`，双击即可运行。

## 首次打开提示“身份不明的开发者 / 已损坏”怎么办？
应用未做 Apple 签名/公证，属正常现象。任选其一：
- 右键点 `.app` → **打开** → 在弹窗里再点「打开」；或
- 「系统设置 → 隐私与安全性」→ 找到被拦截的应用 → 点「仍要打开」。

脚本已自动执行 `xattr -cr` 去除隔离标记，多数情况下可直接双击。

## 说明
- **拖拽导入图片**：`windnd` 仅 Windows 可用，Mac 版不含该依赖，但程序会自动降级——
  在 Mac 上请用界面里的「选择图片文件」按钮，功能不受影响。
- **生成通用二进制（Intel + Apple 芯片通用）**：把 spec 里的 `target_arch=None`
  改为 `target_arch='universal2'`，并使用 python.org 的 *universal2* 版 Python。
  否则产物只适配当前 Mac 的芯片架构（在哪台 Mac 上打包就只保证那种芯片）。
- **图标**：如需自定义图标，准备一个 `icon.icns`，把 spec 中 `icon=None` 改成 `icon='icon.icns'`。
