# py-uv-config-log-example

[English](./README.md) | [简体中文](./README_CN.md)

Use [uv](https://docs.astral.sh/uv/) to manage dependencies, load YAML configuration via [PyYAML](https://pyyaml.org/), generate rotating logs with [logging](https://docs.python.org/3/library/logging.html), and compress archived logs using [APScheduler](https://apscheduler.readthedocs.io/) and [py7zr](https://py7zr.readthedocs.io/).

## Quick Start (First Run)

```shell
# Create project virtual environment
uv venv

# Sync locked dependencies
uv sync

# Install current project in editable mode (provides app1/common imports)
uv pip install -e .

# Run app module
uv run python -m app1.app1


# --- Activate the virtual environment ---
# Windows
.venv\Scripts\activate.bat
# Linux / MacOS
source .venv/bin/activate

# --- Deactivate the virtual environment ---
# Windows
.venv\Scripts\deactivate.bat
# Linux / MacOS
deactivate
```

Notes:

- `uv run` does not require manually activating `.venv`.
- If you run `uv sync` again later, run `uv pip install -e .` again.

## Daily Run

```shell
uv run python -m app1.app1
```

Optional one-off command (without persisting editable install):

```shell
uv run --with-editable . python -m app1.app1
```

## Usage in PyCharm

Set once, then reuse:

1. Interpreter: select project `.venv` (uv-created environment).
2. Mark `src` as `Sources Root` in Project view.
3. Run Configuration:
   - Type: Python
   - Run: `Module name`
   - Module name: `app1.app1`
   - Working directory: project root
4. Save the run configuration (optionally as shared).

If you see `ModuleNotFoundError: No module named 'app1'` or `'common'`:

```shell
uv pip install -e .
```

## Packaging EXE

Initial build:

- `-F` single-file executable, `-D` single-directory executable
- `-n` executable name
- `--add-data` include resource files
- `-p` append search path to `sys.path`

```shell
pyinstaller -n app1 -D --add-data "src/app1/res;res" -p src src/app1/app1.py
```

Build with `.spec`:

- `--noconfirm` No need to confirm whether to overwrite the last built file

```shell
pyinstaller app1.spec --noconfirm
```

Run EXE:

```shell
app1.exe --config _internal\res\config.yml
```
