"""分析层：状态机、幂等、重试/降级、心跳/取消、Run 执行器。

阶段 08 的可靠性机制集中在这里；阶段 09 的 Pipeline 复用它们。
"""

from __future__ import annotations

from app.analysis.heartbeat import (
    DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
    CancelledError,
    HeartbeatRecord,
    check_cancelled,
    heartbeat_deadline,
    is_zombie,
    validate_heartbeat_timeout,
)
from app.analysis.idempotency import (
    PIPELINE_VERSION,
    ErrorKind,
    IdempotencyInputs,
    NonRetryableError,
    canonical_json,
    classify_error,
    compute_idempotency_key,
    is_retryable,
    make_idempotency_key,
)
from app.analysis.knowledge_staging import (
    candidate_id,
    load_candidates,
    staging_path,
    write_candidates,
)
from app.analysis.persistence import (
    EvidencePersistenceError,
    PersistenceResult,
    assert_no_evidence_violation,
    persist_insights,
)
from app.analysis.retry import (
    MAX_RETRIES,
    FallbackOutcome,
    RetryOutcome,
    RetryPolicy,
    call_with_fallback,
    call_with_retry,
    downgrade_ladder,
)
from app.analysis.runner import RunExecutor, RunOutcome, RunRequest, create_run
from app.analysis.state_machine import (
    ALLOWED_TRANSITIONS,
    IllegalTransitionError,
    StateMachine,
    allowed_targets,
    can_transition,
    is_terminal,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "DEFAULT_HEARTBEAT_TIMEOUT_SECONDS",
    "MAX_RETRIES",
    "PIPELINE_VERSION",
    "CancelledError",
    "ErrorKind",
    "EvidencePersistenceError",
    "FallbackOutcome",
    "HeartbeatRecord",
    "IdempotencyInputs",
    "IllegalTransitionError",
    "NonRetryableError",
    "PersistenceResult",
    "RetryOutcome",
    "RetryPolicy",
    "RunExecutor",
    "RunOutcome",
    "RunRequest",
    "StateMachine",
    "allowed_targets",
    "assert_no_evidence_violation",
    "call_with_fallback",
    "call_with_retry",
    "can_transition",
    "candidate_id",
    "canonical_json",
    "check_cancelled",
    "classify_error",
    "compute_idempotency_key",
    "create_run",
    "downgrade_ladder",
    "heartbeat_deadline",
    "is_retryable",
    "is_terminal",
    "is_zombie",
    "load_candidates",
    "make_idempotency_key",
    "persist_insights",
    "staging_path",
    "validate_heartbeat_timeout",
    "write_candidates",
]
