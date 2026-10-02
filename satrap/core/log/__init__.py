"""
Satrap 统一日志配置与输出接口

控制台日志同步到实时流, 文件日志按日期和进程隔离,
文件故障由独立诊断通道记录, 不中断调用日志的业务
"""
import traceback
import colorlog
import logging
from pathlib import Path
import atexit

from satrap.core.log.managed import ManagedDailyHandler, cleanup_logs, log_root, report_failure
from satrap.core.log.policy import LogMaintenance, LoggingPolicyStore
from satrap.core.log.stream import StandardLogStream, StandardStreamCapture, standard_log_stream


class StandardLogHandler(logging.Handler):
    """将控制台日志同步到进程内标准日志实时流"""

    def __init__(self, stream: StandardLogStream) -> None:
        """
        初始化标准流处理器

        参数:
        - stream: 标准日志实时流
        """
        super().__init__()
        self._stream = stream

    def emit(self, record: logging.LogRecord) -> None:
        """
        发布一条控制台日志

        参数:
        - record: Python 日志记录
        """
        try:
            content = self.format(record)
            for line in content.splitlines() or (content,):
                self._stream.publish(line, record.levelname)
        except Exception as error:
            report_failure(f"实时日志发布失败: {error}\n{traceback.format_exc()}")


class Logger:
    """统一业务日志接口, 独立管理输出句柄"""

    def __init__(
        self,
        logger_name: str,
        std_level: int = logging.INFO,
        file_level: int = logging.DEBUG,
        std_out: bool = True,
        file_out: bool = True,
        output_dir: str | None = None,
        file_name: str | None = None,
        max_log_days: int | None = None,
        max_file_lines: int | None = None,
    ) -> None:
        """
        初始化控制台和文件输出

        参数:
        - logger_name: 日志名称
        - std_level: 控制台级别, 默认 INFO
        - file_level: 文件级别, 默认 DEBUG
        - std_out: 默认是否输出到控制台
        - file_out: 默认是否输出到文件
        - output_dir: 兼容自定义输出根目录, 默认稳定的项目数据目录
        - file_name: 兼容显式文件路径, 默认按日期和进程创建文件
        - max_log_days: 兼容启动时手动指定的保留期限, 默认使用独立策略
        - max_file_lines: 已弃用, 完整日志由日期保留策略管理, 不截断活动文件
        """
        self.std_out, self.file_out = std_out, file_out
        self.max_log_days, self.max_file_lines = max_log_days, max_file_lines
        self._closed = False
        self.maintenance: LogMaintenance | None = None
        self.stdout_logger = logging.Logger(f"{logger_name}_std", std_level)
        self.file_logger = logging.Logger(f"{logger_name}_file", file_level)
        self.stdout_logger.parent = logging.getLogger()
        self.file_logger.parent = logging.getLogger()
        # 保留根日志处理器和测试捕获的传播, 实例文件句柄仍独立
        datefmt = "%Y-%m-%d %H:%M:%S"
        plain_format = "[%(asctime)s.%(msecs)03d] [%(levelname)s]: %(message)s"
        handler = logging.StreamHandler()
        handler.setLevel(std_level)
        handler.setFormatter(colorlog.ColoredFormatter(
            fmt="[%(asctime)s.%(msecs)03d] [%(levelname)s]: %(log_color)s%(message)s",
            datefmt=datefmt,
            log_colors={"DEBUG": "cyan", "INFO": "green", "WARNING": "yellow", "ERROR": "red", "CRITICAL": "bold_red"},
        ))
        self.stdout_logger.addHandler(handler)
        if not isinstance(handler.stream, StandardStreamCapture):
            stream_handler = StandardLogHandler(standard_log_stream)
            stream_handler.setLevel(std_level)
            stream_handler.setFormatter(logging.Formatter(plain_format, datefmt))
            self.stdout_logger.addHandler(stream_handler)
        self.file_handler: ManagedDailyHandler | None = None
        self.base_dir = ""
        try:
            explicit_path = Path(file_name).resolve() if file_name else None
            root = explicit_path.parent if explicit_path else (Path(output_dir) / "logs" if output_dir else log_root())
            self.base_dir = str(root.resolve())
            self.file_handler = ManagedDailyHandler(root, file_path=explicit_path)
            self.file_handler.setLevel(file_level)
            self.file_handler.setFormatter(logging.Formatter(plain_format, datefmt))
            self.file_logger.addHandler(self.file_handler)
            if max_log_days is not None:
                self._cleanup_old_logs()
            if file_name is None and max_log_days is None:
                self.maintenance = LogMaintenance(self.file_handler, LoggingPolicyStore(root=root))
                self.maintenance.start()
        except Exception as error:
            report_failure(f"日志文件或维护配置初始化失败: {error}\n{traceback.format_exc()}")
            if self.file_handler is None:
                self.file_logger.addHandler(logging.NullHandler())
        if max_file_lines is not None:
            report_failure("max_file_lines 已弃用, 保留完整日志并使用按日期清理")
        atexit.register(self.close)

    @property
    def log_file(self) -> str | None:
        """
        获取当前文件路径

        返回:
        - 已打开文件的绝对路径, 尚未写入时返回 None
        """
        path = self.file_handler.current_file if self.file_handler else None
        return str(path) if path else None

    def set_service(self, service: str) -> None:
        """
        设置当前进程的日志服务标签

        参数:
        - service: 可扩展的服务名称
        """
        if self.file_handler is not None:
            self.file_handler.set_service(service)

    def close(self) -> None:
        """幂等关闭当前实例的全部日志句柄"""
        if self._closed:
            return
        self._closed = True
        if self.maintenance is not None:
            self.maintenance.close()
        for output in (self.file_logger, self.stdout_logger):
            for handler in tuple(output.handlers):
                output.removeHandler(handler)
                try:
                    handler.close()
                except Exception as error:
                    report_failure(f"日志处理器关闭失败: {error}\n{traceback.format_exc()}")
        atexit.unregister(self.close)

    def info(self, message: str, std_out: bool | None=None, save_to_file: bool | None=None) -> None:
        """
        输出 INFO 日志
        参数:
        - message: 日志消息
        - std_out: 是否输出到控制台
        - save_to_file: 是否保存到文件
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
        """
        输出 DEBUG 日志
        参数:
        - message: 日志消息
        - std_out: 是否输出到控制台
        - save_to_file: 是否保存到文件
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
        """
        输出 WARNING 日志
        参数:
        - message: 日志消息
        - std_out: 是否输出到控制台
        - save_to_file: 是否保存到文件
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
        """
        输出 ERROR 日志
        参数:
        - message: 日志消息
        - std_out: 是否输出到控制台
        - save_to_file: 是否保存到文件
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
        """
        输出 CRITICAL 日志
        参数:
        - message: 日志消息
        - std_out: 是否输出到控制台
        - save_to_file: 是否保存到文件
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
        """兼容显式保留期限, 清理失败不会中断初始化"""
        if self.max_log_days is None:
            return
        try:
            cleanup_logs(Path(self.base_dir), self.max_log_days)
        except Exception as error:
            report_failure(f"启动清理失败: {error}\n{traceback.format_exc()}")


logger = Logger(logger_name="SATRAP", file_level=logging.WARNING, std_level=logging.DEBUG)
