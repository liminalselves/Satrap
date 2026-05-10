from satrap.core.utils.tokenizer import tokenizer_estimate, experience_estimate
from typing import List, Dict, Union, Optional, Any
import aiosqlite
import asyncio
import sqlite3
import json
import copy
import re

from satrap.core.log import logger

class ContextManager:
    """对话上下文管理器"""
    def __init__(
        self,
        conversation_id: Union[int, str],
        keep_in_memory: bool = False,
        db_path: str = ".satrap/chat_history.db",
        max_context: int = 128000,
        context_threshold: float = 0.9,
        exceed_process: str = "sliding",
    ):
        """
        初始化上下文管理器

        参数:
        - conversation_id: 当前对话的唯一ID
        - keep_in_memory:
            - True: 加载数据后在内存操作, 需手动调用 save_context() 写入数据库 <br>
            - False: (推荐) 每次修改操作自动同步到数据库, 保证数据不丢失
        - db_path: SQLite 数据库路径
        - max_context: 最大上下文长度, 默认 128k
        - context_threshold: 上下文阈值, 超过阈值时删除旧消息, 默认 0.9 (即 90% 上下文长度)
        - exceed_process: 超过阈值时的处理方式, 默认 "sliding" (滑动窗口)
            - "sliding": 滑动窗口策略, 删除旧消息, 保持上下文长度在阈值以下
            - "mid_truncate": 中间截断策略, 从中间截断上下文, 不删除旧消息

        返回:
        - None
        """
        self.db_path = db_path
        self.conversation_id = str(conversation_id)
        self.keep_in_memory = keep_in_memory
        self._messages: List[Dict[str, str | list]] = []   # 内存中的消息缓存
        self._init_db_table()   # 初始化数据库表结构
        self.load_context()     # 加载数据

        self.max_context = max_context
        self.context_threshold = context_threshold
        self.exceed_process = exceed_process

        logger.info(f"[上下文管理器] 初始化完成, 对话ID: {self.conversation_id}")

    def _get_conn(self):
        """获取数据库连接"""
        return sqlite3.connect(self.db_path)

    def _init_db_table(self):
        """初始化数据库表结构"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS chat_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL
                )
            ''')   # 创建一个简单的表存储所有消息

            cursor.execute('''CREATE INDEX IF NOT EXISTS idx_conv_id ON chat_history (conversation_id)''')
            # 创建索引加速查询

            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"[上下文管理器] 初始化数据库表失败: {e}, ID: {self.conversation_id}")

    def _sync(self):
        """根据 keep_in_memory 策略决定是否立即写入数据库"""
        if not self.keep_in_memory:
            self.save_context()

    # ================= 核心功能实现 =================

    def load_context(self):
        """从数据库加载当前ID的上下文信息到内存中"""
        try:
            conn = self._get_conn()
            cursor = conn.cursor()
            # 按插入顺序读取
            cursor.execute(
                "SELECT role, content FROM chat_history WHERE conversation_id = ? ORDER BY id ASC", 
                (self.conversation_id,)
            )
            rows = cursor.fetchall()
            conn.close()   # 关闭连接

            self._messages = [{"role": row[0], "content": row[1]} for row in rows]
        except Exception as e:
            logger.error(f"[上下文管理器] 加载上下文失败: {self.conversation_id}: {e}, ID: {self.conversation_id}")
            self._messages = []

    def save_context(self):
        """保存当前上下文到数据库"""
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            # 1. 删除该ID下的旧数据
            cursor.execute("DELETE FROM chat_history WHERE conversation_id = ?", (self.conversation_id,))
            
            # 2. 批量插入新数据
            if self._messages:
                data_to_insert = [
                    (self.conversation_id, msg["role"], msg["content"]) 
                    for msg in self._messages
                ]
                cursor.executemany(
                    "INSERT INTO chat_history (conversation_id, role, content) VALUES (?, ?, ?)", 
                    data_to_insert
                )
            conn.commit()

        except Exception as e:
            logger.error(f"[上下文管理器] 保存上下文失败: {self.conversation_id}: {e}, ID: {self.conversation_id}")
            conn.rollback()

        finally:
            conn.close()

    def get_context(self) -> List[Dict[str, str | list]]:
        """
        获取当前上下文中的所有消息

        返回:
        - List[Dict[str, str]]: 消息列表
        """
        return self._messages

    def get_model_context(self, method: str = "tokenizer") -> List[Dict[str, str | list]]:
        """
        获取发送给模型的上下文 (保证在最大上下文长度内)
        
        参数:
        - method: token 估算方法, 同 estimate_token()
        
        返回:
        - 截断后的消息列表副本, 适合直接用于 API 调用
        """
        return self._apply_truncation(self._messages, method)

    def add_user_message(self, message: str):
        """
        添加用户消息到上下文中

        参数:
        - message: 消息内容
        """
        self._messages.append({"role": "user", "content": message})
        self._sync()

    def reset_system_prompt(self, message: str):
        """
        重置系统提示词

        参数:
        - message: 新的系统提示词
        """
        self._messages = [m for m in self._messages if m.get("role") != "system"]
        # 在开头插入新的系统消息
        self._messages.insert(0, {"role": "system", "content": message})
        self._sync()

    def add_bot_message(self, message: str, tools_calls: list[dict] | None = None, ignore_think: bool = True, reasoning: str | None = None):
        """
        添加机器人消息到上下文中

        参数:
        - message: 消息内容
        - tools_calls: 工具调用信息列表, 可选, 每个元素格式为 {"id": "工具ID", "type": "function", "function": {"name": "工具名", "arguments": "参数JSON字符串"}}
        - ignore_think: 是否忽略消息中的思考过程, 默认True
        - reasoning: 思考过程, 可选
        """
        if tools_calls:
            self._messages.append(
                {
                    "role": "assistant",
                    "content": message,
                    "reasoning_content": reasoning if reasoning is not None and reasoning != "" and not ignore_think else None,   # type: ignore
                    "tool_calls": tools_calls,
                }
            )
        else:
            self._messages.append(
                {
                    "role": "assistant", 
                    "content": message,
                    "reasoning_content": reasoning if reasoning is not None and reasoning != "" and not ignore_think else None,   # type: ignore
                }
            )

        self._sync()

    def add_chat(self, user_message: str, bot_message: str):
        """
        添加一条用户消息和对应的机器人消息到上下文中

        参数:
        - user_message: 用户消息
        - bot_message: 机器人消息
        """
        self._messages.append({"role": "user", "content": user_message})
        self._messages.append({"role": "assistant", "content": bot_message})
        self._sync()

    def add_tool_message(self, tool_call_id: str, tool_result: dict | str):
        """
        添加工具调用的返回消息到上下文中

        参数:
        - tool_call_id: 工具调用ID
        - tool_result: 工具调用的返回结果
        """
        self._messages.append({"role": "tool", "tool_call_id": tool_call_id,
            "content": json.dumps(tool_result, ensure_ascii=False) if isinstance(tool_result, dict) else tool_result})
        self._sync()

    def add_tool_call_flow(self, message: str, tool_messages: list[dict], tool_results: list[dict]):
        """
        添加一个完整的工具调用消息流到上下文中

        相当于:
        ``` python
        ctx.add_bot_message(message, tool_messages)
        for tool_msg, tool_res in zip(tool_messages, tool_results):
            ctx.add_tool_message(tool_msg["id"], tool_res)
        ```

        参数:
        - message: 模型消息
        - tool_messages: 工具调用消息列表
        - tool_results: 工具调用的返回结果列表, 与 tool_messages 一一对应
        """
        self.add_bot_message(message, tool_messages)
        for tool_msg, tool_res in zip(tool_messages, tool_results):
            self.add_tool_message(tool_msg["id"], tool_res)
        self._sync()

    def add_at_system_start(self, message: str, separator: str = ""):
        """
        在原系统提示词的开头添加消息 (修改第一条系统消息的内容)

        参数:
        - message: 要添加的消息内容
        - separator: 拼接时插入的分隔符，默认为空字符串
        """
        # 查找第一条系统消息
        for msg in self._messages:
            if msg.get("role") == "system":
                # 在开头拼接新内容
                msg["content"] = message + separator + msg["content"]   # type: ignore
                break
        else:
            # 没有系统消息, 则新建一条并插入到开头
            self._messages.insert(0, {"role": "system", "content": message})
        self._sync()

    def add_at_system_end(self, message: str, separator: str = ""):
        """
        在原系统提示词的结尾添加消息 (修改第一条系统消息的内容)

        参数:
        - message: 要添加的消息内容
        - separator: 拼接时插入的分隔符，默认为空字符串
        """
        # 查找第一条系统消息
        for msg in self._messages:
            if msg.get("role") == "system":
                # 在结尾拼接新内容
                msg["content"] = msg["content"] + separator + message   # type: ignore
                break
        else:
            # 没有系统消息, 则新建一条并插入到开头
            self._messages.insert(0, {"role": "system", "content": message})
        self._sync()

    def add_turn_messages(self, turn_messages: list[dict]):
        """
        添加多条消息到上下文中

        参数:
        - turn_messages: 要添加的消息列表
        """
        serialized = []
        for msg in turn_messages:
            new_msg = copy.deepcopy(msg)
            serialized.append(new_msg)
        
        self._messages.extend(serialized)
        self._sync()

    def static_message(self) -> int:
        """
        统计上下文中的消息数量

        返回:
        - int: 消息总数
        """
        return len(self._messages)

    def del_context(self):
        """删除当前上下文中的所有消息, 保留系统消息"""
        sys_msg = self._messages[0]
        self._messages.clear()
        self._messages.append(sys_msg)
        self._sync()
        logger.info(f"上下文已清空, ID: {self.conversation_id}")

    def del_system_message(self):
        """删除上下文中的系统消息"""
        self._messages[:] = [msg for msg in self._messages if msg.get("role") != "system"]
        self._sync()

    def del_message(self, index: int):
        """
        删除上下文中指定索引的消息

        参数:
        - index: 消息的索引
        """
        try:
            if 0 <= index < len(self._messages):
                self._messages.pop(index)
            elif -len(self._messages) <= index < 0:
                self._messages.pop(index)

        except IndexError:
            logger.error(f"[上下文管理] 删除消息失败: 索引 {index} 超出范围, ID: {self.conversation_id}")
            pass
        self._sync()

    def del_last_message(self, n: int = 1):
        """
        删除上下文中的最后n条消息

        参数:
        - n: 删除的数量
        """
        for _ in range(n):
            if self._messages:
                self._messages.pop()
        self._sync()

    def del_last_chat(self, n: int = 1):
        """
        删除上下文中的最后 n 组聊天消息

        参数:
        - n: 删除的组数
        """
        indices_to_remove = []
        groups_removed = 0

        # 1. 倒序遍历消息列表
        for i in range(len(self._messages) - 1, -1, -1):
            msg = self._messages[i]
            role = msg.get("role")

            if role == "system":   # 系统消息在开头, 说明已经没对话了
                break

            indices_to_remove.append(i)   # 将当前索引加入待删除列表

            if role == "user":   # 遇到 user 消息, 说明完成了一整组对话的定位
                groups_removed += 1
                if groups_removed >= n:
                    break

        for index in indices_to_remove:
            self._messages.pop(index)
        # 执行删除   

        self._sync()

    def export_json(self, file_path: str):
        """
        导出当前上下文到json文件

        参数:
        - file_path: 导出路径
        """
        data = {"id": self.conversation_id, "messages": self._messages}
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            logger.info(f"[上下文管理] 成功导出 json 文件: {file_path}, ID: {self.conversation_id}")

        except Exception as e:
            logger.error(f"[上下文管理] 导出 json 文件失败: {e}, ID: {self.conversation_id}")

    def _group_messages_by_turns(self, messages: List[Dict]) -> List[List[Dict]]:
        """
        将消息列表按对话轮次分组
        每组以 user 消息开头, 包含随后的 assistant 和 tool 消息
        第一组可能以 system 消息开头

        参数:
        - messages: 待分组的消息列表

        返回:
        - List[List[Dict]]: 分组后的轮次列表
        """
        turns = []
        current_turn = []
        for msg in messages:
            role = msg.get("role")
            if role == "system" and not current_turn:
                current_turn.append(msg)
            elif role == "user":
                if current_turn:
                    turns.append(current_turn)
                current_turn = [msg]
            else:
                current_turn.append(msg)
        if current_turn:
            turns.append(current_turn)
        return turns

    def _flatten_turns(self, turns: List[List[Dict]]) -> List[Dict]:
        """将分组后的轮次列表还原为扁平消息列表"""
        return [msg for turn in turns for msg in turn]

    def estimate_token(self, messages: List[Dict] | None = None, method: str = "tokenizer") -> int:
        """
        估计当前上下文中的 token 数量

        参数:
        - messages: 待估算 token 数的消息列表, 默认当前上下文中的所有消息
        - method: 估计方法, 可选值为 "tokenizer" 或 "experience"

        返回:
        - int: token 数量
        """
        token_count = 0
        if messages is None:
            messages = self._messages
        for msg in messages:
            token_count += 4   # 每个消息有 4 个固定 token
            if method == "tokenizer":
                token_count += tokenizer_estimate(str(msg.get("content", "")))
            elif method == "experience":
                token_count += experience_estimate(str(msg.get("content", "")))
            else:
                token_count += experience_estimate(str(msg.get("content", "")))   # 默认使用经验法则
        return token_count

    def _apply_sliding_truncation(self, messages: List[Dict], threshold: int, method: str) -> List[Dict]:
        """
        滑动窗口截断: 保留系统消息, 从最早的对话轮次开始整轮删除, 直到 token 数不超过阈值

        参数:
        - messages: 原始消息列表
        - threshold: token 上限阈值
        - method: 估算方法

        返回:
        - 截断后的新消息列表
        """
        turns = self._group_messages_by_turns(messages)
        if not turns:
            return messages.copy()

        # 提取并保留纯系统轮次
        system_turn = None
        if all(msg.get("role") == "system" for msg in turns[0]):
            system_turn = turns.pop(0)

        # 逐轮删除最早的非系统轮次
        truncated_turns = turns[:]
        while truncated_turns and self.estimate_token(
            self._flatten_turns(([system_turn] if system_turn else []) + truncated_turns),
            method=method
        ) > threshold:
            truncated_turns.pop(0)

        result_turns = ([system_turn] if system_turn else []) + truncated_turns
        return self._flatten_turns(result_turns)

    def _apply_truncation(self, messages: List[Dict], method: str = "tokenizer") -> List[Dict]:
        """
        对消息列表应用截断策略, 返回截断后的新列表 (不修改原列表)

        参数:
        - messages: 待截断的消息列表
        - method: token 估算方法, 同 estimate_token() 参数

        返回:
        - List[Dict]: 截断后的新列表
        """
        threshold = int(self.max_context * self.context_threshold)

        current_tokens = self.estimate_token(messages, method=method)
        if current_tokens <= threshold:
            return messages.copy()   # 无需截断, 返回副本

        logger.debug(f"模型上下文超限 ({current_tokens} > {threshold})，应用 {self.exceed_process} 截断")

        turns = self._group_messages_by_turns(messages)
        system_turn = None
        if turns and all(msg.get("role") == "system" for msg in turns[0]):
            system_turn = turns.pop(0)

        if self.exceed_process == "sliding":   # 滑动窗口截断
            return self._apply_sliding_truncation(messages, threshold, method)

        elif self.exceed_process == "mid_truncate":   # 中间截断: 保留头部和尾部, 删除中间轮次

            if len(turns) <= 4:
                logger.debug(f"[上下文管理] 轮次过少，中间截断退化为滑动窗口, ID: {self.conversation_id}")
                return self._apply_sliding_truncation(messages, threshold, method)

            def token_of_turn_list(turn_list):   # 计算需要删除多少 token
                return self.estimate_token(self._flatten_turns(
                    ([system_turn] if system_turn else []) + turn_list
                ), method=method)

            kept_turns = turns[:]   # 初始保留所有轮次  
            while token_of_turn_list(kept_turns) > threshold and len(kept_turns) > 2:   # 从中间开始逐轮删除, 直到满足条件
                mid = len(kept_turns) // 2   # 找到中间索引
                del kept_turns[mid]   # 删除

            result = ([system_turn] if system_turn else []) + kept_turns   # 合并保留的轮次
            return self._flatten_turns(result)

        else:
            logger.error(f"[上下文管理] 未知截断策略 {self.exceed_process}, 返回原列表, ID: {self.conversation_id}")
            return messages.copy()

class AsyncContextManager:
    """异步对话上下文管理器"""
    def __init__(
        self,
        conversation_id: Union[int, str],
        keep_in_memory: bool = False,
        db_path: str = ".satrap/chat_history.db",
        max_context: int = 128000,
        context_threshold: float = 0.9,
        exceed_process: str = "sliding",
    ):
        """
        初始化异步上下文管理器

        注意: 初始化后请 await manager.initialize() 或使用 async with 语句自动初始化

        参数:
        - conversation_id: 当前对话的唯一 ID
        - keep_in_memory:
            True: 加载数据后在内存操作，需手动调用 save_context() 写入数据库 <br>
            False: (推荐) 每次修改操作自动同步到数据库, 保证数据不丢失
        - db_path: SQLite 数据库路径, 默认 "./satrap/satrapdata/chat_history.db"
        - max_context: 最大上下文长度, 默认 128k
        - context_threshold: 上下文阈值, 超过阈值时删除旧消息, 默认 0.9 (即 90% 上下文长度)
        - exceed_process: 超过阈值时的处理方式, 默认 "sliding" (滑动窗口)
            - "sliding": 滑动窗口策略, 删除旧消息, 保持上下文长度在阈值以下
            - "mid_truncate": 中间截断策略, 从中间截断上下文, 不删除旧消息

        """
        self.db_path = db_path
        self.conversation_id = str(conversation_id)
        self.keep_in_memory = keep_in_memory
        self._messages: List[Dict[str, str | list]] = []   # 内存中的消息缓存
        self.max_context = max_context                     # 最大上下文长度
        self.context_threshold = context_threshold         # 上下文阈值
        self.exceed_process = exceed_process               # 超过阈值时的处理方式
        logger.info(f"[异步上下文管理] 实例已创建，对话 ID: {self.conversation_id}")

    async def initialize(self):
        """
        异步初始化数据库表并加载上下文
        需在实例化后手动调用, 或使用 async with 语句
        """
        await self._init_db_table()
        await self.load_context()
        logger.info(f"[异步上下文管理] 初始化完成，对话 ID: {self.conversation_id}")

    async def __aenter__(self):
        """支持 async with 语句"""
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """退出时确保数据保存"""
        if not self.keep_in_memory:
            await self.save_context()

    async def _init_db_table(self):
        """初始化数据库表结构"""
        try:
            async with aiosqlite.connect(self.db_path) as conn:
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS chat_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        conversation_id TEXT NOT NULL,
                        role TEXT NOT NULL,
                        content TEXT NOT NULL
                    )
                ''')   # 创建一个简单的表存储所有消息

                await conn.execute('''CREATE INDEX IF NOT EXISTS idx_conv_id ON chat_history (conversation_id)''')
                # 创建索引加速查询

                await conn.commit()
        except Exception as e:
            logger.error(f"[异步上下文管理] 初始化数据库表失败：{e}, ID: {self.conversation_id}")

    async def _sync(self):
        """根据 keep_in_memory 策略决定是否立即写入数据库"""
        if not self.keep_in_memory:
            await self.save_context()

    # ================= 核心功能实现 =================

    async def load_context(self):
        """从数据库加载当前 ID 的上下文信息到内存中"""
        try:
            async with aiosqlite.connect(self.db_path) as conn:
                # 按插入顺序读取
                cursor = await conn.execute(
                    "SELECT role, content FROM chat_history WHERE conversation_id = ? ORDER BY id ASC", 
                    (self.conversation_id,)
                )
                rows = await cursor.fetchall()

            self._messages = [{"role": row[0], "content": row[1]} for row in rows]
        except Exception as e:
            logger.error(f"[异步上下文管理] 加载上下文失败：{self.conversation_id}: {e}, ID: {self.conversation_id}")
            self._messages = []

    async def save_context(self):
        """保存当前上下文到数据库"""
        try:
            async with aiosqlite.connect(self.db_path) as conn:
                # 1. 删除该 ID 下的旧数据
                await conn.execute("DELETE FROM chat_history WHERE conversation_id = ?", (self.conversation_id,))
                
                # 2. 批量插入新数据
                if self._messages:
                    data_to_insert = [
                        (self.conversation_id, msg["role"], msg["content"]) 
                        for msg in self._messages
                    ]
                    await conn.executemany(
                        "INSERT INTO chat_history (conversation_id, role, content) VALUES (?, ?, ?)", 
                        data_to_insert
                    )
                await conn.commit()

        except Exception as e:
            logger.error(f"[异步上下文管理] 保存上下文失败：{self.conversation_id}: {e}, ID: {self.conversation_id}")

    def get_context(self) -> List[Dict[str, str | list]]:
        """
        获取当前上下文中的所有消息 (内存操作, 同步)

        返回:
        - List[Dict[str, str]]: 消息列表
        """
        return self._messages
    
    def get_model_context(self, method: str = "tokenizer") -> List[Dict[str, str | list]]:
        """
        获取发送给模型的上下文 (保证在最大上下文长度内)
        
        参数:
        - method: token 估算方法, 同 estimate_token()
        
        返回:
        - 截断后的消息列表副本, 适合直接用于 API 调用
        """
        return self._apply_truncation(self._messages, method)

    async def add_user_message(self, message: str):
        """
        添加用户消息到上下文中

        参数:
        - message: 消息内容
        """
        self._messages.append({"role": "user", "content": message})
        await self._sync()

    async def reset_system_prompt(self, message: str):
        """
        重置系统提示词

        参数:
        - message: 新的系统提示词
        """
        self._messages = [m for m in self._messages if m.get("role") != "system"]
        # 在开头插入新的系统消息
        self._messages.insert(0, {"role": "system", "content": message})
        await self._sync()

    async def add_bot_message(self, message: str, tools_calls: list[dict] | None = None, ignore_think: bool = True, reasoning: str | None = None):
        """
        添加机器人消息到上下文中

        参数:
        - message: 消息内容
        - tools_calls: 工具调用信息列表，可选，每个元素格式为 {"id": "工具 ID", "type": "function", "function": {"name": "工具名", "arguments": "参数 JSON 字符串"}}
        - ignore_think: 是否忽略消息中的思考过程, 默认True
        - reasoning: 思考过程, 可选
        """
        if tools_calls:
            self._messages.append(
                {
                    "role": "assistant",
                    "content": message,
                    "reasoning_content": reasoning if reasoning is not None and reasoning != "" and not ignore_think else None,   # type: ignore
                    "tool_calls": tools_calls,
                }
            )
        else:
            self._messages.append(
                {
                    "role": "assistant", 
                    "content": message,
                    "reasoning_content": reasoning if reasoning is not None and reasoning != "" and not ignore_think else None,   # type: ignore
                }
            )

        await self._sync()

    async def add_chat(self, user_message: str, bot_message: str):
        """
        添加一条用户消息和对应的机器人消息到上下文中

        参数:
        - user_message: 用户消息
        - bot_message: 机器人消息
        """
        self._messages.append({"role": "user", "content": user_message})
        self._messages.append({"role": "assistant", "content": bot_message})
        await self._sync()

    async def add_tool_message(self, tool_call_id: str, tool_result: dict):
        """
        添加工具调用的返回消息到上下文中

        参数:
        - tool_call_id: 工具调用 ID
        - tool_result: 工具调用的返回结果
        """
        self._messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": json.dumps(tool_result, ensure_ascii=False)})
        await self._sync()

    async def add_tool_call_flow(self, message: str, tool_messages: list[dict], tool_results: list[dict]):
        """
        添加一个完整的工具调用消息流到上下文中

        相当于:
        ``` python
        await ctx.add_bot_message(message, tool_messages)
        for tool_msg, tool_res in zip(tool_messages, tool_results):
            await ctx.add_tool_message(tool_msg["id"], tool_res)
        ```

        参数:
        - message: 模型消息
        - tool_messages: 工具调用消息列表
        - tool_results: 工具调用的返回结果列表，与 tool_messages 一一对应
        """
        await self.add_bot_message(message, tool_messages)
        for tool_msg, tool_res in zip(tool_messages, tool_results):
            await self.add_tool_message(tool_msg["id"], tool_res)
        await self._sync()

    async def add_at_system_start(self, message: str, separator: str = ""):
        """
        在原系统提示词的开头添加消息（修改第一条系统消息的内容）

        参数:
        - message: 要添加的消息内容
        - separator: 拼接时插入的分隔符，默认为空字符串
        """
        # 查找第一条系统消息
        for msg in self._messages:
            if msg.get("role") == "system":
                # 在开头拼接新内容
                msg["content"] = message + separator + msg["content"]   # type: ignore
                break
        else:
            # 没有系统消息，则新建一条并插入到开头
            self._messages.insert(0, {"role": "system", "content": message})
        await self._sync()


    async def add_at_system_end(self, message: str, separator: str = ""):
        """
        在原系统提示词的结尾添加消息 (修改第一条系统消息的内容)

        参数:
        - message: 要添加的消息内容
        - separator: 拼接时插入的分隔符, 默认为空字符串
        """
        # 查找第一条系统消息
        for msg in self._messages:
            if msg.get("role") == "system":
                # 在结尾拼接新内容
                msg["content"] = msg["content"] + separator + message   # type: ignore
                break
        else:
            # 没有系统消息, 则新建一条并插入到开头
            self._messages.insert(0, {"role": "system", "content": message})
        await self._sync()

    async def add_turn_messages(self, turn_messages: list[dict]):
        """
        添加多条消息到上下文中

        参数:
        - turn_messages: 要添加的消息列表
        """
        serialized = []
        for msg in turn_messages:
            new_msg = copy.deepcopy(msg)
            serialized.append(new_msg)

        self._messages.extend(serialized)
        await self._sync()

    def static_message(self) -> int:
        """
        统计上下文中的消息数量 (内存操作, 同步)

        返回:
        - int: 消息总数
        """
        return len(self._messages)

    async def del_context(self):
        """删除当前上下文中的所有消息, 保留系统消息"""
        if not self._messages:
            return
        sys_msg = self._messages[0]
        self._messages.clear()
        self._messages.append(sys_msg)
        await self._sync()
        logger.info(f"上下文已清空：{self.conversation_id}, ID: {self.conversation_id}")

    async def del_system_message(self):
        """删除上下文中的系统消息"""
        self._messages[:] = [msg for msg in self._messages if msg.get("role") != "system"]
        await self._sync()

    async def del_message(self, index: int):
        """
        删除上下文中指定索引的消息

        参数:
        - index: 消息的索引
        """
        try:
            if 0 <= index < len(self._messages):
                self._messages.pop(index)
            elif -len(self._messages) <= index < 0:
                self._messages.pop(index)

        except IndexError:
            logger.error(f"[异步上下文管理器] 删除消息失败：索引 {index} 超出范围, ID: {self.conversation_id}")
            pass
        await self._sync()

    async def del_last_message(self, n: int = 1):
        """
        删除上下文中的最后 n 条消息

        参数:
        - n: 删除的数量
        """
        for _ in range(n):
            if self._messages:
                self._messages.pop()
        await self._sync()

    async def del_last_chat(self, n: int = 1):
        """
        删除上下文中的最后 n 组聊天消息

        参数:
        - n: 删除的组数
        """
        indices_to_remove = []
        groups_removed = 0

        # 1. 倒序遍历消息列表
        for i in range(len(self._messages) - 1, -1, -1):
            msg = self._messages[i]
            role = msg.get("role")

            if role == "system":   # 系统消息在开头，说明已经没对话了
                break

            indices_to_remove.append(i)   # 将当前索引加入待删除列表

            if role == "user":   # 遇到 user 消息，说明完成了一整组对话的定位
                groups_removed += 1
                if groups_removed >= n:
                    break

        # 需要排序索引以确保 pop 顺序正确 (从大到小 pop 避免索引偏移)
        for index in sorted(indices_to_remove, reverse=True):
            self._messages.pop(index)
        
        await self._sync()

    async def export_json(self, file_path: str):
        """
        导出当前上下文到 json 文件 (异步文件 IO)

        参数:
        - file_path: 导出路径
        """
        data = {"id": self.conversation_id, "messages": self._messages}
        try:
            # 使用 asyncio.to_thread 避免阻塞事件循环
            def _write_file():
                with open(file_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=4)
            
            await asyncio.to_thread(_write_file)
            logger.info(f"[异步上下文管理] 成功导出 json 文件：{file_path}, ID: {self.conversation_id}")

        except Exception as e:
            logger.error(f"[异步上下文管理] 导出 json 文件失败：{e}, ID: {self.conversation_id}")

    def _group_messages_by_turns(self, messages: List[Dict]) -> List[List[Dict]]:
        """
        将消息列表按对话轮次分组
        每组以 user 消息开头, 包含随后的 assistant 和 tool 消息
        第一组可能以 system 消息开头

        参数:
        - messages: 待分组的消息列表

        返回:
        - List[List[Dict]]: 分组后的轮次列表
        """
        turns = []
        current_turn = []
        for msg in messages:
            role = msg.get("role")
            if role == "system" and not current_turn:
                current_turn.append(msg)
            elif role == "user":
                if current_turn:
                    turns.append(current_turn)
                current_turn = [msg]
            else:
                current_turn.append(msg)
        if current_turn:
            turns.append(current_turn)
        return turns

    def _flatten_turns(self, turns: List[List[Dict]]) -> List[Dict]:
        """将分组后的轮次列表还原为扁平消息列表"""
        return [msg for turn in turns for msg in turn]

    def estimate_token(self, messages: List[Dict] | None = None, method: str = "tokenizer") -> int:
        """
        估计当前上下文中的 token 数量

        参数:
        - messages: 待估算 token 数的消息列表, 默认当前上下文中的所有消息
        - method: 估计方法, 可选值为 "tokenizer" 或 "experience"

        返回:
        - int: token 数量
        """
        token_count = 0
        if messages is None:
            messages = self._messages
        for msg in messages:
            token_count += 4   # 每个消息有 4 个固定 token
            if method == "tokenizer":
                token_count += tokenizer_estimate(str(msg.get("content", "")))
            elif method == "experience":
                token_count += experience_estimate(str(msg.get("content", "")))
            else:
                token_count += experience_estimate(str(msg.get("content", "")))   # 默认使用经验法则
        return token_count

    def _apply_sliding_truncation(self, messages: List[Dict], threshold: int, method: str) -> List[Dict]:
        """
        滑动窗口截断: 保留系统消息, 从最早的对话轮次开始整轮删除, 直到 token 数不超过阈值

        参数:
        - messages: 原始消息列表
        - threshold: token 上限阈值
        - method: 估算方法

        返回:
        - 截断后的新消息列表
        """
        turns = self._group_messages_by_turns(messages)
        if not turns:
            return messages.copy()

        # 提取并保留纯系统轮次
        system_turn = None
        if all(msg.get("role") == "system" for msg in turns[0]):
            system_turn = turns.pop(0)

        # 逐轮删除最早的非系统轮次
        truncated_turns = turns[:]
        while truncated_turns and self.estimate_token(
            self._flatten_turns(([system_turn] if system_turn else []) + truncated_turns),
            method=method
        ) > threshold:
            truncated_turns.pop(0)

        result_turns = ([system_turn] if system_turn else []) + truncated_turns
        return self._flatten_turns(result_turns)

    def _apply_truncation(self, messages: List[Dict], method: str = "tokenizer") -> List[Dict]:
        """
        对消息列表应用截断策略, 返回截断后的新列表 (不修改原列表)

        参数:
        - messages: 待截断的消息列表
        - method: token 估算方法, 同 estimate_token() 参数

        返回:
        - List[Dict]: 截断后的新列表
        """
        threshold = int(self.max_context * self.context_threshold)

        current_tokens = self.estimate_token(messages, method=method)
        if current_tokens <= threshold:
            return messages.copy()   # 无需截断, 返回副本

        logger.debug(f"[异步上下文管理] 模型上下文超限 ({current_tokens} > {threshold})，应用 {self.exceed_process} 截断, ID: {self.conversation_id}")

        turns = self._group_messages_by_turns(messages)
        system_turn = None
        if turns and all(msg.get("role") == "system" for msg in turns[0]):
            system_turn = turns.pop(0)

        if self.exceed_process == "sliding":   # 滑动窗口截断
            return self._apply_sliding_truncation(messages, threshold, method)

        elif self.exceed_process == "mid_truncate":   # 中间截断: 保留头部和尾部, 删除中间轮次

            if len(turns) <= 4:
                logger.debug(f"[异步上下文管理] 轮次过少，中间截断退化为滑动窗口, ID: {self.conversation_id}")
                return self._apply_sliding_truncation(messages, threshold, method)

            def token_of_turn_list(turn_list):   # 计算需要删除多少 token
                return self.estimate_token(self._flatten_turns(
                    ([system_turn] if system_turn else []) + turn_list
                ), method=method)

            kept_turns = turns[:]   # 初始保留所有轮次  
            while token_of_turn_list(kept_turns) > threshold and len(kept_turns) > 2:   # 从中间开始逐轮删除, 直到满足条件
                mid = len(kept_turns) // 2   # 找到中间索引
                del kept_turns[mid]   # 删除

            result = ([system_turn] if system_turn else []) + kept_turns   # 合并保留的轮次
            return self._flatten_turns(result)

        else:
            logger.error(f"[异步上下文管理] 未知截断策略 {self.exceed_process}, 返回原列表, ID: {self.conversation_id}")
            return messages.copy()

def add_user_message(context: list[dict[str, Any]], message: str):
    """向上下文中添加一条用户消息
    
    参数:
    - context: 上下文列表
    - message: 用户消息内容
    """
    context.append({"role": "user", "content": message})

def add_bot_message(context: list[dict[str, Any]], message: str, tools_calls: list[dict] | None = None, reasoning: str | None = None):
    """向上下文中添加一条助手消息
    
    参数:
    - context: 上下文列表
    - message: 助手消息内容
    - tools_calls: 工具调用列表, 默认 None
    - reasoning: 思考内容, 默认 None
    """
    if tools_calls:
        context.append(
            {
                "role": "assistant",
                "content": message,
                "reasoning_content": reasoning if reasoning is not None and reasoning != "" else None,   # type: ignore
                "tool_calls": tools_calls,
            }
        )
    else:
        context.append(
            {
                "role": "assistant", 
                "content": message,
                "reasoning_content": reasoning if reasoning is not None and reasoning != "" else None,   # type: ignore
            }
        )

def add_tool_message(context: list[dict[str, Any]], tool_call_id: str, tool_result: dict | str):
    """向上下文中添加一条工具调用结果消息
    
    参数:
    - context: 上下文列表
    - tool_call_id: 工具调用 ID
    - tool_result: 工具调用结果
    """
    context.append({"role": "tool", "tool_call_id": tool_call_id,
        "content": json.dumps(tool_result, ensure_ascii=False) if isinstance(tool_result, dict) else tool_result})

def add_tools_call_flow(context: list[dict[str, Any]], message: str, tool_messages: list[dict], tool_results: list[dict], reasoning: str | None = None):
    """添加一个完整的工具调用消息流到上下文中

    相当于:
    ``` python
    ctx.add_bot_message(message, tool_messages, reasoning)
    for tool_msg, tool_res in zip(tool_messages, tool_results):
        ctx.add_tool_message(tool_msg["id"], tool_res)
    ```

    参数:
    - context: 上下文列表
    - message: 助手消息内容
    - tool_messages: 工具调用消息列表
    - tool_results: 工具调用结果列表
    - reasoning: 思考内容, 默认 None
    """
    add_bot_message(context, message, tools_calls=tool_messages, reasoning=reasoning)
    for tool_msg, tool_res in zip(tool_messages, tool_results):
        add_tool_message(context, tool_msg["id"], tool_res)

def clear_reasoning_content(messages: list[dict[str, Any]]):
    """清除上下文中所有消息的思考内容 (reasoning_content 字段)"""
    for message in messages:
        if 'reasoning_content' in message:
            message["reasoning_content"] = None


