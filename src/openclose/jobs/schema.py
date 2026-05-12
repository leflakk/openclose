"""Pydantic models for Jobs — scheduled triggers that chain skills in series."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


TimingMode = Literal["one_shot", "recurring"]
OnFailure = Literal["stop", "continue"]
NotifyOn = Literal["failure", "always", "verification_fail"]
SkillStatus = Literal["pending", "running", "passed", "failed", "skipped"]
JobStatus = Literal["pending", "running", "passed", "failed", "partial", "expired"]


class JobTiming(BaseModel):
    """When a job fires."""

    mode: TimingMode
    # recurring
    cron: str = ""
    # one-shot
    run_at: str = ""
    executed: bool = False
    # common
    timezone: str = "UTC"


class JobNotification(BaseModel):
    """Optional out-of-band notification after a run."""

    channel: str = ""  # deliver_message alias ("me", "ops"); empty = no notification
    notify_on: NotifyOn = "failure"
    include_output: bool = True


class JobConfig(BaseModel):
    """Persisted job definition, written to `<job-id>.json`."""

    id: str
    name: str
    skills: list[str] = Field(default_factory=list)  # ordered skill slugs
    skill_parameters: dict[str, dict[str, str]] = Field(default_factory=dict)
    timing: JobTiming
    on_failure: OnFailure = "stop"
    notification: JobNotification = Field(default_factory=JobNotification)
    enabled: bool = True
    created_at: str = ""
    version: int = 1


# ── Run summaries (written to `jobs/<id>/<ts>-<run_id>/summary.json`) ──


class SkillRunSummary(BaseModel):
    """One skill's slot in a job's summary.json."""

    slug: str
    status: SkillStatus = "pending"
    started_at: str = ""
    finished_at: str = ""
    duration_s: float = 0.0
    error: str = ""
    output_preview: str = ""
    jsonl_file: str = ""  # basename within the run folder
    output_file: str = ""


class JobRunSummary(BaseModel):
    """Top-level `summary.json` for one job run."""

    job_id: str
    job_name: str
    run_id: str
    started_at: str = ""
    finished_at: str = ""
    duration_s: float = 0.0
    status: JobStatus = "running"
    skills: list[SkillRunSummary] = Field(default_factory=list)
    notification_sent: bool = False
    notification_error: str = ""


# ── Request payloads ──


class JobSaveRequest(BaseModel):
    """POST/PUT body for creating or updating a job (id is ignored on save)."""

    name: str
    skills: list[str] = Field(default_factory=list)
    skill_parameters: dict[str, dict[str, str]] = Field(default_factory=dict)
    timing: JobTiming
    on_failure: OnFailure = "stop"
    notification: JobNotification = Field(default_factory=JobNotification)
    enabled: bool = True


class JobEnableRequest(BaseModel):
    enabled: bool


class JobRunNowRequest(BaseModel):
    """Optional payload for manual runs — currently empty, kept for future flags."""

    pass


class CronParseRequest(BaseModel):
    """Natural-language or literal cron translation request."""

    text: str
    timezone: str = "UTC"
