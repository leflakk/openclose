"""Execute a job: chain skills in series, write summary.json, trigger notifications."""

from __future__ import annotations

from datetime import datetime, timezone

from openclose.id import generate_id
from openclose.jobs.notify import send_job_notification
from openclose.jobs.schema import (
    JobConfig,
    JobRunSummary,
    JobStatus,
    SkillRunSummary,
)
from openclose.jobs.storage import job_run_dir, write_summary
from openclose.log import get_logger
from openclose.skills.runner import execute_skill_to_files
from openclose.skills.storage import read_skill

log = get_logger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _run_folder(run_id: str) -> str:
    """`<ts>-<run_id>` with colons replaced so it's a safe path component."""
    return f"{_now_iso().replace(':', '-')}-{run_id}"


def _should_notify(job: JobConfig, summary: JobRunSummary) -> bool:
    """Decide whether to send a notification based on `notify_on` policy."""
    if not job.notification.channel:
        return False
    mode = job.notification.notify_on
    if mode == "always":
        return True
    if mode == "failure":
        return summary.status in ("failed", "partial")
    if mode == "verification_fail":
        # Phase 1: treat any failure as verification fail. The skill's
        # Verification section is still prose; no machine signal exists
        # yet. Future: have the agent emit a VERIFICATION: PASS/FAIL
        # marker and check for that explicitly.
        return summary.status in ("failed", "partial")
    return False


def _build_notification_text(
    job: JobConfig, summary: JobRunSummary, include_output: bool
) -> str:
    """Compose a concise notification message for the job's final status."""
    icon = {
        "passed": "✅",
        "failed": "❌",
        "partial": "⚠️",
    }.get(summary.status, "ℹ️")
    lines = [
        f"{icon} Job \"{job.name}\" — {summary.status.upper()}",
        f"Started: {summary.started_at}",
        f"Duration: {summary.duration_s:.1f}s",
        "",
    ]
    for s in summary.skills:
        status_icon = {
            "passed": "✓",
            "failed": "✗",
            "skipped": "–",
        }.get(s.status, "·")
        line = f"  {status_icon} {s.slug} ({s.duration_s:.1f}s)"
        if s.status == "failed" and s.error:
            line += f" — {s.error[:120]}"
        lines.append(line)

    if include_output:
        non_empty = [s for s in summary.skills if s.output_preview]
        if non_empty:
            lines.append("")
            lines.append("Outputs:")
            for s in non_empty:
                lines.append(f"  {s.slug}: {s.output_preview}")

    return "\n".join(lines)


def _derive_overall_status(skill_summaries: list[SkillRunSummary]) -> JobStatus:
    """Reduce per-skill statuses to a job status."""
    if not skill_summaries:
        return "passed"
    failed = sum(1 for s in skill_summaries if s.status == "failed")
    skipped = sum(1 for s in skill_summaries if s.status == "skipped")
    passed = sum(1 for s in skill_summaries if s.status == "passed")
    if failed == 0 and skipped == 0:
        return "passed"
    if passed == 0:
        return "failed"
    return "partial"


async def run_job(job: JobConfig) -> JobRunSummary:
    """Execute one full run of `job`: all skills in declared order, write summary."""
    run_id = generate_id()
    folder = _run_folder(run_id)
    run_dir = job_run_dir(job.id, folder)
    started = datetime.now(timezone.utc)

    # Seed summary with pending skills so a partial crash leaves useful state.
    skill_slots: list[SkillRunSummary] = [
        SkillRunSummary(slug=slug, status="pending") for slug in job.skills
    ]
    summary = JobRunSummary(
        job_id=job.id,
        job_name=job.name,
        run_id=run_id,
        started_at=started.isoformat(timespec="seconds"),
        status="running",
        skills=skill_slots,
    )
    write_summary(job.id, folder, summary)

    log.info("Job %s run %s: starting (%d skills)", job.id, run_id, len(job.skills))

    for i, slot in enumerate(skill_slots):
        slug = slot.slug
        skill = read_skill(slug)
        if skill is None:
            slot.status = "failed"
            slot.error = f"Skill not found: {slug}"
            slot.started_at = _now_iso()
            slot.finished_at = slot.started_at
            write_summary(job.id, folder, summary)
            if job.on_failure == "stop":
                # Mark remaining skills as skipped
                for later in skill_slots[i + 1:]:
                    later.status = "skipped"
                break
            continue

        slot.status = "running"
        slot.started_at = _now_iso()
        write_summary(job.id, folder, summary)

        jsonl_path = run_dir / f"{slug}.jsonl"
        out_path = run_dir / f"{slug}.out.md"

        skill_started = datetime.now(timezone.utc)
        try:
            result = await execute_skill_to_files(
                skill, jsonl_path, out_path,
                inputs=job.skill_parameters.get(slug) or None,
                trigger_message="",
            )
        except Exception as e:  # noqa: BLE001
            log.exception("Job %s: skill %s crashed", job.id, slug)
            slot.status = "failed"
            slot.error = str(e)
            slot.finished_at = _now_iso()
            slot.duration_s = (datetime.now(timezone.utc) - skill_started).total_seconds()
            slot.jsonl_file = jsonl_path.name
            slot.output_file = out_path.name
            write_summary(job.id, folder, summary)
            if job.on_failure == "stop":
                for later in skill_slots[i + 1:]:
                    later.status = "skipped"
                break
            continue

        slot.status = "passed" if result["status"] == "done" else "failed"
        slot.error = result.get("error", "")
        slot.finished_at = result.get("finished_at", _now_iso())
        slot.duration_s = (datetime.now(timezone.utc) - skill_started).total_seconds()
        slot.output_preview = result.get("output_preview", "")
        slot.jsonl_file = jsonl_path.name
        slot.output_file = out_path.name
        write_summary(job.id, folder, summary)

        if slot.status == "failed" and job.on_failure == "stop":
            for later in skill_slots[i + 1:]:
                later.status = "skipped"
            break

    # Finalize
    finished = datetime.now(timezone.utc)
    summary.finished_at = finished.isoformat(timespec="seconds")
    summary.duration_s = (finished - started).total_seconds()
    summary.status = _derive_overall_status(skill_slots)

    # Send notification if triggered
    if _should_notify(job, summary):
        text = _build_notification_text(job, summary, job.notification.include_output)
        try:
            ok, err = await send_job_notification(job.notification.channel, text)
            summary.notification_sent = ok
            if not ok:
                summary.notification_error = err
                log.warning("Job %s notify failed: %s", job.id, err)
        except Exception as e:  # noqa: BLE001
            summary.notification_sent = False
            summary.notification_error = str(e)
            log.exception("Job %s notify crashed", job.id)

    write_summary(job.id, folder, summary)
    log.info(
        "Job %s run %s: done (%s in %.1fs)",
        job.id, run_id, summary.status, summary.duration_s,
    )
    return summary
