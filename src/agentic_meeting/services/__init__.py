"""本地推理服务的进程管理。

    paths.py       定位推理程序与动态库（runtimes/ 与 third_party/*/build/）
    supervisor.py  按配置生成命令行、拉起子进程、健康检查、收集日志、优雅退出

命令行入口见 cli.py 的 ``services up`` / ``services status``。
"""
