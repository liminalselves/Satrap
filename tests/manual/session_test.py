import satrap, uuid
from satrap import LLM, ToolsManager
from satrap.core.framework import Session
from satrap import ModelWorkflowFramework
from satrap.core.framework.command import CommandHandler

llm = LLM(
    api_key="",
    base_url="https://api.deepseek.com/v1",
    model="deepseek-chat",
    temperature=0.7,
    max_tokens=2000
)

command_handler = CommandHandler()
command_handler.register_command("get_model_name", llm.get_model, "获取当前模型名称")
command_handler.register_command("get_api_key", llm.get_api_key, "获取当前 API 密钥")

class MyWF(ModelWorkflowFramework):
    def __init__(self, llm: LLM, tools_manager: ToolsManager | None = None, content_callback=None, command_handler=None, context_id=""):
        super().__init__(llm=llm, tools_manager=tools_manager, content_callback=content_callback, context_id=context_id)

    def forward(self, query: str) -> str:
        self.ctx.add_user_message(query)
        response = self.llm.call(self.ctx.get_context(), tools=self.tools_manager.get_tools_definitions())
        res_msg = self.final_response(response)
        self.ctx.add_bot_message(res_msg)
        return res_msg

class MySession(Session):
    def __init__(self, session_id: str, content_callback=None, command_handler=None):
        super().__init__(session_id=session_id, content_callback=content_callback, command_handler=command_handler)
        self.wf = MyWF(llm=llm, tools_manager=ToolsManager(), content_callback=content_callback, command_handler=command_handler, context_id=session_id)

    def run(self, query: str) -> str | None:
        result, is_cmd = self.cmd_process(query)
        if is_cmd:
            return None   # 如果是命令消息, 直接返回 None 不继续处理
        response = self.wf.forward(query)
        if self.content_callback:
            self.content_callback(response)
        return response

def callback(content: str):
    print(f"bot: {content}")

def command_callback(content: str):
    print(f"{content}")

command_handler.set_callback(command_callback)

test_session = MySession(str(uuid.uuid4()), content_callback=callback, command_handler=command_handler)

while True:
    query = input("user: ")
    if query == "exit":
        break
    response = test_session.run(query)


