"""
模型源码与已登记业务库的应用层执行策略

统一公开模块导航, 受限编译, 文件路径和局部 API 参数检查
配合 worker 内的网络与进程守卫防止意外越界, 不提供内核级恶意代码隔离
"""

from __future__ import annotations

import ast
import builtins
from collections.abc import Callable, Iterable
import importlib
import inspect
import io
import mimetypes
import operator
import os
from pathlib import Path
import sys
import subprocess
from types import ModuleType
from typing import IO, NoReturn, Protocol, cast
from zipfile import ZipFile

try:
    from .models import Violations, allowed_modules
except ImportError:
    from models import Violations, allowed_modules
# 同一策略支持包内导入和 worker 的可信脚本入口, 不要求加载 Satrap 应用


_PUBLIC_EXPORTS = {
    "numpy": frozenset((
        "array asarray asanyarray arange linspace logspace geomspace zeros ones empty full zeros_like ones_like empty_like full_like "
        "eye identity diag diagonal tri tril triu copy reshape resize ravel flatten transpose swapaxes moveaxis rollaxis squeeze expand_dims "
        "concatenate stack hstack vstack dstack column_stack row_stack split array_split hsplit vsplit dsplit repeat tile roll flip fliplr flipud rot90 "
        "sum prod mean median std var min max amin amax argmin argmax ptp percentile quantile nanmean nansum nanmin nanmax nanstd nanvar "
        "cumsum cumprod diff gradient abs absolute sqrt cbrt square power exp exp2 expm1 log log2 log10 log1p sin cos tan arcsin arccos arctan arctan2 "
        "sinh cosh tanh degrees radians floor ceil round rint trunc sign maximum minimum clip add subtract multiply divide true_divide floor_divide remainder mod "
        "dot matmul vdot inner outer tensordot einsum cross convolve correlate corrcoef cov histogram histogram2d bincount unique sort argsort searchsorted "
        "where select nonzero count_nonzero argwhere isfinite isinf isnan isclose allclose equal greater less logical_and logical_or logical_not all any "
        "meshgrid indices take put choose compress extract broadcast_to broadcast_arrays fromiter fromfunction vectorize piecewise atleast_1d atleast_2d atleast_3d "
        "save load savez savez_compressed savetxt loadtxt genfromtxt fromfile dtype ndarray int8 int16 int32 int64 uint8 uint16 uint32 uint64 float16 float32 float64 "
        "complex64 complex128 bool_ integer floating number generic int_ uint str_ bytes_ object_ nan inf pi e newaxis"
    ).split()),
    "pandas": frozenset((
        "DataFrame Series Index RangeIndex MultiIndex DatetimeIndex TimedeltaIndex Categorical CategoricalDtype Timestamp Timedelta Period NA NaT "
        "concat merge merge_asof merge_ordered crosstab pivot pivot_table melt wide_to_long get_dummies cut qcut factorize unique to_datetime to_numeric "
        "to_timedelta date_range period_range timedelta_range interval_range isna isnull notna notnull read_csv read_json read_excel"
    ).split()),
    "matplotlib.pyplot": frozenset((
        "figure subplots subplot subplot_mosaic axes gca gcf plot scatter bar barh hist hist2d boxplot violinplot pie imshow matshow pcolor pcolormesh contour contourf "
        "errorbar fill fill_between stackplot step stairs stem hlines vlines axhline axvline axhspan axvspan text annotate title suptitle xlabel ylabel xlim ylim "
        "xticks yticks legend grid tight_layout subplots_adjust xscale yscale axis colorbar table savefig close cla clf get_fignums get_figlabels imread imsave "
        "rcParams rc_context rc rcdefaults get_backend"
    ).split()),
    "docx": frozenset({"Document"}),
    "pptx": frozenset({"Presentation"}),
    "openpyxl": frozenset({"Workbook", "load_workbook"}),
    "pypdf": frozenset({"PdfReader", "PdfWriter"}),
    "pdfplumber": frozenset({"open"}),
    "reportlab.pdfgen.canvas": frozenset({"Canvas"}),
    "reportlab.pdfbase.ttfonts": frozenset({"TTFont"}),
    "reportlab.pdfbase.pdfmetrics": frozenset({"registerFont", "registerFontFamily", "getFont", "getRegisteredFontNames", "stringWidth"}),
    "reportlab.platypus": frozenset((
        "SimpleDocTemplate BaseDocTemplate Paragraph Spacer Image Table LongTable TableStyle PageBreak CondPageBreak KeepTogether "
        "KeepInFrame Frame PageTemplate NextPageTemplate ListFlowable ListItem HRFlowable"
    ).split()),
}


