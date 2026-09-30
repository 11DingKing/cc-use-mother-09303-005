"""领域错误类型。

所有业务规则失败都抛出 DomainError 的子类，HTTP 层据此映射状态码，
避免把底层 sqlite 异常泄漏给调用方。
"""
from __future__ import annotations


class DomainError(Exception):
    """业务规则错误基类。"""

    code = "domain_error"
    http_status = 400

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message, "details": self.details}


class ValidationError(DomainError):
    code = "validation_error"
    http_status = 400


class NotFoundError(DomainError):
    code = "not_found"
    http_status = 404


class PermissionError(DomainError):  # noqa: A001 - 领域内有意同名
    code = "forbidden"
    http_status = 403


class ConflictError(DomainError):
    """状态冲突、容量不足、唯一约束冲突等。"""

    code = "conflict"
    http_status = 409


class IdempotencyConflictError(ConflictError):
    code = "idempotency_conflict"
