from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta


@dataclass(frozen=True)
class TaskPolicy:
    task_key: str
    task_name: str
    protocol_path: str
    m1_publish_time: time | None
    m1_reserve: timedelta
    research_timeout: timedelta
    m1_timeout: timedelta
    m2_timeout: timedelta
    knowledge_family: str

    def deadlines(
        self, scheduled_for: str, ready_at: datetime, *, reserve: timedelta | None = None,
    ) -> tuple[datetime, datetime]:
        scheduled = datetime.fromisoformat(scheduled_for.replace("Z", "+00:00"))
        if scheduled.tzinfo is None:
            raise ValueError("scheduled_for must be timezone-aware")
        if self.m1_publish_time is None:
            publish = max(ready_at, scheduled) + timedelta(minutes=30)
        else:
            publish = datetime.combine(scheduled.date(), self.m1_publish_time, scheduled.tzinfo)
            if publish <= scheduled:
                publish += timedelta(days=1)
        auto_submit = publish - (reserve or self.m1_reserve)
        return auto_submit, publish


_RESERVE = timedelta(minutes=10)
_INTRADAY = timedelta(minutes=5)

TASK_POLICIES = {
    "daily.opportunity.0900": TaskPolicy("daily.opportunity.0900", "A股 09:00盘前机会发现", "docs/protocols/09_OPPORTUNITY_DISCOVERY_PROTOCOL.md", time(9, 29), _RESERVE, timedelta(minutes=20), _INTRADAY, timedelta(minutes=15), "daily_open_close"),
    "daily.execution.0945": TaskPolicy("daily.execution.0945", "A股 09:45异常发现", "docs/protocols/07_DAILY_EXECUTION_PROTOCOL.md", time(10, 30), _RESERVE, _INTRADAY, _INTRADAY, timedelta(minutes=15), "daily_intraday"),
    "daily.execution.1030": TaskPolicy("daily.execution.1030", "A股 10:30趋势确认", "docs/protocols/07_DAILY_EXECUTION_PROTOCOL.md", time(14, 30), _RESERVE, _INTRADAY, _INTRADAY, timedelta(minutes=15), "daily_intraday"),
    "daily.execution.1430": TaskPolicy("daily.execution.1430", "A股 14:30操作决策", "docs/protocols/07_DAILY_EXECUTION_PROTOCOL.md", time(15, 20), _RESERVE, _INTRADAY, _INTRADAY, timedelta(minutes=15), "daily_intraday"),
    "daily.review.1520": TaskPolicy("daily.review.1520", "A股 15:20收盘复盘", "docs/protocols/07_DAILY_EXECUTION_PROTOCOL.md", time(16, 0), _RESERVE, _INTRADAY, _INTRADAY, timedelta(minutes=15), "daily_open_close"),
    "periodic.monthly": TaskPolicy("periodic.monthly", "A股月度复盘", "docs/protocols/08_PERIODIC_REVIEW_PROTOCOL.md", None, _RESERVE, timedelta(minutes=10), _INTRADAY, timedelta(minutes=15), "periodic_review"),
    "periodic.quarterly": TaskPolicy("periodic.quarterly", "A股季度复盘", "docs/protocols/08_PERIODIC_REVIEW_PROTOCOL.md", None, _RESERVE, timedelta(minutes=10), _INTRADAY, timedelta(minutes=15), "periodic_review"),
    "periodic.annual": TaskPolicy("periodic.annual", "A股年度复盘", "docs/protocols/08_PERIODIC_REVIEW_PROTOCOL.md", None, _RESERVE, timedelta(minutes=10), _INTRADAY, timedelta(minutes=15), "periodic_review"),
}

TERMINAL_STATES = {"complete", "reflected", "failed", "skipped", "missed"}
ACTIVE_STATES = {
    "queued", "researching_m0", "awaiting_h0", "voice_grace", "h0_locked",
    "researching_m1", "judging_m1", "m1_retry_wait", "m1_ready",
    "synthesizing_m2", "m2_deferred", "waiting_for_repair", "outcome_pending",
    "outcome_ready", "reflecting",
}
