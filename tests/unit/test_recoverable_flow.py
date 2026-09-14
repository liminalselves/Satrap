"""固定执行状态转换不依赖同步或异步 I/O"""
import pytest

from satrap.core.framework.Base.execution.flow import agent_flow, ModelStep, ToolStep


def test_flow_reconstructs_tool_transcript():
    flow = agent_flow("hello", 2)
    first = next(flow)
    assert isinstance(first, ModelStep)
    call = {"id": "call-1", "name": "search", "arguments": {}}
    step = flow.send({"type": "tools_call", "tool_calls": [call], "content": ""})
    assert isinstance(step, ToolStep)
    step = flow.send((call, {"role": "tool", "tool_call_id": "call-1", "content": "found"}))
    assert isinstance(step, ModelStep)
    assert step.key == "model:1"
    with pytest.raises(StopIteration) as ended:
        flow.send({"type": "text", "content": "answer"})
    messages, answer = ended.value.value
    assert answer == "answer"
    assert messages[0] == {"role": "user", "content": "hello"}
    assert messages[-1]["content"] == "answer"
