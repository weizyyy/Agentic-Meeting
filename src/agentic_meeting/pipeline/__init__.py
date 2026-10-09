"""Pipecat 管线的组装与自定义处理器。

bot.py         组装一次连接的管线并运行
session.py     会话管理：新建 / 继续 / 结束，新连接顶替旧连接
recorder.py    会议记录器：融合说话人、落库、推送字幕（位于唤醒门控上游）
wake.py        唤醒策略：Pipecat 自带策略 + 适用于中英混排的词边界
services.py    实时模型与语音合成的 Pipecat 服务
context.py     实时模型的上下文管理：只追加、空闲期压缩、缓存预热
tools.py       暴露给实时模型的工具
text_input.py  文字输入；modality.py 决定一次应答朗读还是只出文字
activity.py    助理此刻忙不忙；background.py 后台模型入口（忙时让路）
digest.py      滚动纪要；report.py 会后报告
"""
