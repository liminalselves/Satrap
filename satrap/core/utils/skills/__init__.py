"""技能加载与同步异步激活的兼容入口"""

from __future__ import annotations
from collections.abc import Iterable
import importlib.util
import inspect
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union, cast
from typing import Protocol
import yaml
import os
import re
from satrap.core.log import logger
from satrap.core.type import safe_getattr, safe_getattr_callable, safe_getattr_dict
from satrap.core.framework.Base import (
    ModelWorkflowFramework,
    AsyncModelWorkflowFramework,
)
from satrap.core.utils.TCBuilder import (
    AsyncTool,
    Tool,
    ToolsManager,
    AsyncToolsManager,
    create_tool_defined,
)
from satrap.core.utils.context import AsyncContextManager, ContextManager
from .utils import (
    _YamlLoader,
    _yaml_loader,
    SkillWorkflowProtocol,
    SKILLS_PRESET_DIR,
    DEFAULT_USER_SKILLS_DIR,
    _FRONT_MATTER_RE,
    _SKILL_TOOLS_MODULE_COUNTER,
    _parse_front_matter,
    _load_skill_tools,
)
from .skill import Skill
from .manager import SkillsManager
from .tool import SkillTool

__all__ = [
    "Skill",
    "SkillsManager",
    "SkillTool",
    "SKILLS_PRESET_DIR",
]
