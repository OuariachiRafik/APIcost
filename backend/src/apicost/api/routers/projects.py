"""Project workspaces — UC-04.

A project is the isolation boundary for usage, rules, budgets, and the cache
namespace. It also carries every feature toggle, so the settings endpoint here
is what UC-14, UC-20, UC-21, and UC-22 will drive from the UI in later phases;
the fields already exist on the model, so the endpoint is defined once here
rather than being retrofitted three times.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from apicost.api.deps import CurrentUser, DbSession, require_project
from apicost.core.ids import new_id
from apicost.core.logging import get_logger
from apicost.db.models import Project
from apicost.db.redis import get_redis
from apicost.proxy.auth import purge_project_auth_cache

router = APIRouter(prefix="/projects", tags=["projects"])

_logger = get_logger(__name__)


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class ProjectSettingsRequest(BaseModel):
    """Every field optional — this is a partial update."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    cache_enabled: bool | None = None
    similarity_threshold: float | None = Field(default=None, ge=0.80, le=0.99)
    """Range fixed by UC-21. Outside it the cache is either useless or unsafe."""

    cache_ttl_seconds: int | None = Field(default=None, ge=60, le=2_592_000)
    routing_enabled: bool | None = None
    escalation_enabled: bool | None = None
    store_raw_content: bool | None = None


class ProjectResponse(BaseModel):
    id: str
    name: str
    created_at: datetime
    archived_at: datetime | None
    cache_enabled: bool
    similarity_threshold: float
    cache_ttl_seconds: int
    routing_enabled: bool
    escalation_enabled: bool
    store_raw_content: bool


def _to_response(project: Project) -> ProjectResponse:
    return ProjectResponse(
        id=project.id,
        name=project.name,
        created_at=project.created_at,
        archived_at=project.archived_at,
        cache_enabled=project.cache_enabled,
        similarity_threshold=project.similarity_threshold,
        cache_ttl_seconds=project.cache_ttl_seconds,
        routing_enabled=project.routing_enabled,
        escalation_enabled=project.escalation_enabled,
        store_raw_content=project.store_raw_content,
    )


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: CreateProjectRequest, user: CurrentUser, session: DbSession
) -> ProjectResponse:
    """Create a project with defaults from BUILD_SPEC §7."""
    project = Project(id=new_id(), user_id=user.id, name=payload.name.strip())
    session.add(project)
    await session.flush()

    _logger.info("project_created", user_id=user.id, project_id=project.id)
    return _to_response(project)


@router.get("", response_model=list[ProjectResponse])
async def list_projects(user: CurrentUser, session: DbSession) -> list[ProjectResponse]:
    result = await session.execute(
        select(Project).where(Project.user_id == user.id).order_by(Project.created_at.desc())
    )
    return [_to_response(project) for project in result.scalars()]


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: str, user: CurrentUser, session: DbSession) -> ProjectResponse:
    return _to_response(await require_project(project_id, user, session))


@router.put("/{project_id}/settings", response_model=ProjectResponse)
async def update_settings(
    project_id: str,
    payload: ProjectSettingsRequest,
    user: CurrentUser,
    session: DbSession,
) -> ProjectResponse:
    """Partial update of a project's toggles and thresholds."""
    project = await require_project(project_id, user, session)

    for field, value in payload.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(project, field, value)

    await session.flush()

    # The proxy resolves project settings through the auth cache, so a changed
    # threshold or toggle would otherwise not take effect for up to 60 s. The
    # user just moved a slider; they expect the next request to honour it.
    await purge_project_auth_cache(session, get_redis(), user.id, project.id)

    _logger.info(
        "project_settings_updated",
        user_id=user.id,
        project_id=project.id,
        fields=sorted(payload.model_dump(exclude_unset=True)),
    )
    return _to_response(project)
