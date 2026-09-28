from enum import StrEnum
from typing import Any


class ErrorCategory(StrEnum):
    TRANSIENT = "transient"
    VALIDATION = "validation"
    BUSINESS = "business"
    PERMISSION = "permission"


class ToolError(Exception):
    def __init__(
        self,
        category: ErrorCategory,
        code: str,
        description: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{category}/{code}: {description}")
        self.category = category
        self.code = code
        self.description = description
        self.details = details or {}

    @property
    def is_retryable(self) -> bool:
        return self.category is ErrorCategory.TRANSIENT

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "isError": True,
            "errorCategory": self.category.value,
            "errorCode": self.code,
            "isRetryable": self.is_retryable,
            "description": self.description,
        }
        if self.details:
            payload["details"] = self.details
        return payload
