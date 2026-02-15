import sqlite3
import json
from typing import List, Dict, Union, Optional
from satrap import logger

class ContextManager:
    """对话上下文管理器"""
    def __init__(self, conversation_id: Union[int, str], keep_in_memory: bool = False, db_path: str = "./satrap/satrapdata/chat_history.db"):
        """
        初始化上下文管理器

        参数:
        - conversation_id: 当前对话的唯一ID
        - keep_in_memory:
            True: 加载数据后在内存操作, 需手动调用 save_context() 写入数据库 <br>
            False: (推荐) 每次修改操作自动同步到数据库, 保证数据不丢失
        - db_path: SQLite数据库路径

        返回:
        - None
        """
        self.db_path = db_path
        self.conversation_id = str(conversation_id)
        self.keep_in_memory = keep_in_memory
        self._messages: List[Dict[str, str | list]] = []   # 内存中的消息缓存
        self._init_db_table()                       # 初始化数据库表结构
        self.load_context()                         # 加载数据

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
            logger.error(f"[上下文管理器] 初始化数据库表失败: {e}")

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
            logger.error(f"Error loading context for {self.conversation_id}: {e}")
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
            logger.info(f"[上下文管理器] 上下文已保存, 对话ID: {self.conversation_id}, 消息数量: {len(self._messages)}")

        except Exception as e:
            logger.error(f"[上下文管理器] 保存上下文失败: {e}")
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

    def add_user_message(self, message: str):
        """
        添加用户消息到上下文中

        参数:
        - message: 消息内容
        """
        self._messages.append({"role": "user", "content": message})
        self._sync()

    def add_system_message(self, message: str):
        """
        添加系统消息到上下文中

        参数:
        - message: 消息内容
        """
        self._messages.append({"role": "system", "content": message})
        self._sync()

    def add_bot_message(self, message: str, tools_call: dict | None = None):
        """
        添加机器人消息到上下文中

        参数:
        - message: 消息内容
        - tools_call: 工具调用信息, 可选, 格式为 {"id": "工具ID", "type": "function", "function": {"name": "工具名", "arguments": "参数JSON字符串"}}
        """
        if tools_call:
            self._messages.append({"role": "assistant", "content": message, "tool_calls": [tools_call]})

        else:
            self._messages.append({"role": "assistant", "content": message})

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

    def add_tool_message(self, tool_call_id: str, tool_result: dict):
        """
        添加工具调用的返回消息到上下文中

        参数:
        - tool_call_id: 工具调用ID
        - tool_result: 工具调用的返回结果
        """
        self._messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": json.dumps(tool_result, ensure_ascii=False)})
        self._sync()

    def add_tool_call_flow(self, message: str, tool_message: dict, tool_result: dict):
        """
        添加一个完整的工具调用消息流到上下文中

        相当于:
        ctx.add_bot_message(message, tool_message)
        ctx.add_tool_message(tool_message["id"], tool_result)

        参数:
        - message: 模型消息
        - tool_message: 工具调用消息
        - tool_result: 工具调用的返回结果
        """
        self.add_bot_message(message, tool_message)
        self.add_tool_message(tool_message["id"], tool_result)
        self._sync()


    def add_at_system_start(self, message: str):
        """
        在上下文的开头添加系统消息

        参数:
        - message: 消息内容
        """
        self._messages.insert(0, {"role": "system", "content": message})
        self._sync()

    def add_at_system_end(self, message: str):
        """
        在上下文的结尾添加系统消息

        参数:
        - message: 消息内容
        """
        self.add_system_message(message)

    def static_message(self) -> int:
        """
        统计上下文中的消息数量

        返回:
        - int: 消息总数
        """
        return len(self._messages)

    def del_context(self):
        """删除当前上下文中的所有消息"""
        self._messages.clear()
        self._sync()
        logger.info(f"Context cleared for ID: {self.conversation_id}")

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
            logger.error(f"[上下文管理] 删除消息失败: 索引 {index} 超出范围")
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
        删除上下文中的最后n条聊天消息 (用户消息和机器人消息)

        参数:
        - n: 删除的组数 (每组包含两条消息: 用户 + 机器人)
        """
        indices_to_remove = []   # 倒序查找
        for i in range(len(self._messages) - 1, -1, -1):
            if self._messages[i].get("role") in ["user", "assistant"]:
                indices_to_remove.append(i)
                if len(indices_to_remove) >= n * 2:
                    break

        for index in indices_to_remove:
            self._messages.pop(index)
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
            logger.info(f"[上下文管理] 成功导出 json 文件: {file_path}")

        except Exception as e:
            logger.error(f"[上下文管理] 导出 json 文件失败: {e}")

# ================= 使用示例 =================
if __name__ == "__main__":
    ctx = ContextManager(conversation_id=1001, keep_in_memory=False)
    
    if ctx.static_message() == 0:
        ctx.add_at_system_start("你是一个翻译助手.")
        logger.info("Initialized new conversation for 1001")

    ctx.add_user_message("Hello")
    ctx.add_bot_message("你好")
    
    print(f"ID 1001 消息数: {ctx.static_message()}")

    # 2. 批量操作模式 (keep_in_memory=True)
    # 适合需要大量修改, 最后一次性保存的场景.
    ctx2 = ContextManager(conversation_id=1002, keep_in_memory=True)
    ctx2.add_system_message("你是一个数学助手.")
    ctx2.add_chat("1+1等于几?", "等于2.")
    ctx2.save_context() # 必须手动调用