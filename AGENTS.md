# AGENTS.md - Collie Project Guidelines

## 全局规则（Global Rules）

以下规则适用于所有与本项目相关的 Agent 交互，后续可根据需要新增或修改：

1. **语言要求**：所有回复请使用中文。

## Project Overview

**Collie** is a Python-based Android memory testing toolkit for automated app startup/residency testing and log analysis.

- **Language**: Python 3.7+
- **Package Name**: `collie_package`
- **Entry Point**: `collie` CLI command
- **Project Layout**: `src/` structure with setuptools packaging

## Build Commands

```bash
# Install dependencies
pip3 install -r requirements.txt

# Build wheel distribution
python setup.py bdist_wheel

# Install locally (editable mode)
pip3 install -e .

# Install from wheel
pip3 install --force-reinstall dist/collie-0.9.6-py3-none-any.whl

# Run without installing
python -m src.collie_package.cli
```

## Test Commands

**No test framework currently configured.** The project uses manual testing.

To add tests, create a `tests/` directory and use pytest:
```bash
# Install pytest
pip3 install pytest

# Run all tests
pytest

# Run single test file
pytest tests/test_module.py

# Run single test
pytest tests/test_module.py::test_function
```

## Code Style Guidelines

### Import Ordering
1. Standard library imports (builtins first)
2. Third-party imports (numpy, pandas, matplotlib)
3. Local package imports using relative imports: `from .. import state, tools`

```python
# Standard library
import os
import sys
import json
from datetime import datetime
from typing import List, Optional, Dict

# Third-party
import numpy as np
import pandas as pd

# Local imports - use relative for package modules
from .. import state, tools
from .pre_start import run_pre_start
```

### Naming Conventions
- **Functions**: `snake_case` - `def load_config_status()`
- **Classes**: `CamelCase` - `class ConsoleLogger:`
- **Constants**: `UPPER_CASE` - `DEFAULT_HIGHLIGHT_PROCESSES`
- **Private functions**: `_leading_underscore` - `def _lazy_call()`
- **Modules**: `snake_case.py` - `cont_startup_stay.py`

### Type Hints
Use type hints for function signatures (especially in newer code):
```python
def load_config_status(
    return_raw: bool = False,
    include_keys: Optional[List[str]] = None,
) -> List[str]:
```

### Formatting
- **Indentation**: 4 spaces (no tabs)
- **Line length**: ~100 characters (follow existing patterns)
- **String quotes**: Single quotes for internal strings, double for display text
- **Comments**: Chinese comments acceptable; docstrings in Chinese

### Error Handling
```python
try:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
except subprocess.TimeoutExpired:
    print(f"⚠️ 命令超时: {cmd}")
except Exception as e:
    print(f"⚠️ 命令异常: {cmd} -> {e}")
```

### State Management
Use the shared `state` module for global state:
```python
from .. import state
state.FILE_DIR = out_dir  # Set output directory
```

### ADB Command Patterns
```python
# Build ADB command with optional device
adb_cmd = ['adb']
if device_id:
    adb_cmd.extend(['-s', device_id])
adb_cmd.extend(['shell', 'command_here'])

# Execute with timeout
result = subprocess.run(adb_cmd, capture_output=True, text=True, timeout=20)
```

### File Operations
```python
# Always use UTF-8 encoding
with open(file_path, 'r', encoding='utf-8') as f:
    data = json.load(f)

# Create directories with exist_ok
os.makedirs(output_dir, exist_ok=True)
```

## Project Structure

```
src/collie_package/
├── __init__.py              # Package init (empty)
├── cli.py                   # Main CLI menu system
├── state.py                 # Global state (FILE_DIR)
├── tools.py                 # Shared utilities
├── log_class.py             # Logging/reporting classes
├── app_config.json          # App package configurations
├── automation/              # Automated testing scripts
│   ├── cont_startup_stay.py # Main startup test
│   ├── startup_runner.py
│   └── ...
├── log_tools/               # Log parsing utilities
│   ├── log_analyzer.py
│   ├── parse_logcat.py
│   └── ...
├── utilities/               # Helper utilities
│   ├── check_app.py
│   └── ...
└── memory_models/           # Memory testing modules
    └── ...
```

## Dependencies

Core dependencies (from `setup.py`):
- `numpy>=1.21`
- `pandas>=1.3`
- `matplotlib>=3.5`
- `openpyxl>=3.0`

## Configuration Files

The project uses JSON configuration in `app_config.json` for:
- App package lists (TOP 20, TOP 30, etc.)
- Test scenarios (九大场景-驻留)
- Highlight process lists
- Device-specific settings

## Notes for Agents

1. **No linting configured** - Follow existing code patterns manually
2. **No CI/CD** - Manual testing required
3. **Mixed language codebase** - Chinese comments, English code
4. **ADB dependency** - Requires Android device connected via ADB
5. **Lazy loading pattern** - Heavy imports deferred until menu selection
6. **Console output** - Uses custom `_print()` for centered terminal output

## Common Patterns

### Menu Action Registration
```python
def _lazy_call(target: str) -> MenuAction:
    """Delay heavy imports until action invoked."""
    def _runner() -> None:
        module_name, func_name = target.rsplit(".", 1)
        func = getattr(import_module(module_name), func_name)
        func()
    return _runner

# Usage in menu definition
MenuOption("1", "功能名称", action=_lazy_call("collie_package.module.function"))
```

### Logging to File and Console
```python
class ConsoleLogger:
    """Tee stdout/stderr to both terminal and file."""
    def __enter__(self):
        sys.stdout = self
        sys.stderr = self
        return self
    def write(self, data):
        self._stdout.write(data)
        self.file.write(data)
```
