# py-uv-config-log-example

[English](./README.md) | [简体中文](./README_CN.md)

使用 [uv](https://docs.astral.sh/uv/) 管理依赖，使用 [PyYAML](https://pyyaml.org/) 读取 YAML 配置，基于 [logging](https://docs.python.org/3/library/logging.html) 输出并轮转日志，再结合 [APScheduler](https://apscheduler.readthedocs.io/) 和 [py7zr](https://py7zr.readthedocs.io/) 对归档日志进行压缩清理。

## 快速开始（首次运行）

```shell
# 创建项目虚拟环境
uv venv

# 按锁文件同步依赖
uv sync

# 以可编辑模式安装当前项目（提供 app1/common 导入）
uv pip install -e .

# 运行应用模块
uv run python -m app1.app1


# --- 激活虚拟环境 ---
# Windows
.venv\Scripts\activate.bat
# Linux / MacOS
source .venv/bin/activate

# --- 退出虚拟环境 ---
# Windows
.venv\Scripts\deactivate.bat
# Linux / MacOS
deactivate
```

说明：

- `uv run` 无需手动激活 `.venv`。
- 后续如果再次执行 `uv sync`，请再执行一次 `uv pip install -e .`。

## 日常运行

```shell
uv run python -m app1.app1
```

可选的一次性运行方式（不持久安装 editable）：

```shell
uv run --with-editable . python -m app1.app1
```

## 在 PyCharm 中使用

一次设置后可长期复用：

1. Interpreter：选择项目 `.venv`（uv 创建的虚拟环境）。
2. 在 Project 视图中将 `src` 标记为 `Sources Root`。
3. 新建 Run Configuration：
   - Type：Python
   - Run：`Module name`
   - Module name：`app1.app1`
   - Working directory：项目根目录
4. 保存该配置（可选设为 shared）。

如果出现 `ModuleNotFoundError: No module named 'app1'` 或 `'common'`：

```shell
uv pip install -e .
```

## 打包 EXE

首次构建：

- `-F` 单文件，`-D` 单目录
- `-n` EXE 文件名
- `--add-data` 添加资源文件
- `-p` 添加模块搜索路径（`sys.path`）

```shell
pyinstaller -n app1 -D --add-data "src/app1/res;res" -p src src/app1/app1.py
```

通过 `.spec` 构建：

- `--noconfirm`无需确认是否覆盖上次构建的文件

```shell
pyinstaller app1.spec --noconfirm
```

运行 EXE：

```shell
app1.exe --config _internal\res\config.yml
```
