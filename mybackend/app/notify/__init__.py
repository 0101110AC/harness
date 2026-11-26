"""站内信事件架构（prd-04 §9）：Outbox 表 / 适配器 / 渲染 / 分发。

模块划分的理由：这四件事的变更频率完全不同 ——
业务侧只碰 `emit`（outbox.py），文案改动只碰 `render.py`，
渠道扩展只碰 `adapters.py`，调度/重试只碰 `dispatcher.py` + consumer。
"""
