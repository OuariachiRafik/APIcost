"""The ARQ entrypoint's shape.

`arq apicost.worker.schedules.WorkerSettings` reads these as plain attributes,
so a wrong *type* here is not a type error anywhere — it is a worker that dies
on boot in production while every test still passes. That happened: a
`staticmethod` where arq wanted a `RedisSettings` instance meant the ledger
drain and every cron job never ran inside a container.
"""

from __future__ import annotations

from arq.connections import RedisSettings
from arq.cron import CronJob

from apicost.worker.schedules import WorkerSettings


def test_redis_settings_is_an_instance_not_a_callable() -> None:
    """arq does `settings.redis_settings.host`, never `redis_settings()`."""
    assert isinstance(WorkerSettings.redis_settings, RedisSettings)
    assert WorkerSettings.redis_settings.host
    assert WorkerSettings.redis_settings.port


def test_every_cron_job_is_a_real_cron_definition() -> None:
    assert WorkerSettings.cron_jobs
    for job in WorkerSettings.cron_jobs:
        assert isinstance(job, CronJob), job


def test_every_registered_function_is_callable() -> None:
    assert WorkerSettings.functions
    for function in WorkerSettings.functions:
        assert callable(function), function


def test_lifecycle_hooks_are_coroutine_functions() -> None:
    import inspect

    assert inspect.iscoroutinefunction(WorkerSettings.on_startup)
    assert inspect.iscoroutinefunction(WorkerSettings.on_shutdown)


def test_the_scheduled_jobs_cover_every_background_responsibility() -> None:
    """A job that exists but is never scheduled is a feature that never runs.

    UC-32's pattern scan shipped inert once already; this is the cheap check
    that would have caught it at the scheduling layer.
    """
    # arq prefixes the coroutine name with "cron:".
    scheduled = {job.name.removeprefix("cron:") for job in WorkerSettings.cron_jobs}
    for expected in (
        "drain_ledger_job",
        "rebuild_rollups_job",
        "cache_maintenance_job",
        "scan_usage_patterns_job",
        "ensure_partitions_job",
        "advisor_recommendations_job",
        "weekly_digest_job",
    ):
        assert expected in scheduled, f"{expected} is defined but never scheduled"
