from typing import Literal
from uuid import UUID

from pydantic import Field, HttpUrl, model_validator

from app.schemas import StrictModel


class Behavior(StrictModel):
    failure_rate: float = Field(default=0, ge=0, le=1)
    error_type: Literal["technical", "moderation", "payload"] = "technical"
    sync_error: Literal["none", "503", "timeout"] = "none"
    delay_min: float = Field(default=2, ge=0, le=600)
    delay_max: float = Field(default=20, ge=0, le=600)
    duplicates: int = Field(default=1, ge=1, le=20)
    concurrent_duplicates: bool = True
    drop_webhook: bool = False
    late_delay: float | None = Field(default=None, gt=0, le=3600)

    @model_validator(mode="after")
    def valid_delay(self):
        if self.delay_max < self.delay_min:
            raise ValueError("delay_max must be >= delay_min")
        return self


class TaskRequest(StrictModel):
    attempt_id: UUID
    model_slug: str
    prompt: str
    ref_images: list[HttpUrl]
    callback_url: HttpUrl
    scenario_key: str | None = Field(default=None, max_length=200)
