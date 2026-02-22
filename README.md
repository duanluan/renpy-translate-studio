# Ren'Py Translate Studio

基于 `Ren'Py` 的翻译工具集，包含两种入口：

- 桌面端（Flet）：`src/app1/app1.py`
- 命令行流水线：`scripts/renpy_translate_pipeline.py`

当前版本已针对常见“翻译后仍无中文选项 / 中文显示方块 / 语言按钮写死 English”等问题提供自动修复能力。

## 功能概览

- 一键跑完整流程：解包 `.rpa` -> 反编译 `.rpyc` -> 生成 `tl` 模板 -> 机器翻译
- 支持 OpenAI 兼容接口（含 Ollama）和 DeepL
- 支持缓存与断点续跑（`.translation_cache.json`）
- 自动启用目标语言（写入 `game/_renpy_translate_autolang.rpy`）
- UI 兼容修复：
  - 自动补 `config.language`
  - 自动修正 `screens.rpy` 语言按钮
  - 自动尝试注入 CJK 字体（如 `simhei.ttf`）
  - 自动写入 `before_main_menu` 语言钩子
- 桌面端提供“快速修复并启动”按钮，适合增量修复

## 项目关键文件

- `scripts/renpy_translate_pipeline.py`：CLI 主流程
- `src/app1/app1.py`：Flet 桌面端入口
- `src/app1/res/config.yml`：桌面端默认配置与快速修复配置

## 环境要求

- Python `>=3.9,<=3.13`
- 建议使用 `uv`
- 若需解包/反编译：本机需可用 `git`、`pip`
- 若需机器翻译：
  - OpenAI 兼容服务（Ollama / OpenAI / LM Studio 等）
  - 或 DeepL Key

## 快速开始

### 1. 安装依赖

```bash
uv venv
uv sync
uv pip install -e .
```

### 2. 启动桌面端

```bash
uv run python src/app1/app1.py
```

可选：指定配置文件

```bash
uv run python src/app1/app1.py --config src/app1/res/config.yml
```

## 桌面端使用建议

### 常规翻译

1. 填 `游戏目录`
2. 选择翻译后端（OpenAI / DeepL）
3. 点击 `开始运行`

### 快速修复（推荐用于“游戏能跑但中文选项异常”）

点击 `快速修复并启动`。默认读取 `config.yml` 的 `renpy_gui.quick-fix`：

- `provider: none`
- `skip-extract: true`
- `skip-decompile: true`
- `skip-template: true`
- `skip-mt: true`
- `auto-enable-language: true`

该模式会快速执行语言接入修复并在成功后自动启动游戏。

## 命令行用法

### 查看帮助

```bash
python scripts/renpy_translate_pipeline.py --help
```

### Ollama/OpenAI 兼容翻译示例

```bash
python scripts/renpy_translate_pipeline.py \
  --game-dir "E:/games/YourGame" \
  --provider openai \
  --model dolphin3:latest \
  --base-url http://127.0.0.1:11434 \
  --openai-format json \
  --language chinese \
  --target-language "Simplified Chinese"
```

### 仅做快速修复（不跑机翻）

```bash
python scripts/renpy_translate_pipeline.py \
  --game-dir "E:/games/YourGame" \
  --provider none \
  --skip-extract \
  --skip-decompile \
  --skip-template \
  --skip-mt \
  --auto-enable-language
```

## 核心参数（CLI）

- 路径：
  - `--game-dir`
  - `--launcher`
  - `--tools-dir`
  - `--cache-file`
- 语言：
  - `--language`（默认 `chinese`）
  - `--target-language`（默认 `Simplified Chinese`）
  - `--auto-enable-language` / `--no-auto-enable-language`
  - `--auto-language-file`
- 阶段控制：
  - `--skip-extract`
  - `--skip-decompile`
  - `--skip-template`
  - `--skip-mt`
- 解包反编译：
  - `--rpa-pattern`
  - `--extract-all-rpa`
  - `--unrpyc-no-init-offset`
  - `--unrpyc-clobber`
- 翻译行为：
  - `--provider {none,openai,deepl}`
  - `--batch-size`
  - `--sleep-seconds`
  - `--overwrite`
  - `--resume-untranslated` / `--no-resume-untranslated`
  - `--stop-on-quota` / `--no-stop-on-quota`
  - `--max-lines`
- OpenAI 兼容：
  - `--model`
  - `--base-url`
  - `--api-key`
  - `--temperature`
  - `--openai-format`
  - `--retries`
  - `--timeout`
- DeepL：
  - `--deepl-url`
  - `--deepl-auth-key`
  - `--deepl-target-lang`
  - `--deepl-source-lang`

## 运行后可能变更的文件

- `game/tl/<language>/*.rpy`
- `game/_renpy_translate_autolang.rpy`
- `game/_renpy_force_<language>.rpy`
- `game/options.rpy`（可能补 `config.language`、字体）
- `game/screens.rpy`（可能补语言按钮）
- `game/<cjk-font-file>`（可能复制字体）
- `.translation_cache.json`

## 常见问题

### 1) 设置页没有中文按钮

先运行一次“快速修复并启动”或对应 CLI 快速修复命令。  
若游戏脚本写死 `Language(None)`，流水线会尝试自动补按钮并给出日志警告。

### 2) 中文显示方块/乱码

通常是字体缺失。流水线会尝试注入 CJK 字体并修正默认字体；若本机没有可用字体，日志会出现 `[WARN]`。

### 3) 日志很多 WARN，是否失败？

以退出码与最后汇总为准。网络抖动类 WARN 可重试；如果退出码为 `0`，通常表示流程已完成。

## 日志

- 桌面端日志文件：`logs/app1.log`
- 流水线输出会实时显示在 UI“执行与日志”区域

## 打包（可选）

```bash
pyinstaller -n app1 -D --add-data "src/app1/res;res" -p src src/app1/app1.py
```

### Flet 打包 Windows（规避 Reparse Point）

在某些 Windows 文件系统下，`src/app1/app1.py` 可能是 reparse point，`flet build` 会在打包阶段报：
`Flet app package app/app.zip was not created`。

可使用仓库内脚本先做一次普通文件 staging，再调用 `flet build`：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build_flet_windows.ps1
```

常用参数：

- `-Output build/windows`：指定输出目录（默认 `build/windows`）
- `-KeepStage`：保留 staging 目录，便于排查
- `-SkipDevModeCheck`：跳过 Windows Developer Mode 前置检查
- `-VerboseBuild`：启用 `flet build -v` 日志

说明：该脚本使用 `git archive HEAD` 生成 staging 源码，因此默认只包含已提交内容。
另外，构建 Windows 桌面端前请先开启系统 `Developer Mode`（Flutter 插件需要 symlink 支持）。
脚本会自动尝试修复 `screen_brightness_windows` 已知 Windows 构建问题，并在命中后自动重试一次。
