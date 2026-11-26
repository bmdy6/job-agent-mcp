"""Separate verified resume delivery from contact-only automation.

These are pre-effect capability holds, not transient errors or permission
to clear an original-site risk pause. Extend only after an independently
verified formal resume workflow exists.
"""
from .models import Reason


def resume_block_reason(platform, settings):
    if platform == 'liepin':
        # Current concrete adapter can confirm a text greeting only. There is
        # no verified application/attachment submission and final receipt.
        return Reason.RESUME_FLOW_UNVERIFIED
    if platform == '51job' and not settings.attachment_name.strip():
        # The concrete adapter verifies the selected attachment receipt.
        # An application without an exact configured attachment is not that flow.
        return Reason.RESUME_ATTACHMENT_REQUIRED
    return None
