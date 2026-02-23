from typing import Union, Dict, Any
import subprocess, sys, os
import shutil

from satrap.core.log import logger

class CodeSandbox:
    """代码沙箱执行器, 在指定目录和 Python 环境中运行代码"""
    def __init__(self, sandbox_path: str, env: str):
        """
        初始化沙箱

        参数:
        - sandbox_path: 沙箱根目录 (绝对或相对路径)
        - env: Python 解释器路径 (例如 '/usr/bin/python3' 或虚拟环境中的 python)
        """
        self.sandbox_path = os.path.abspath(sandbox_path)
        self.python_executable = env
        # 确保沙箱目录存在

        os.makedirs(self.sandbox_path, exist_ok=True)

    def _safe_join(self, *paths: str) -> str:
        """
        安全地将路径连接到沙箱根目录, 防止路径遍历攻击
        """
        abs_path = os.path.abspath(os.path.join(self.sandbox_path, *paths))   # 将输入路径拼接成绝对路径
        return abs_path

    def _run_python(self, args: list, cwd: str | None = None) -> Dict[str, Any]:
        """
        执行 Python 命令; 返回包含 stdout, stderr 和返回码的字典

        参数:
        - args: Python 命令参数列表 (例如 ['-c', 'print("Hello, World!")'])
        - cwd: 工作目录, 默认为沙箱根目录

        返回:
        - 包含 stdout, stderr, returncode 的字典
        """
        try:
            result = subprocess.run(
                [self.python_executable] + args,
                cwd=cwd or self.sandbox_path,
                capture_output=True,
                text=True,
                check=False
            )
            return {
                'stdout': result.stdout,
                'stderr': result.stderr,
                'returncode': result.returncode
            }
        except FileNotFoundError:
            logger.error(f"[代码沙箱] Python 解释器未找到: {self.python_executable}")
            return {
                'stdout': '',
                'stderr': f"Python 解释器未找到: {self.python_executable}",
                'returncode': -1
            }
        except Exception as e:
            logger.error(f"[代码沙箱] 执行时发生未知错误: {str(e)}")
            return {
                'stdout': '',
                'stderr': f"执行时发生未知错误: {str(e)}",
                'returncode': -2
            }

    def run(self, code: str) -> Dict[str, Any]:
        """
        执行传入的代码字符串, 返回执行结果

        参数:
        - code: Python 代码字符串

        返回:
        - 包含 stdout, stderr, returncode 的字典
        """
        return self._run_python(['-c', code])

    def run_file(self, path: str) -> Dict[str, Any]:
        """
        执行沙箱内的指定文件, 返回执行结果

        参数:
        - path: 相对于沙箱根目录的文件路径

        返回:
        - 包含 stdout, stderr, returncode 的字典
        """
        abs_path = os.path.join(self.sandbox_path, path)
        if not os.path.isfile(abs_path):
            return {
                'stdout': '',
                'stderr': f"文件不存在: {abs_path}",
                'returncode': -3
            }
        return self._run_python([abs_path])

    def save_to_file(self, code: str, path: str) -> None:
        """
        将代码保存到沙箱内的指定文件, 支持自动创建父目录

        参数:
        - code: 要保存的代码字符串
        - path: 相对于沙箱根目录的文件路径
        """
        abs_path = os.path.join(self.sandbox_path, path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)   # 确保父目录存在
        with open(abs_path, 'w', encoding='utf-8') as f:
            f.write(code)

    def delete_file(self, path: str) -> None:
        """删除沙箱内的指定文件
        
        参数:
        - path: 相对于沙箱根目录的文件路径
        """
        abs_path = self._safe_join(path)
        if not os.path.exists(abs_path):
            logger.error(f"[代码沙箱] 删除文件不存在: {abs_path}")
        if os.path.isdir(abs_path):
            logger.error(f"[代码沙箱] 路径是目录, 请使用删除目录的方法: {abs_path}")
        os.remove(abs_path)

    def delete_directory(self, path: str, recursive: bool = True) -> None:
        """
        删除沙箱内的目录

        参数:
        - path: 相对于沙箱根目录的目录路径
        - recursive: 是否递归删除非空目录, 如果为 False, 只能删除空目录; 为 True 则删除整个目录树
        """
        abs_path = self._safe_join(path)
        if not os.path.exists(abs_path):
            logger.error(f"[代码沙箱] 删除目录不存在: {abs_path}")
        if not os.path.isdir(abs_path):
            logger.error(f"[代码沙箱] 删除路径不是目录: {abs_path}")

        if recursive:
            shutil.rmtree(abs_path)
            # 递归删除整个目录树
        else:
            os.rmdir(abs_path)
            # 只删除空目录

    def list_files(self, subdir: str = '') -> list[str]:
        """
        列出沙箱内指定子目录下的所有文件, 返回相对于沙箱根目录的路径列表
        返回的路径已包含文件所在的子目录信息

        参数:
        - subdir: 相对于沙箱根目录的子目录路径, 为空则从根目录开始

        返回:
        - 文件相对路径列表
        """
        if subdir:   # 确定要遍历的绝对路径
            search_path = self._safe_join(subdir)
            if not os.path.isdir(search_path):
                logger.error(f"[代码沙箱] 列出文件子目录不是目录: {search_path}")
        else:
            search_path = self.sandbox_path

        file_list = []
     
        for root, dirs, files in os.walk(search_path):   # 使用 os.walk 递归遍历目录
            rel_root = os.path.relpath(root, self.sandbox_path)   # 计算相对于沙箱根目录的路径
            for f in files:   # 如果 rel_root 是 '.', 表示根目录, 此时文件相对路径应直接是文件名
                if rel_root == '.':
                    rel_path = f
                else:
                    rel_path = os.path.join(rel_root, f)
                file_list.append(rel_path)
        return file_list
