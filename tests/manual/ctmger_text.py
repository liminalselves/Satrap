from satrap.core.utils.context import ContextManager


ctx = ContextManager(conversation_id=1001, keep_in_memory=False)

if ctx.static_message() == 0:
    ctx.add_at_system_start("你是一个翻译助手.")

ctx.add_user_message("Hello")
ctx.add_bot_message("你好")

print(f"ID 1001 消息数: {ctx.static_message()}")

# 2. 批量操作模式 (keep_in_memory=True)
# 适合需要大量修改, 最后一次性保存的场景.
ctx2 = ContextManager(conversation_id=1002, keep_in_memory=True)
ctx2.add_system_message("你是一个数学助手.")
ctx2.add_chat("1+1等于几?", "等于2.")
ctx2.save_context() # 必须手动调用