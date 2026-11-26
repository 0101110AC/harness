"""应用配置：全部来自环境变量 / .env（pydantic-settings）。"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- 应用 ----
    APP_NAME: str = "Arch-Harness API"
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"

    # ---- 数据库 ----
    # 开发默认 SQLite；生产改 postgresql+asyncpg://user:pass@host:5432/db
    DB_URL: str = Field(default="sqlite+aiosqlite:///./arch_harness.db")
    DB_ECHO: bool = False
    # 连接池（仅 PostgreSQL 生效）：压测时默认 5+10 会在 20 并发下排队，
    # 表现为 P95 突然抬高而不是报错。按进程并发上限显式配置。
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_TIMEOUT: int = 30
    DB_POOL_RECYCLE: int = 1800

    # ---- JWT ----
    JWT_SECRET: str = "dev-insecure-secret-change-me-please"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRES: int = 7200  # access: 2h
    JWT_REFRESH_EXPIRES: int = 604800  # refresh（勾选记住我）: 7d
    JWT_REFRESH_EXPIRES_SHORT: int = 86400  # refresh（未勾选）: 1d

    # ---- CORS ----
    CORS_ORIGINS: str = "http://localhost:3000"

    # ---- 登录限流（prd-07 §1.3 429 RATE_LIMITED）----
    LOGIN_MAX_FAILURES: int = 5
    LOGIN_LOCK_SECONDS: int = 300

    # ---- 文件上传（prd-02 §4）----
    UPLOAD_DIR: str = "./uploads"
    CHUNK_SIZE: int = 5 * 1024 * 1024
    MAX_PDF_SIZE: int = 100 * 1024 * 1024
    MAX_DXF_SIZE: int = 50 * 1024 * 1024
    UPLOAD_SESSION_TTL_HOURS: int = 24
    # 签名下载链接有效期（prd-07 §3.9 / §5.4：≤10 分钟）
    FILE_URL_TTL: int = 600

    # ---- AI 引擎 / SSE / HITL（prd-06 §8~§9）----
    REVIEW_ENGINE: str = "mock"  # mock / real（改环境变量即可回退，不动契约）
    # ---- 沙箱工具（prd-06 §6）----
    SANDBOX_TOOL_TIMEOUT: int = 60
    SANDBOX_DXF_TIMEOUT: int = 180
    SANDBOX_PDF_TIMEOUT: int = 120
    # DXF 白名单图层：只读这四层，其余图层只统计不采集几何
    SANDBOX_DXF_LAYERS: str = "WALL,DOOR,WINDOW,COLUMN"
    # 数值护栏：FIELD_ONLY（V1.0，只校验 fact_ref 存在性）/ FULL_SCAN（加全文数值比对）
    FACT_REF_MODE: str = "FIELD_ONLY"
    # Mock 节点人为延迟（毫秒/节点），仅用于肉眼观察 SSE 过程，生产置 0
    MOCK_NODE_DELAY_MS: int = 0
    # 一次性 SSE token（绑定 user + thread_id，单次有效）
    STREAM_TOKEN_TTL: int = 60
    SSE_HEARTBEAT_SEC: int = 15
    SSE_MAX_PER_THREAD: int = 3
    SSE_MAX_CONNECTIONS: int = 200
    SSE_REPLAY_LIMIT: int = 5000
    SSE_REPLAY_WINDOW_MINUTES: int = 30
    SSE_IDLE_CLOSE_SEC: int = 30 * 60
    # 中断工单有效期与超时扫描周期
    HITL_TTL_HOURS: int = 24
    HITL_SWEEP_SECONDS: int = 300
    HITL_SWEEP_BATCH: int = 50

    # ---- 站内信 / Outbox（prd-04 §9）----
    # 适配器注册表：逗号分隔，默认只开站内信；V1.5 加飞书只需改这一行
    NOTIFICATION_ADAPTERS: str = "inapp"
    OUTBOX_POLL_MS: int = 100  # 消费者轮询间隔（prd-04 §5 要求落库 < 1s）
    OUTBOX_BATCH: int = 20
    OUTBOX_MAX_RETRY: int = 3
    # PROCESSING 租约：消费者崩在中途时，超过这个时间的 PROCESSING 会被重新认领
    OUTBOX_LEASE_SECONDS: int = 60
    OUTBOX_ENABLED: bool = True
    # 批量合并窗口（prd-04 §4：60 秒内同 type 同接收人合并为 1 条）
    NOTIFICATION_MERGE_WINDOW: int = 60
    # 评论自删窗口（prd-03 §6.2）
    COMMENT_DELETE_WINDOW: int = 300

    @property
    def adapter_names(self) -> list[str]:
        return [n.strip().lower() for n in self.NOTIFICATION_ADAPTERS.split(",") if n.strip()]

    @property
    def dxf_layers(self) -> list[str]:
        return [n.strip().upper() for n in self.SANDBOX_DXF_LAYERS.split(",") if n.strip()]

    @property
    def fact_ref_mode(self) -> str:
        return self.FACT_REF_MODE.strip().upper()

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.DB_URL.startswith("sqlite")

    @field_validator("JWT_SECRET")
    @classmethod
    def _secret_not_empty(cls, v: str) -> str:
        if not v or len(v) < 16:
            raise ValueError("JWT_SECRET 至少 16 字符，生产环境请用 openssl rand -hex 32 生成")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
