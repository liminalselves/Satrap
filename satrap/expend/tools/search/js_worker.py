"""轻量 V8 子进程入口, 按文件路径运行以避免加载 Satrap 主服务"""
from __future__ import annotations

from typing import cast
import traceback
import json
import time
import sys


_BOOTSTRAP = r"""
(data) => {
  const state = {cookie: '', reload: false, unsupported: false};
  const location = {};
  Object.defineProperty(location, 'href', {get(){return '';}, set(){state.unsupported=true;}});
  location.reload = () => {state.reload=true;};
  const document = {
    referrer: '', location,
    getElementById(id) {
      if (id !== 'renderData') {state.unsupported=true; return null;}
      return {innerHTML: data};
    }
  };
  Object.defineProperty(document, 'cookie', {
    get(){return state.cookie.split(';')[0];},
    set(value){const text=String(value);if(text.length>4096){state.unsupported=true;return;}state.cookie=text;}
  });
  globalThis.window=globalThis;
  globalThis.document=new Proxy(document, {
    get(target, key) {
      if (!(key in target)) state.unsupported=true;
      return Reflect.get(target,key);
    }
  });
  Object.defineProperty(globalThis, 'location', {get(){return location;},set(){state.unsupported=true;}});
  globalThis.console=Object.freeze({log(){},warn(){},error(){},info(){},debug(){},trace(){}});
  return () => JSON.stringify(state);
}
"""


def evaluate(job: dict[str, object]) -> dict[str, object]:
    """
    在独立 V8 context 内执行已筛选的内联脚本

    参数:
    - job: 父进程传来的 data 和 scripts, 不接受路径或 Python 回调

    返回:
    - 单个 Cookie 与重载意图, 或不含网页源码的稳定失败类别
    """
    try:
        from py_mini_racer import JSEvalException, JSTimeoutException, JSOOMException, JSFunction, mini_racer
    except ImportError:
        return {"ok": False, "code": "js_runtime_unavailable"}
    data, scripts = job.get("data"), job.get("scripts")
    if (set(job) != {"data", "scripts"} or not isinstance(data, str) or len(data.encode("utf-8")) > 4096
            or not isinstance(scripts, list)):
        return {"ok": False, "code": "js_execution_failed"}
    entries = cast(list[object], scripts)
    if not 1 <= len(entries) <= 3:
        return {"ok": False, "code": "js_execution_failed"}
    sources: list[str] = []
    for script in entries:
        if not isinstance(script, str):
            return {"ok": False, "code": "js_execution_failed"}
        sources.append(script)
    if sum(len(script.encode("utf-8")) for script in sources) > 256 * 1024:
        return {"ok": False, "code": "js_execution_failed"}
    try:
        with mini_racer() as context:
            context.set_hard_memory_limit(64 * 1024 * 1024)
            setup = cast(JSFunction, context.eval(_BOOTSTRAP, timeout_sec=1))
            read_state = cast(JSFunction, setup(data, timeout_sec=1))
            end = time.monotonic() + 2
            for source in sources:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return {"ok": False, "code": "js_execution_timeout"}
                context.eval(source, timeout_sec=remaining)
            state_json = read_state(timeout_sec=0.2)
            if not isinstance(state_json, str):
                return {"ok": False, "code": "js_execution_failed"}
            raw: object = json.loads(state_json)
            state = cast(dict[str, object], raw)
            if state.get("unsupported") or not state.get("reload"):
                return {"ok": False, "code": "js_challenge_unsupported"}
            return {"ok": True, "cookie": state.get("cookie"), "reload": True}
    except JSTimeoutException:
        return {"ok": False, "code": "js_execution_timeout"}
    except JSOOMException:
        return {"ok": False, "code": "js_memory_limit"}
    except JSEvalException:
        return {"ok": False, "code": "js_challenge_unsupported"}


def main() -> None:
    """读取一个有界请求并发布有界结果, stdin 关闭后不再接受后续任务"""
    result: dict[str, object]
    try:
        payload = sys.stdin.buffer.read(512 * 1024 + 1)
        if len(payload) > 512 * 1024:
            raise ValueError("request limit")
        raw: object = json.loads(payload.decode("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("request type")
        result = evaluate(cast(dict[str, object], raw))
    except Exception:
        traceback.print_exc(file=sys.stderr)
        result = {"ok": False, "code": "js_execution_failed"}
    output = json.dumps(result, ensure_ascii=True).encode("utf-8")
    if len(output) > 8192:
        output = b'{"ok":false,"code":"js_execution_failed"}'
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
