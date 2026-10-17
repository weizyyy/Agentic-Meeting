"""声纹：跨会议记住说话人（docs/design/speaker-memory.md，接口见 docs/interfaces.md §4.4）。

service.py  声纹服务：独立进程，封装声纹模型，只监听本机回环地址
client.py   应用一侧调用声纹服务的客户端
clips.py    会议进行中切出单人语音片段、算声纹、落库（不影响转录）
"""
