"""HTTP 服务：WebRTC 信令、截图上传、会话/说话人/任务的查询与修改、静态页面。

app.py           FastAPI 应用工厂、WebRTC 信令
sessions_api.py  会话 / 发言 / 说话人
frames_api.py    截图上传与读取
tasks_api.py     后台任务
reports_api.py   会后报告
export.py        导出（Markdown / JSON / 压缩包）
auth.py          访问口令：登录、会话 Cookie、CSRF、登录限速
"""