class FilePolicy:
    """模型路径仅限业务根, 库运行时另允许固定只读资源"""

    def __init__(self, root: Path, temporary: Path, violations: Violations):
        """
        固定业务目录并登记依赖读取所需的只读运行时资源

        参数:
        - root: 模型业务根目录
        - temporary: 本次 worker 私有临时目录, 可供业务读写
        - violations: 本次 worker 共用的首次违规记录
        """
        # Step.1 固定业务根与私有临时根, 模型 open 只访问这两类目录
        self.root = root.resolve()
        self.temporary = temporary.resolve()
        self.violations = violations
        self.original_open = builtins.open
        # Step.2 保留解释器与依赖路径, 包括尚不存在的标准库 ZIP 路径
        self.runtime = tuple(Path(value).resolve() for value in sys.path if value)
        # Step.3 登记字体, 时区与 MIME 资源, 库内部只能读取这些固定位置
        font_paths = (Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts", Path("/usr/share/fonts"),
                      Path("/usr/local/share/fonts"), Path("/System/Library/Fonts"), Path("/Library/Fonts"))
        if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
            font_paths += (Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "Windows" / "Fonts",)
        self.runtime += tuple(path.resolve() for path in font_paths if path.is_dir())
        self.runtime += tuple(path.resolve() for path in (Path("/usr/share/zoneinfo"), Path("/etc/localtime"), Path("/etc/timezone")) if path.exists())
        self.runtime += tuple(Path(value).resolve() for value in mimetypes.knownfiles if Path(value).is_file())

    def path(self, value: object, *, write: bool = False, runtime: bool = False) -> Path:
        """
        解析路径并分别约束业务读写和库运行时读取

        参数:
        - value: 字符串或 PathLike, 不接受 URL, UNC 或整数描述符
        - write: 默认 False, True 时不允许使用只读运行时目录
        - runtime: 默认 False, 仅库内部读取可设 True 增加固定运行时目录

        返回:
        - 解析后的绝对路径; 格式, Windows 设备路径或范围不合法时记录并抛出策略异常
        """
        # Step.1 拒绝远程协议, 借用描述符和不支持的路径格式
        if not isinstance(value, (str, os.PathLike)):
            self.violations.reject("argument_not_allowed", "文件参数必须是沙箱路径或受控文件对象")
        raw = os.fspath(cast(str | os.PathLike[str], value))
        if not isinstance(raw, str) or "://" in raw or raw.startswith(("\\\\", "//")) or (os.name == "nt" and ":" in raw[2:]):
            self.violations.reject("path_outside_sandbox", "路径越出沙箱范围或使用不支持的路径格式")
        # Step.2 兼容 Windows 版本差异并拒绝设备名和含歧义的路径段
        if os.name == "nt":
            from pathlib import PureWindowsPath
            reserved = getattr(os.path, "isreserved", lambda part: PureWindowsPath(part).is_reserved())
            if any(reserved(part) or part.endswith((".", " "))
                   for part in Path(raw).parts if part not in {".", ".."}):
                self.violations.reject("path_outside_sandbox", "沙箱不支持 Windows 设备或含歧义的路径")
        # Step.3 解析链接后的绝对路径, 写操作不获得运行时只读例外
        candidate = Path(raw)
        candidate = (candidate if candidate.is_absolute() else self.root / candidate).resolve()
        roots = (self.root, self.temporary) + (self.runtime if runtime and not write else ())
        if not any(candidate.is_relative_to(root) for root in roots):
            self.violations.reject("path_outside_sandbox", f"路径越出沙箱范围: {candidate}")
        return candidate

    def argument(self, value: object, *, write: bool = False) -> object:
        """
        将库文件参数规范化为路径或已检查的文件对象

        参数:
        - value: 路径, 内存流或具有可验证文件名的 IOBase, None 原样保留
        - write: 默认 False, 写入参数必须显式设 True

        返回:
        - 绝对路径字符串, 原内存流或受检文件对象; 不支持的参数与越界路径抛出策略异常
        """
        if value is None:
            return value
        if isinstance(value, (io.StringIO, io.BytesIO)):
            return value
        if isinstance(value, io.IOBase):
            self.path(getattr(value, "name", None), write=write)
            return value
        return str(self.path(value, write=write))

    def open(self, file: object, mode: str = "r", buffering: int = -1, encoding: str | None = None,
             errors: str | None = None, newline: str | None = None, closefd: bool = True,
             opener: Callable[[str, int], int] | None = None) -> IO[str] | IO[bytes]:
        """
        提供只允许业务路径的模型 open 入口

        参数:
        - file: 沙箱内目标路径, 不接受整数文件描述符
        - mode: 打开模式, 默认 r, 含 w/a/x/+ 时按写入校验
        - buffering: 缓冲配置, 默认 -1 沿用 Python 行为
        - encoding: 文本编码, 默认 None 沿用解释器设置
        - errors: 编解码错误策略, 默认 None
        - newline: 文本换行策略, 默认 None
        - closefd: 默认 True, 模型不能通过 False 借用已有描述符
        - opener: 默认 None, 模型不允许自定义 opener

        返回:
        - 按模式返回文本或二进制文件流; 不允许的参数抛出策略异常, 文件错误继续传播
        """
        if not closefd or opener is not None:
            self.violations.reject("argument_not_allowed", "沙箱不允许自定义文件描述符或 opener")
        target = self.path(file, write=any(flag in mode for flag in "wax+"))
        return self.original_open(target, mode, buffering, encoding, errors, newline, closefd, opener)

    def install_library_io(self) -> None:
        """
        包装库内部 Python 文件入口, 保留只读运行时兼容

        只在当前 worker 修改 builtins/io/os, 模型 open 仍使用更严格的独立入口
        """
        original_io_open = io.open
        original_os_open = os.open

        def checked_open(file: object, mode: str = "r", buffering: int = -1, encoding: str | None = None,
                         errors: str | None = None, newline: str | None = None, closefd: bool = True,
                         opener: Callable[[str, int], int] | None = None) -> IO[str] | IO[bytes]:
            """
            校验库内部读写范围后调用原 io.open

            参数:
            - file: 业务路径或固定只读运行时路径
            - mode: 打开模式, 默认 r, 含 w/a/x/+ 时按写入校验
            - buffering: 缓冲配置, 默认 -1 沿用 Python 行为
            - encoding: 文本编码, 默认 None 沿用解释器设置
            - errors: 编解码错误策略, 默认 None
            - newline: 文本换行策略, 默认 None
            - closefd: 默认 True, 保持库内部原生打开接口的调用形态
            - opener: 默认 None, 可供可信库临时文件使用, 底层 os.open 仍检查路径

            返回:
            - 文本或二进制流; 写入运行时资源或越界路径抛出策略异常
            """
            target = self.path(file, write=any(flag in mode for flag in "wax+"), runtime=True)
            # 模型 open 单独拒绝 opener; 库内临时文件仍经过路径和 os.open 校验
            return original_io_open(target, mode, buffering, encoding, errors, newline, closefd, opener)

        def checked_os_open(file: object, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
            """
            检查库底层打开标志并拒绝借用目录描述符

            参数:
            - file: 业务或只读运行时路径
            - flags: os.open 标志, 写入相关位决定路径权限
            - mode: 创建文件的权限位, 默认 0o777
            - dir_fd: 默认 None, 不支持以目录描述符绕开根目录

            返回:
            - 原 os.open 返回的文件描述符; 参数或路径越界时抛出策略异常
            """
            if dir_fd is not None:
                self.violations.reject("argument_not_allowed", "沙箱不允许借用目录描述符")
            writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            return original_os_open(self.path(file, write=writing, runtime=True), flags, mode)

        builtins.open = checked_open
        io.open = checked_open
        os.open = checked_os_open

    def inspect_document(self, value: object) -> None:
        """
        限制文档输入大小与 OOXML 展开规模

        参数:
        - value: 待检查路径, 非路径对象跳过, 不存在的文件交给实际库报错

        普通输入不超过 64 MiB, OOXML 不超过 10000 项和 64 MiB 展开大小, 超限记录策略异常
        """
        if not isinstance(value, (str, os.PathLike)):
            return
        path = self.path(value)
        if path.exists() and path.is_file() and path.stat().st_size > 64 * 1024 * 1024:
            self.violations.reject("argument_not_allowed", "文档/数据输入超过 64 MiB")
        if path.suffix.lower() not in {".docx", ".pptx", ".xlsx"} or not path.exists():
            return
        with ZipFile(path) as archive:
            items = archive.infolist()
            if len(items) > 10_000 or sum(item.file_size for item in items) > 64 * 1024 * 1024:
                self.violations.reject("argument_not_allowed", "文档展开规模超过沙箱限制")


class ModuleView:
    """模块导航仅开放生效白名单和登记成员"""

    def __init__(self, policy: PythonPolicy, name: str):
        """
        绑定公开模块导航与当前执行策略

        参数:
        - policy: 当前 worker 的公开模块与属性策略
        - name: 完整模块名或仅用于导航的父模块名
        """
        self._policy = policy
        self._name = name

    def __getattr__(self, name: str) -> object:
        """
        通过统一属性守卫解析模块成员

        参数:
        - name: 待访问的公开成员名

        返回:
        - 受控子模块, API 包装或普通成员; 私有属性抛出 AttributeError, 未开放入口抛出策略异常
        """
        if name.startswith("_"):
            raise AttributeError(name)
        return self._policy.attribute(self, name)


class GuardedCall:
    """对登记 API 的普通别名调用保留相同参数检查"""

    def __init__(self, policy: PythonPolicy, target: Callable[..., object]):
        """
        保存函数身份, 让普通别名调用继续执行相同检查

        参数:
        - policy: 当前 worker 的 API 参数策略
        - target: 已经登记的底层可调用对象
        """
        self._policy = policy
        self._target = target

    def __call__(self, *args: object, **kwargs: object) -> object:
        """
        在统一参数检查后调用登记对象

        参数:
        - args: 原调用的位置参数
        - kwargs: 原调用的关键字参数

        返回:
        - 底层调用结果; 不允许的 API 或参数抛出策略异常
        """
        return self._policy.call(self._target, args, kwargs)


class MutableItems(Protocol):
    """数组, 表格索引器和容器的共同写入接口"""

    def __setitem__(self, key: object, value: object) -> None:
        """
        声明容器和索引器的受控写入协议

        参数:
        - key: 目标索引或切片
        - value: 待写入的业务值
        """
        ...

    def __delitem__(self, key: object) -> None:
        """
        声明容器和索引器的受控删除协议

        参数:
        - key: 目标索引或切片
        """
        ...


class WriteView:
    """受限编译器写操作的局部适配, 保留数组和文档赋值"""

    policy: PythonPolicy
    target: object

    def __init__(self, policy: PythonPolicy, target: object):
        """
        保存当前写入目标, 不通过模型属性写入守卫自举

        参数:
        - policy: 当前 worker 的属性与配置策略
        - target: 数组, 表格, 文档或普通容器对象
        """
        object.__setattr__(self, "policy", policy)
        object.__setattr__(self, "target", target)

    def __setattr__(self, name: str, value: object) -> None:
        """
        校验属性与绘图配置后转发对象赋值

        参数:
        - name: 待写入的属性名
        - value: 新属性值, 绘图配置受固定后端限制
        """
        self.policy.attribute(self.target, name)
        self.policy.check_setting(name, value)
        setattr(self.target, name, value)

    def __setitem__(self, key: object, value: object) -> None:
        """
        转发索引赋值并检查 RcParams 配置写入

        参数:
        - key: 容器索引或绘图配置键
        - value: 待写入的新值
        """
        if type(self.target).__name__ == "RcParams":
            self.policy.check_setting(str(key), value)
        cast(MutableItems, self.target)[key] = value

    def __delitem__(self, key: object) -> None:
        """
        将受限编译器的索引删除转发给业务对象

        参数:
        - key: 待删除的索引或键
        """
        del cast(MutableItems, self.target)[key]


class PythonPolicy:
    """导入守卫与已登记业务 API 的局部限制"""

    def __init__(self, modules: tuple[str, ...], files: FilePolicy, violations: Violations):
        """
        绑定公开模块, 文件策略与首次违规记录

        参数:
        - modules: 经过配置解析的完整公开模块名元组, 空元组禁止模型导入
        - files: 当前 worker 的业务路径与库内部文件策略
        - violations: 当前 worker 共用的首次违规记录
        """
        self.modules = frozenset(allowed_modules(",".join(modules)))
        self.files = files
        self.violations = violations
        self.loaded: dict[str, ModuleType] = {}

    def preflight(self, source: str, filename: str) -> None:
        """
        在执行任何模型语句前检查整段源码的静态导入

        参数:
        - source: 待解析的 Python 源码, 不执行其中语句
        - filename: 用于语法错误和源码定位的受控文件名

        非执行分支内的禁止导入也拒绝, 仅允许白名单及指向已登记子模块的父模块导航
        """
        tree = ast.parse(source, filename=filename)
        # 扫描整棵语法树, 禁止导入即使位于不会执行的分支也在业务执行前拒绝
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level or node.module is None:
                    self.reject_module("相对导入", node.lineno)
                names = [node.module]
                if any(alias.name == "*" for alias in node.names):
                    self.reject_module(node.module + ".*", node.lineno)
                if node.module not in self.modules and all(self.navigation(node.module + "." + alias.name) for alias in node.names):
                    names = []
            else:
                continue
            for name in names:
                if name not in self.modules:
                    self.reject_module(name, node.lineno)

    def reject_module(self, name: str, line: int = 0) -> NoReturn:
        """
        记录禁止导入的模块及可用源码位置

        参数:
        - name: 被拒绝的完整模块名或导入形态
        - line: 源码行号, 默认 0 表示未知

        始终抛出 PolicyViolation, 不自动扩大白名单
        """
        self.violations.reject("module_not_allowed", f"模块 {name} 不在允许列表中", module=name, line=line)

    def import_module(self, name: str, globals: object = None, locals: object = None,
                      fromlist: Iterable[str] = (), level: int = 0) -> ModuleView:
        """
        替代模型 import, 只返回受控模块视图

        参数:
        - name: 请求的完整模块名
        - globals: 兼容 __import__ 的全局参数, 默认 None, 不作为授权依据
        - locals: 兼容 __import__ 的局部参数, 默认 None, 不作为授权依据
        - fromlist: from-import 成员集合, 默认空元组, 仅登记子模块可使用父模块导航
        - level: 默认 0, 非零的相对导入一律拒绝

        返回:
        - 与导入语法对应的 ModuleView; 禁止导入或依赖缺失抛出策略异常
        """
        navigation_only = bool(fromlist) and all(self.navigation(name + "." + entry) for entry in fromlist)
        if level or (name not in self.modules and not navigation_only):
            self.reject_module(name)
        self.load(name)
        return ModuleView(self, name if fromlist else name.split(".")[0])

    def navigation(self, name: str) -> bool:
        """
        判断模块名是否为允许导入或子模块导航路径

        参数:
        - name: 完整模块名或父模块路径

        返回:
        - 已登记模块或其父路径返回 True, 其他名称返回 False
        """
        return name in self.modules or any(module.startswith(name + ".") for module in self.modules)

    def load(self, name: str) -> ModuleType:
        """
        在当前进程加载可信依赖并缓存模块对象

        参数:
        - name: 经调用方检查的模块名, pyplot 使用固定绘图初始化

        返回:
        - 当前 worker 内的模块对象; ImportError 转为 library_dependency_error
        """
        if name not in self.loaded:
            try:
                if name == "matplotlib.pyplot":
                    self.loaded[name] = self.load_plotting()
                else:
                    self.loaded[name] = importlib.import_module(name)
            except ImportError as error:
                self.violations.reject("library_dependency_error", f"模块 {name} 的固定依赖不可用: {error}", module=name)
        return self.loaded[name]

    def load_plotting(self) -> ModuleType:
        """
        在固定后端下初始化 pyplot 并使用进程内字体发现

        返回:
        - 初始化后的 pyplot 模块; 只在可信导入期间替代固定字体查询, 所有出口恢复进程拒绝守卫

        中文字体仅从宿主已有字体选择, 无可用项时使用 DejaVu Sans
        """
        # Step.1 保存已阻断的进程入口, 字体查询替代不恢复原始启动能力
        blocked_check_output = subprocess.check_output

        def font_query(command: object, *args: object, **kwargs: object) -> bytes:
            """
            替代 pyplot 可信导入期间的固定外部字体发现查询

            参数:
            - command: 仅接受固定 fc-list 或 system_profiler 字体查询
            - args: 兼容 check_output 的额外位置参数, 不用于启动程序
            - kwargs: 兼容 check_output 的关键字参数, 不用于启动程序

            返回:
            - 固定的空字体发现响应; 其他命令记录 process_not_allowed 并抛出策略异常
            """
            if command == ["fc-list", "--help"]:
                return b"--format"
            if command == ["fc-list", "--format=%{file}\\n"]:
                return b""
            if command == ["system_profiler", "-xml", "SPFontsDataType"]:
                return b'<?xml version="1.0"?><plist version="1.0"><array><dict><key>_items</key><array/></dict></array></plist>'
            self.violations.reject("process_not_allowed", "沙箱禁止启动外部程序", action="subprocess.check_output")

        # Step.2 只在可信 pyplot 导入期间提供固定字体发现结果
        subprocess.check_output = font_query
        try:
            plotting = importlib.import_module("matplotlib.pyplot")
            plotting.rcParams["text.usetex"] = False
            manager = importlib.import_module("matplotlib.font_manager")
            available = {font.name for font in manager.fontManager.ttflist}
            preferred = ("Microsoft YaHei", "Noto Sans CJK SC", "SimHei", "PingFang SC", "WenQuanYi Micro Hei")
            plotting.rcParams["font.family"] = [next((font for font in preferred if font in available), "DejaVu Sans")]
            return plotting
        finally:
            # Step.3 导入成功或失败都恢复统一的进程拒绝守卫
            subprocess.check_output = blocked_check_output

    def attribute(self, target: object, name: str) -> object:
        """
        统一检查模块成员和对象属性, 包装后续 API 调用

        参数:
        - target: 受控模块视图, API 包装或库返回的普通业务对象
        - name: 待访问的属性名, 不允许私有属性, 执行帧或内部守卫状态

        返回:
        - 受控子模块, GuardedCall 或业务属性; 索引器保持原对象, 未开放成员抛出策略异常或属性错误
        """
        from RestrictedPython.Guards import safer_getattr_raise
        from RestrictedPython.transformer import INSPECT_ATTRIBUTES
        # Step.1 拒绝私有属性, 执行帧和守卫内部状态
        if name.startswith("_") or isinstance(target, (ModuleView, GuardedCall, WriteView)) and name in {"policy", "target", "name"}:
            self.violations.reject("api_not_allowed", "不允许访问执行器内部或私有属性")
        if name in INSPECT_ATTRIBUTES:
            self.violations.reject("api_not_allowed", "不允许访问执行帧或源码内部属性")
        # Step.2 区分模块导航与业务属性, 按底层模块身份检查 API
        if isinstance(target, ModuleView):
            fullname = target._name + "." + name
            if fullname in self.modules or any(module.startswith(fullname + ".") for module in self.modules):
                return ModuleView(self, fullname)
            if target._name not in self.modules:
                self.violations.reject("api_not_allowed", "父模块仅允许导航到已登记子模块")
            module = self.load(target._name)
            self.check_api(target._name, name)
            if target._name in _PUBLIC_EXPORTS and name not in _PUBLIC_EXPORTS[target._name]:
                self.violations.reject("api_not_allowed", f"接口 {target._name}.{name} 未登记", action=name)
            result = safer_getattr_raise(module, name)
        elif isinstance(target, GuardedCall):
            result = self.attribute(target._target, name)
        else:
            module_name = getattr(type(target), "__module__", "")
            self.check_api(module_name, name)
            try:
                result = safer_getattr_raise(target, name)
            except NotImplementedError as error:
                self.violations.reject("api_not_allowed", str(error))
        # Step.3 模块透出继续受白名单约束, 可调用成员保持参数检查
        if isinstance(result, ModuleType):
            if result.__name__ not in self.modules:
                self.reject_module(result.__name__)
            return ModuleView(self, result.__name__)
        indexer = name in {"loc", "iloc", "at", "iat"} and getattr(type(result), "__module__", "").startswith("pandas")
        return GuardedCall(self, result) if callable(result) and not indexer else result

    def check_api(self, module: str, name: str) -> None:
        """
        拒绝登记库中的进程, 动态执行和未开放存储入口

        参数:
        - module: 底层 API 或对象类型所属模块
        - name: 待访问或调用的成员名

        匹配禁止入口时记录 api_not_allowed 并抛出策略异常
        """
        denied = {"ctypes", "ctypeslib", "f2py", "memmap", "dump", "dumps", "load_library", "frombuffer",
                  "fromstring", "setbufsize", "seterrcall"} if module.startswith("numpy") else set()
        if module.startswith("pandas"):
            denied |= {"eval", "query", "read_pickle", "to_pickle", "read_clipboard", "to_clipboard", "read_sql", "to_sql",
                       "read_sql_query", "read_sql_table", "read_hdf", "to_hdf", "HDFStore", "ExcelFile", "ExcelWriter"}
            if name.startswith("read_") and name not in {"read_csv", "read_json", "read_excel"}:
                denied.add(name)
            if name.startswith("to_") and name not in {"to_csv", "to_json", "to_excel", "to_numpy", "to_dict", "to_list", "to_frame", "to_string", "to_period", "to_timestamp", "to_datetime", "to_numeric", "to_timedelta", "to_records"}:
                denied.add(name)
        if module.startswith("matplotlib"):
            denied |= {"show", "use", "switch_backend", "canvas", "style", "get_backend_mod", "connect", "new_figure_manager"}
        if module.startswith("reportlab"):
            denied |= {"DocExec", "DocAssign", "DocIf", "DocWhile", "DocAssert", "DocPara", "rl_safe_eval", "rl_exec"}
        if module == "time":
            denied |= {"clock_settime", "clock_settime_ns"}
        if name in denied:
            self.violations.reject("api_not_allowed", f"接口 {module}.{name} 未开放", action=name)

    def check_setting(self, name: str, value: object) -> None:
        """
        阻止绘图切换外部排版和动态后端

        参数:
        - name: rcParams 键或对象配置名
        - value: 本次设置的配置值

        允许内部文本排版, 拒绝开启 LaTeX 或修改后端与 PGF 配置
        """
        if (name in {"text.usetex", "usetex"} and bool(value)) or name in {"backend", "backend_fallback"} or name.startswith("pgf."):
            self.violations.reject("argument_not_allowed", "绘图只允许固定静态后端与内部文本排版")

    def call(self, target: Callable[..., object], args: tuple[object, ...], kwargs: dict[str, object]) -> object:
        """
        按底层 API 身份校验参数和文件路径后执行业务调用

        参数:
        - target: 底层可调用对象, 普通别名保留原函数身份
        - args: 原位置参数元组, 仅在局部副本中规范化
        - kwargs: 关键字参数副本, 可注入固定引擎或受检路径

        返回:
        - 底层业务结果; 路径, 序列化, 绘图或文档规模违规抛出策略异常
        """
        # Step.1 依据底层 API 身份检查入口和本次绘图配置
        name = getattr(target, "__name__", "")
        owner = getattr(target, "__self__", None)
        module = getattr(target, "__module__", "") or getattr(type(owner), "__module__", "")
        self.check_api(module, name)
        if name == "update" and type(owner).__name__ == "RcParams":
            values = dict(cast(dict[str, object], args[0])) if args else {}
            values.update(kwargs)
            for key, value in values.items():
                self.check_setting(str(key), value)
        for key, value in kwargs.items():
            if key == "usetex":
                self.check_setting(key, value)
        if name == "set_usetex" and args:
            self.check_setting("usetex", args[0])
        if module.startswith("matplotlib") and name in {"rc", "rc_context"}:
            values = kwargs.get("rc", args[0] if name == "rc_context" and args else {})
            if isinstance(values, dict):
                for key, value in values.items():
                    self.check_setting(str(key), value)
            if name == "rc" and args and args[0] == "text" and kwargs.get("usetex"):
                self.check_setting("usetex", True)

        # Step.2 尽可能绑定原签名, 同时保留不支持反射的原生 API 适配
        try:
            bound = inspect.signature(target).bind_partial(*args, **kwargs)
        except (TypeError, ValueError):
            bound = None
        arguments = dict(bound.arguments) if bound is not None else {}
        mutable_args = list(args)

        def set_argument(key: str, value: object, position: int | None = None) -> None:
            """
            按原绑定位置替换参数, 避免同时传入同名关键字

            参数:
            - key: 底层函数参数名
            - value: 受控的新参数值
            - position: 默认 None 采用关键字传参, 有位置参数时直接替换该项
            """
            if position is not None and len(mutable_args) > position:
                mutable_args[position] = value
            else:
                kwargs[key] = value

        # Step.3 固定序列化规则和文档引擎, 不扩展可选执行后端
        if module.startswith("numpy") and name in {"load", "save"}:
            position = 2 if name == "save" else 2
            supplied = arguments.get("allow_pickle", kwargs.get("allow_pickle", False))
            if supplied:
                self.violations.reject("argument_not_allowed", "NumPy 禁止 pickle 序列化")
            set_argument("allow_pickle", False, position)
            if name == "load" and arguments.get("mmap_mode", kwargs.get("mmap_mode")) is not None:
                self.violations.reject("argument_not_allowed", "NumPy 不开放文件内存映射")
        if module.startswith("numpy") and name in {"savez", "savez_compressed", "tofile"}:
            numpy = self.load("numpy")
            values = [*args[1:], *kwargs.values()] if name != "tofile" else [owner]
            for value in values:
                if numpy.asarray(value).dtype.hasobject:
                    self.violations.reject("argument_not_allowed", "不允许 object 数组序列化")
        if module.startswith("pandas") and name in {"read_excel", "to_excel"}:
            if arguments.get("engine", kwargs.get("engine")) not in (None, "openpyxl"):
                self.violations.reject("argument_not_allowed", "XLSX 固定使用 openpyxl 引擎")
            position = list(bound.signature.parameters).index("engine") if bound is not None else None
            set_argument("engine", "openpyxl", position)
        if module.startswith("openpyxl") and name == "load_workbook":
            if arguments.get("keep_vba", False) or kwargs.get("keep_vba", False):
                self.violations.reject("argument_not_allowed", "首版不保留 VBA")
            if arguments.get("keep_links", False) is True:
                self.violations.reject("argument_not_allowed", "首版不保留外部工作簿链接")
            position = list(bound.signature.parameters).index("keep_links") if bound is not None else None
            set_argument("keep_links", False, position)
        if module.startswith("pdfplumber") and arguments.get("repair"):
            self.violations.reject("argument_not_allowed", "PDF 不使用外部修复程序")

        # Step.4 检查主路径和图片, 字体等次级资源路径
        path_names = {"file", "filename", "fileName", "fname", "path", "path_or_buf", "path_or_buffer", "filepath_or_buffer",
                      "excel_writer", "io", "docx", "pptx", "image_file", "image_path_or_stream", "path_or_fp", "fileobj", "stream"}
        file_calls = {"load", "save", "savez", "savez_compressed", "savetxt", "loadtxt", "genfromtxt", "fromfile", "tofile",
                      "read_csv", "read_json", "read_excel", "to_csv", "to_json", "to_excel", "Document", "Presentation",
                      "load_workbook", "PdfReader", "PdfWriter", "open", "Canvas", "SimpleDocTemplate", "BaseDocTemplate",
                      "Image", "ImageReader", "TTFont", "add_picture", "add_image", "add_image_or_link", "drawImage", "drawInlineImage",
                      "add_font", "savefig", "imread", "imsave", "write", "append", "merge", "append_pages_from_reader"}
        is_library = module.startswith(("numpy", "pandas", "docx", "pptx", "openpyxl", "pypdf", "pdfplumber", "reportlab", "matplotlib"))
        if is_library and name in file_calls:
            if bound is not None:
                for key, value in arguments.items():
                    if key not in path_names or value is None:
                        continue
                    if isinstance(value, (str, os.PathLike, io.IOBase)):
                        checked = self.files.argument(value, write=name in {"save", "savez", "savez_compressed", "savetxt", "tofile", "to_csv", "to_json", "to_excel", "Canvas", "SimpleDocTemplate", "BaseDocTemplate", "savefig", "imsave", "write"})
                        position = list(bound.signature.parameters).index(key)
                        set_argument(key, checked, position)
                        self.files.inspect_document(value)
            elif mutable_args and name in {"fromfile", "tofile", "load", "save", "savetxt", "loadtxt", "savez", "savez_compressed"}:
                mutable_args[0] = self.files.argument(mutable_args[0], write=name.startswith("save") or name == "tofile")
        if module.startswith("pandas") and name in {"read_json", "read_csv"} and args:
            self.files.argument(args[0])
        # Step.5 在渲染前复核后端, 规范化类型包装以兼容数值库参数
        if module.startswith("matplotlib") and name in {"savefig", "draw", "save"}:
            plotting = sys.modules.get("matplotlib")
            if plotting is not None:
                params = getattr(plotting, "rcParams")
                if params.get("text.usetex") or str(params.get("backend", "")).lower() != "agg":
                    self.violations.reject("argument_not_allowed", "绘图配置不属于固定静态运行模式")
            if kwargs.get("backend") is not None or kwargs.get("format") not in (None, "png", "svg", "pdf"):
                self.violations.reject("argument_not_allowed", "仅支持 PNG/SVG/PDF 固定静态输出")
            filename = arguments.get("fname", arguments.get("filename"))
            if isinstance(filename, str) and Path(filename).suffix.lower() not in {"", ".png", ".svg", ".pdf"}:
                self.violations.reject("argument_not_allowed", "绘图文件扩展名仅允许 PNG/SVG/PDF")
        for key in ("dtype",):
            value = kwargs.get(key)
            if isinstance(value, GuardedCall) and isinstance(value._target, type):
                kwargs[key] = value._target
            if bound is not None and key in bound.arguments:
                value = bound.arguments[key]
                if isinstance(value, GuardedCall) and isinstance(value._target, type):
                    set_argument(key, value._target, list(bound.signature.parameters).index(key))
        if module.startswith("numpy") and name == "astype" and mutable_args and isinstance(mutable_args[0], GuardedCall):
            mutable_args[0] = mutable_args[0]._target
        # Step.6 调用真实库并限制返回文档的规模
        result = target(*mutable_args, **kwargs)
        if (module.startswith("pypdf") and name == "PdfReader") or (module.startswith("pdfplumber") and name == "open"):
            pages = getattr(result, "pages", ())
            if len(pages) > 1_000:
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                self.violations.reject("argument_not_allowed", "PDF 页数超过 1000 页")
        return result

    def execute(self, source: str, filename: str, stdout: io.TextIOBase) -> None:
        """
        受限编译源码并在独立守卫命名空间执行

        参数:
        - source: 待检查的 Python 源码
        - filename: 用于诊断和受控 __file__ 的文件名
        - stdout: 接收 print 输出的当前 worker 文本流

        静态导入先检查, 语法错误不切回普通编译; 本过程只应在一次性 worker 内调用
        """
        from RestrictedPython.compile import compile_restricted_exec
        from RestrictedPython.Guards import safe_builtins, guarded_iter_unpack_sequence, guarded_unpack_sequence
        # Step.1 在任何业务语句执行前完成导入检查与受限编译
        self.preflight(source, filename)
        compiled = compile_restricted_exec(source, filename)
        if compiled.errors or compiled.code is None:
            raise SyntaxError("; ".join(compiled.errors))
        code = compiled.code
        # Step.2 仅加入受控 builtins, 不暴露完整解释器全局状态
        builtins_map = dict(safe_builtins)
        for name in ("setattr", "delattr", "id"):
            builtins_map.pop(name, None)
        builtins_map.update({name: getattr(builtins, name) for name in ("list", "dict", "set", "frozenset", "enumerate", "sum", "min", "max", "all", "any", "reversed", "iter", "next")})
        builtins_map.update({"__import__": self.import_module, "open": self.files.open, "getattr": self.attribute})

        # Step.3 适配类型判断, 打印, 写入和增强赋值的受限编译协议
        def isinstance_checked(value: object, classes: object) -> bool:
            """
            还原受控类型包装并兼容类型元组判断

            参数:
            - value: 待判断的业务对象
            - classes: 类型, GuardedCall 类型包装或递归类型元组

            返回:
            - 对象匹配时返回 True; 非类型目标抛出 TypeError
            """
            if isinstance(classes, GuardedCall):
                classes = classes._target
            if isinstance(classes, tuple):
                return any(isinstance_checked(value, item) for item in classes)
            if not isinstance(classes, type):
                raise TypeError("isinstance 第二个参数必须是类型或类型元组")
            return isinstance(value, classes)

        builtins_map["isinstance"] = isinstance_checked

        class Printer:
            def __init__(self, _getattr: object = None):
                """
                兼容受限编译器创建打印收集器的调用形态

                参数:
                - _getattr: 编译器传入的属性守卫, 默认 None, 输出直接使用外层受控文本流
                """
                pass

            def _call_print(self, *args: object, sep: str | None = " ", end: str | None = "\n",
                            file: object = None, flush: bool = False) -> None:
                """
                把模型 print 重定向到当前 worker 的有界输出

                参数:
                - args: 待打印的业务值
                - sep: 值之间的分隔符, 默认空格, None 沿用 print 行为
                - end: 输出结束文本, 默认换行, None 沿用 print 行为
                - file: 默认 None, 不允许指定其他输出文件
                - flush: 默认 False, 传给受控输出流
                """
                if file is not None:
                    raise ValueError("print 仅支持标准输出")
                print(*args, file=stdout, sep=sep, end=end, flush=flush)

            def __call__(self) -> str:
                """
                满足受限编译器的打印收集器读取协议

                返回:
                - 固定空字符串, 实际输出已写入外层 stdout, 不重复保存
                """
                return ""

        def write(value: object) -> WriteView:
            """
            为业务对象创建局部写入守卫

            参数:
            - value: 待赋值的业务对象, 模块与执行器包装禁止写入

            返回:
            - WriteView 写入适配; 执行器或模块对象抛出策略异常
            """
            if isinstance(value, (ModuleView, GuardedCall, ModuleType)):
                self.violations.reject("api_not_allowed", "不允许修改模块或执行器对象")
            return WriteView(self, value)

        def inplace(op: str, left: object, right: object) -> object:
            """
            将受限编译器的增强赋值映射为已登记运算

            参数:
            - op: 受支持的增强赋值运算符
            - left: 左侧业务值
            - right: 右侧业务值

            返回:
            - 对应运算结果; 未登记运算符或业务类型错误继续传播
            """
            operations = {"+=": operator.add, "-=": operator.sub, "*=": operator.mul, "/=": operator.truediv,
                          "//=": operator.floordiv, "%=": operator.mod, "**=": operator.pow,
                          "|=": operator.or_, "&=": operator.and_, "^=": operator.xor, "<<=": operator.lshift, ">>=": operator.rshift}
            return operations[op](left, right)

        # Step.4 构造本次独立命名空间, 安装库文件守卫后执行业务字节码
        scope: dict[str, object] = {"__builtins__": builtins_map, "__name__": "__sandbox__", "__file__": filename,
                                    "_getattr_": self.attribute, "_getitem_": operator.getitem, "_getiter_": iter,
                                    "_write_": write, "_print_": Printer, "_inplacevar_": inplace,
                                    "_iter_unpack_sequence_": guarded_iter_unpack_sequence, "_unpack_sequence_": guarded_unpack_sequence}
        self.files.install_library_io()
        exec(code, scope)
