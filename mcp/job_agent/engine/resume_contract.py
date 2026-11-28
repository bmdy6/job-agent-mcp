"""Separate verified resume delivery from contact-only automation.

These are pre-effect capability holds, not transient errors or permission
to clear an original-site risk pause. Extend only after an independently
verified formal resume workflow exists.
"""
from job_agent.engine.models import Reason
from job_agent.platforms.spec import spec_for


def resume_block_reason(platform, settings):
    spec = spec_for(platform)
    if not spec.resume_flow_verified:
        return Reason.RESUME_FLOW_UNVERIFIED
    if spec.requires_attachment_for_resume and not settings.attachment_name.strip():
        return Reason.RESUME_ATTACHMENT_REQUIRED
    return None
