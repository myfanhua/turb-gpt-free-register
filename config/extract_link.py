# -*- coding: utf-8 -*-
"""Plus 试用提链服务配置。"""
from config.env_loader import apply_env_overrides

# 提链服务地址
EXTRACT_LINK_API_BASE: str = ""

# 提链 CDK；创建任务和监听事件都需要。
EXTRACT_LINK_CDK: str = ""

# 默认提链 provider：legacy / kakao_batch
EXTRACT_LINK_PROVIDER: str = "legacy"

# 提链类型：pix / upi / kakao_pay / ideal
EXTRACT_LINK_TYPE: str = "pix"

# 后台提链并发与超时
EXTRACT_LINK_WORKERS: int = 3
EXTRACT_LINK_QUEUE_LIMIT: int = 500
EXTRACT_LINK_REQUEST_TIMEOUT: int = 30
EXTRACT_LINK_EVENT_TIMEOUT: int = 180

# Kakao Pay 异步批量提链接口
KAKAO_EXTRACT_API_BASE: str = "https://tiqu.dxmcs.xin"
KAKAO_EXTRACT_CDK: str = ""
KAKAO_EXTRACT_USE_PROXY_POOL: bool = True
KAKAO_EXTRACT_BATCH_SIZE: int = 5
KAKAO_EXTRACT_TIMEOUT_SECONDS: int = 930
KAKAO_EXTRACT_POLL_INTERVAL: float = 4.0

apply_env_overrides(globals(), {
    'EXTRACT_LINK_API_BASE': 'str',
    'EXTRACT_LINK_CDK': 'str',
    'EXTRACT_LINK_PROVIDER': 'str',
    'EXTRACT_LINK_TYPE': 'str',
    'EXTRACT_LINK_WORKERS': 'int',
    'EXTRACT_LINK_QUEUE_LIMIT': 'int',
    'EXTRACT_LINK_REQUEST_TIMEOUT': 'int',
    'EXTRACT_LINK_EVENT_TIMEOUT': 'int',
    'KAKAO_EXTRACT_API_BASE': 'str',
    'KAKAO_EXTRACT_CDK': 'str',
    'KAKAO_EXTRACT_USE_PROXY_POOL': 'bool',
    'KAKAO_EXTRACT_BATCH_SIZE': 'int',
    'KAKAO_EXTRACT_TIMEOUT_SECONDS': 'int',
    'KAKAO_EXTRACT_POLL_INTERVAL': 'float',
})
