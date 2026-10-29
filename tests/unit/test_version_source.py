"""版本号只在 satrap/_version.py 维护, 其余位置读取或与之一致"""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import satrap
from satrap._version import __version__
from satrap.core.telemetry import health
from satrap.edictum.plugin_compatibility import framework_version

ROOT = Path(__file__).resolve().parents[2]


def test_runtime_readers_use_the_single_source():
    assert satrap.__version__ == __version__
    assert framework_version() == __version__
    health._build_identity.cache_clear()
    try:
        assert health._build_identity()[0] == __version__
    finally:
        health._build_identity.cache_clear()


def test_package_reads_version_instead_of_hard_coding():
    from setuptools.config.pyprojecttoml import read_configuration

    config_path = ROOT / "pyproject.toml"
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert "version" in config["project"]["dynamic"]
    assert "version" not in config["project"]
    assert config["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "satrap._version.__version__"}
    assert read_configuration(config_path)["project"]["version"] == __version__
    extras = config["project"]["optional-dependencies"]
    assert set(extras["all"]) == {dependency for name, dependencies in extras.items() if name != "all" for dependency in dependencies}


def test_frontend_package_matches_and_ui_has_no_literal_version():
    package = json.loads((ROOT / "satrap-ui" / "package.json").read_text(encoding="utf-8"))
    assert package["version"] == __version__
    about = (ROOT / "satrap-ui" / "src" / "pages" / "Settings" / "AboutSection.tsx").read_text(encoding="utf-8")
    assert "__APP_VERSION__" in about
    assert not re.search(r"""['"]\d+\.\d+\.\d+['"]""", about)
