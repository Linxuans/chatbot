"""微信群聊智能体：可插拔网关 + 容器化智能体核心。

分层：
    gateway/  贴着微信客户端跑（Windows），只负责收发消息
    agent.py  核心编排：触发判定 -> 上下文 -> 模型 -> 切分回复
    core_api.py 把核心暴露成 HTTP 服务，供 Docker 部署
"""

__version__ = "0.2.0"