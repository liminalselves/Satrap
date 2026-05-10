import logging
from os.path import dirname, abspath
import os
import colorlog 
import time
from datetime import datetime

class Logger():
    def __init__(
        self,
        logger_name: str,
        std_level=logging.INFO,
        file_level=logging.DEBUG,
        std_out: bool=True,
        file_out: bool=True,
        output_dir: str | None=None,
        file_name: str | None=None,
        max_log_days: int | None=None,
        max_file_lines: int | None=None,
    ) -> None:
        """日志类

        参数:
        - logger_name (str): 日志名称, 用于区分不同模块
        - std_level (logging level): 控制台输出日志级别, 默认为 INFO
        - file_level (logging level): 文件输出日志级别, 默认为 DEBUG
        - std_out (bool): 是否输出到控制台, 默认为 True
        - file_out (bool): 是否输出到文件, 默认为 True
        - output_dir (str | None): 输出目录, 为 None, 此时使用默认日志目录
        - file_name (str | None): 日志文件名, 默认为 None, 此时使用日期记录
        - max_log_days (int | None): 自动删除 max_log_days 天前的日志文件, 为 None 时不清理
        - max_file_lines (int | None): 日志文件只保留最后 max_file_lines 行, 为 None 时不截断
        """
        self.std_out = std_out
        self.file_out = file_out
        self.max_log_days = max_log_days
        self.max_file_lines = max_file_lines
        # 默认输出选项

        datefmt = "%Y-%m-%d %H:%M:%S"
        # 日期格式化, 年-月-日 时:分:秒

        std_logfmt = "[%(asctime)s.%(msecs)03d] [%(levelname)s]: %(log_color)s%(message)s"
        # 构建标准格式

        self.stdout_logger = logging.getLogger('{}_std'.format(logger_name))
        self.stdout_logger.setLevel(std_level)
        # 创建 logger 实例

        log_colors_config = {
            'DEBUG': 'cyan',
            'INFO': 'green',
            'WARNING': 'yellow',
            'ERROR': 'red',
            'CRITICAL': 'bold_red',
        }   # 日志颜色配置

        formatter = colorlog.ColoredFormatter(
            fmt=std_logfmt,
            datefmt=datefmt,
            log_colors=log_colors_config,
        )   # 彩色日志格式标准化

        sh = logging.StreamHandler()
        sh.setLevel(std_level)
        sh.setFormatter(formatter)
        self.stdout_logger.addHandler(sh)
        # 绑定 formatter, 按彩色格式输出

        file_logfmt = "[%(asctime)s.%(msecs)03d] [%(levelname)s]: %(message)s"
        # 去掉颜色字段

        self.file_logger = logging.getLogger('{}_file'.format(logger_name))
        self.file_logger.setLevel(file_level)
        # 创建文件专用 logger, 设置日志级别为 file_level

        if output_dir is not None:   # 指定项目根目录
            self.base_dir = os.path.join(output_dir, 'logs')   # 指定目录
        else:
            self.base_dir = os.path.join(dirname(dirname(abspath(__file__))), 'logs')   # 获取上级目录的绝对路径

        if not os.path.exists(self.base_dir):   # 检查目录是否存在, 不存在则创建
            os.makedirs(self.base_dir, exist_ok=True)

        if file_name is not None:   # 确定日志文件名
            self.log_file = file_name
        else:                       # 未指定文件名, 则使用日期记录
            self.log_file = os.path.join(self.base_dir, f"{logger_name}-{time.strftime('%Y%m%d')}.log")

        fh = logging.FileHandler(filename=self.log_file, mode='a', encoding='utf-8')
        fh.setLevel(file_level)
        # 创建文件处理器

        save_formatter =  logging.Formatter(
            fmt=file_logfmt,
            datefmt=datefmt,
            )
        fh.setFormatter(save_formatter)
        self.file_logger.addHandler(fh)
        # 绑定格式器并添加到 logger

        if self.max_log_days is not None and self.max_log_days > 0:
            self._cleanup_old_logs()
        if self.max_file_lines is not None and self.max_file_lines > 0:
            self._truncate_log_file()

    def info(self, message: str, std_out: bool | None=None, save_to_file: bool | None=None) -> None:
        """输出 INFO 日志
        参数:
        - message (str): 日志消息
        - std_out (bool): 是否输出到控制台
        - save_to_file (bool): 是否保存到文件
        """
        if std_out is None:
            std_out = self.std_out
        if save_to_file is None:
            save_to_file = self.file_out

        if std_out:
            self.stdout_logger.info(message)
        if save_to_file:
            self.file_logger.info(message)

    def debug(self, message: str, std_out: bool | None=None, save_to_file: bool | None=None) -> None:
        """输出 DEBUG 日志
        参数:
        - message (str): 日志消息
        - std_out (bool): 是否输出到控制台
        - save_to_file (bool): 是否保存到文件
        """
        if std_out is None:
            std_out = self.std_out
        if save_to_file is None:
            save_to_file = self.file_out

        if std_out:
            self.stdout_logger.debug(message)
        if save_to_file:
            self.file_logger.debug(message)

    def warning(self, message: str, std_out: bool | None=None, save_to_file: bool | None=None) -> None:
        """输出 WARNING 日志
        参数:
        - message (str): 日志消息
        - std_out (bool): 是否输出到控制台
        - save_to_file (bool): 是否保存到文件
        """
        if std_out is None:
            std_out = self.std_out
        if save_to_file is None:
            save_to_file = self.file_out

        if std_out:
            self.stdout_logger.warning(message)
        if save_to_file:
            self.file_logger.warning(message)

    def error(self, message: str, std_out: bool | None=None, save_to_file: bool | None=None) -> None:
        """输出 ERROR 日志
        参数:
        - message (str): 日志消息
        - std_out (bool): 是否输出到控制台
        - save_to_file (bool): 是否保存到文件
        """
        if std_out is None:
            std_out = self.std_out
        if save_to_file is None:
            save_to_file = self.file_out

        if std_out:
            self.stdout_logger.error(message)
        if save_to_file:
            self.file_logger.error(message)

    def critical(self, message: str, std_out: bool | None=None, save_to_file: bool | None=None) -> None:
        """输出 CRITICAL 日志
        参数:
        - message (str): 日志消息
        - std_out (bool): 是否输出到控制台
        - save_to_file (bool): 是否保存到文件
        """
        if std_out is None:
            std_out = self.std_out
        if save_to_file is None:
            save_to_file = self.file_out

        if std_out:
            self.stdout_logger.critical(message)
        if save_to_file:
            self.file_logger.critical(message)

    def _cleanup_old_logs(self) -> None:
        """删除超过 max_log_days 天未修改的日志文件"""
        max_days = self.max_log_days
        if max_days is None:
            return
        now = datetime.now().timestamp()
        cutoff = now - max_days * 86400

        if not os.path.exists(self.base_dir):
            return

        for filename in os.listdir(self.base_dir):
            if not filename.endswith('.log'):
                continue
            filepath = os.path.join(self.base_dir, filename)
            try:
                if os.path.getmtime(filepath) < cutoff:
                    os.remove(filepath)
            except OSError:
                pass

    def _truncate_log_file(self) -> None:
        """将当前日志文件截断, 只保留最后 max_file_lines 行"""
        log_path = self.log_file
        max_lines = self.max_file_lines
        if log_path is None or max_lines is None:
            return

        if not os.path.exists(log_path):
            return

        try:
            with open(log_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        except OSError:
            return

        if len(lines) <= max_lines:
            return

        try:
            with open(log_path, 'w', encoding='utf-8') as f:
                f.writelines(lines[-max_lines:])
        except OSError:
            pass

logger = Logger(logger_name="SATRAP", output_dir=".satrap")

if __name__ == "__main__":
    logger = Logger(
        logger_name="TEST",
        std_level=logging.DEBUG,
        file_level=logging.DEBUG,
        output_dir=None,
        file_name=None,
    )

    logger.info("This is an info message.", std_out=True, save_to_file=True)
    logger.debug("This is a debug message.", std_out=True, save_to_file=True)
    logger.warning("This is a warning message.", std_out=True, save_to_file=True)
    logger.error("This is an error message.", std_out=True, save_to_file=True)
    logger.critical("This is a critical message.", std_out=True, save_to_file=True)
