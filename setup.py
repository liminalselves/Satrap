from __future__ import annotations

from pathlib import Path

from setuptools import find_packages, setup


ROOT = Path(__file__).parent


def read_readme() -> str:
    """读取项目说明"""
    readme = ROOT / "README.md"
    if not readme.exists():
        return ""
    return readme.read_text(encoding="utf-8")


install_requires = [
    "openai>=2.0.0",
    "colorlog>=6.7.0",
    "splintr-rs>=0.1.0",
    "numpy>=1.24.0",
    "pillow>=10.0.0",
    "msgpack>=1.0.5",
    "aiosqlite>=0.19.0",
    "beautifulsoup4>=4.12.0",
    "requests>=2.31.0",
    "aiohttp>=3.9.0",
    "websockets>=12.0",
    "aiocqhttp>=1.4.4",
    "aiofiles>=23.2.0",
    "pyyaml>=6.0.0",
]

extras_require = {
    "admin": [
        "streamlit>=1.36.0",
    ],
    "vector": [
        "faiss-cpu>=1.7.4",
    ],
}
extras_require["all"] = sorted({dep for deps in extras_require.values() for dep in deps})


setup(
    name="satrap",
    version="0.1.0",
    description="Python LLM tool library with context, tools, and workflow helpers",
    long_description=read_readme(),
    long_description_content_type="text/markdown",
    author="midway2333",
    url="https://github.com/midway2333/Satrap",
    license="GPL-3.0-only",
    packages=find_packages(include=["satrap", "satrap.*"]),
    include_package_data=True,
    package_data={
        "satrap": ["pages/*.py"],
    },
    python_requires=">=3.10",
    install_requires=install_requires,
    extras_require=extras_require,
    entry_points={
        "console_scripts": [
            "satrap=satrap.main:main",
        ],
    },
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
    project_urls={
        "Source": "https://github.com/midway2333/Satrap",
        "Issues": "https://github.com/midway2333/Satrap/issues",
    },
)
