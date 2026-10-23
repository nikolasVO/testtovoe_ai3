from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

PositiveID = Annotated[int, Field(gt=0, le=9223372036854775807, strict=True)]
Tokens = Annotated[int, Field(gt=0, le=1000000000, strict=True)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Generate(StrictModel):
    user_id: PositiveID
    model_slug: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1, max_length=20000)
    ref_images: list[HttpUrl] = Field(default_factory=list, max_length=100)


class Payment(StrictModel):
    payment_id: str = Field(min_length=1, max_length=200)
    user_id: PositiveID
    tokens: Tokens
    status: Literal["pending", "failed", "succeeded"]


class ProviderWebhook(StrictModel):
    attempt_id: UUID
    external_id: str = Field(min_length=1, max_length=200)
    status: Literal["completed", "failed"]
    result_url: HttpUrl | None = None
    error_type: Literal["technical", "moderation", "payload"] | None = None

    @model_validator(mode="after")
    def valid_outcome(self):
        if self.status == "completed" and (self.result_url is None or self.error_type is not None):
            raise ValueError("completed requires result_url and no error_type")
        if self.status == "failed" and (self.error_type is None or self.result_url is not None):
            raise ValueError("failed requires error_type and no result_url")
        return self
