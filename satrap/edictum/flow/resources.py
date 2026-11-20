"""流程节点共享执行资源。"""
from satrap.core.utils.async_worker import BoundedAsyncWorker

FLOW_CONDITION_WORKERS = BoundedAsyncWorker("satrap-flow-cond", workers=4, capacity=32)
"""插件条件函数专用的有界线程池: 条件不做 IO, 超时或池满判为错误"""
