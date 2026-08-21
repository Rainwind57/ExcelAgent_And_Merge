"""ProcessPool 冒烟测试用轻量 worker(agent-free,可被子进程安全 import)。

放 routers/ 下以模拟真实 worker(_compare_one_table_proc)的模块位置,
子进程导入仅触发空的 routers/__init__.py + 本模块,不触 agent 重依赖。
"""
def smoke_worker(gn: str):
    return (gn, gn.upper())
