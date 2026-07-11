"""
Central configuration loaded from environment / .env via pydantic-settings.

All tunables live here so the rest of the codebase never reads os.environ
directly. Keep secrets out of version control — see .env.example.
"""
from functools import lru_cache
from typing import List
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
BASE_DIR = Path(__file__).resolve().parent.parent

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR /".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )
    print("model_config:",model_config)
    # ----- Application -----
    APP_NAME: str = "Company AI Assistant"
    APP_ENV: str = "development"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    APP_DEBUG: bool = True
    CORS_ORIGINS: List[str] = Field(default_factory=list)

    # ----- Security -----
    JWT_SECRET_KEY: str = "change-me"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    OTP_EXPIRE_SECONDS: int = 300
    OTP_LENGTH: int = 6
    RATE_LIMIT_PER_MINUTE: int = 60

    # ----- Database -----
    DATABASE_URL: str = "sqlite+aiosqlite:///./Companyasset.db"
    SYNC_DATABASE_URL: str = "sqlite:///./Companyasset.db"

    # ----- AWS -----
    AWS_REGION: str = "us-east-1"
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    S3_BUCKET_NAME: str = "company-kb"
    S3_KMS_KEY_ID: str = ""
    AWS_SESSION_TOKEN: str= ""

    # ----- Bedrock -----
    BEDROCK_REGION: str = "us-east-1"
    BEDROCK_MODEL_ID: str = "arn:aws:bedrock:ap-south-1:012857119299:inference-profile/global.anthropic.claude-haiku-4-5-20251001-v1:0"
    # Models the in-chat model switcher offers (CROSSADMIN / SUPERADMIN
    # only). Comma-separated "modelId|Label" pairs. Edit to match what's
    # actually enabled in your AWS account/region. The default model above
    # is always included automatically.
    BEDROCK_SELECTABLE_MODELS: str = (
    "arn:aws:bedrock:ap-south-1:012857119299:inference-profile/global.anthropic.claude-sonnet-4-6|Claude 4.6 Sonnet,"
    "arn:aws:bedrock:ap-south-1:012857119299:inference-profile/global.anthropic.claude-haiku-4-5-20251001-v1:0|Claude 4.5 Haiku,"
    "arn:aws:bedrock:ap-south-1:012857119299:inference-profile/apac.anthropic.claude-3-7-sonnet-20250219-v1:0|Claude 3.7 Sonnet,"
    "arn:aws:bedrock:ap-south-1:012857119299:inference-profile/apac.anthropic.claude-3-5-sonnet-20240620-v1:0|Claude 3.5 Sonnet,"
    
    "arn:aws:bedrock:ap-south-1:012857119299:inference-profile/apac.amazon.nova-pro-v1:0|Amazon Nova Pro,"
    "arn:aws:bedrock:ap-south-1:012857119299:inference-profile/apac.amazon.nova-lite-v1:0|Amazon Nova Lite,"
    "arn:aws:bedrock:ap-south-1:012857119299:inference-profile/apac.amazon.nova-micro-v1:0|Amazon Nova Micro,"
    "arn:aws:bedrock:ap-south-1:012857119299:inference-profile/global.amazon.nova-2-lite-v1:0|Amazon Nova 2 Lite,"
    
    "arn:aws:bedrock:ap-south-1::foundation-model/meta.llama3-70b-instruct-v1:0|Llama 3.1 70B,"
    "arn:aws:bedrock:ap-south-1::foundation-model/meta.llama3-8b-instruct-v1:0|Llama 3 8B Instruct,"
    
    "arn:aws:bedrock:ap-south-1::foundation-model/openai.gpt-oss-safeguard-120b|GPT OSS 120B,"
    "arn:aws:bedrock:ap-south-1::foundation-model/openai.gpt-oss-20b-1:0|GPT OSS 20B,"
    
    "arn:aws:bedrock:ap-south-1::foundation-model/qwen.qwen3-32b-v1:0|Qwen3 32B,"
    
    "arn:aws:bedrock:ap-south-1::foundation-model/mistral.mistral-7b-instruct-v0:2|Mistral 7B,"
    "arn:aws:bedrock:ap-south-1::foundation-model/mistral.mixtral-8x7b-instruct-v0:1|Mixtral 8x7B,"
    "arn:aws:bedrock:ap-south-1::foundation-model/mistral.mistral-large-3-675b-instruct|Mistral Large 3,"
    "arn:aws:bedrock:ap-south-1::foundation-model/mistral.mistral-large-2402-v1:0|Mistral Large,"
    
    "arn:aws:bedrock:ap-south-1::foundation-model/moonshotai.kimi-k2.5|Kimi K2.5,"
    "arn:aws:bedrock:ap-south-1::foundation-model/zai.glm-4.7|GLM 4.7,"
    "arn:aws:bedrock:ap-south-1::foundation-model/zai.glm-5|GLM 5 ,"
    
    "arn:aws:bedrock:ap-south-1::foundation-model/qwen.qwen3-235b-a22b-2507-v1:0|Qwen3 235B,"
    "arn:aws:bedrock:ap-south-1::foundation-model/deepseek.v3-v1:0|DeepSeek V3.1,"
)
    BEDROCK_EMBEDDING_MODEL_ID: str = "amazon.titan-embed-text-v2:0"
    BEDROCK_KB_ID: str = ""
    BEDROCK_KB_DATA_SOURCE_ID: str = ""
    BEDROCK_KB_NUM_RESULTS: int = 10
    # Vector store backing the KB. Drives whether HYBRID search is requested.
    # s3 / pgvector / pinecone -> SEMANTIC only. opensearch -> HYBRID supported.
    BEDROCK_KB_VECTOR_STORE: str = "s3"
    # Optional manual override: "HYBRID" | "SEMANTIC" | "" (auto from store above).
    BEDROCK_KB_SEARCH_TYPE_OVERRIDE: str = ""

    # ----- Reranking -----
    # Over-fetch K candidates from the KB, rerank, then trim to BEDROCK_KB_NUM_RESULTS.
    RERANK_ENABLED: bool = False
    RERANK_CANDIDATE_K: int = 4
    # If set, use AWS Bedrock Rerank API (Cohere Rerank v3.5 / Amazon Rerank v1).
    # Leave blank to fall back to the local FlashRank model below (if installed).
    BEDROCK_RERANK_MODEL_ARN: str = ""
    LOCAL_RERANK_MODEL: str = "ms-marco-MiniLM-L-12-v2"

    # ----- User-selectable models -----
    # Two models regular users can pick between. Same "modelId|Label"
    # format as BEDROCK_SELECTABLE_MODELS but limited to exactly the
    # models a normal USER / ADMIN is allowed to choose from.
    USER_SELECTABLE_MODELS: str = ""

    # ----- Per-user usage budget -----
    # Default monthly token budget (input + output) applied when a user
    # row has no per-user override. 0 disables the cap globally.
    DEFAULT_MONTHLY_TOKEN_LIMIT: int = 200_000
    BEDROCK_GUARDRAIL_ID: str = ""
    BEDROCK_GUARDRAIL_VERSION: str = "DRAFT"
    BEDROCK_MAX_TOKENS: int = 2048
    BEDROCK_TEMPERATURE: float = 0.2

    # ----- Email -----
    SES_FROM_EMAIL: str = "noreply@company.example"
    SES_REGION: str = "us-east-1"

    OUTLOOK_CLIENT_ID: str = ""
    OUTLOOK_CLIENT_SECRET: str = ""
    OUTLOOK_TENANT_ID: str = ""
    OUTLOOK_FROM_EMAIL: str = "support@company.example"
    OUTLOOK_ENABLED: bool = False

    # ----- Redis -----
    REDIS_URL: str = "redis://localhost:6379/0"

    # ----- Logging -----
    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = "./logs"
    LOG_ROTATION_MB: int = 20
    LOG_RETENTION_DAYS: int = 30

    # ----- Admin -----
    SUPERADMIN_EMAIL: str = "admin@company.example"

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, v):
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v or []


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
