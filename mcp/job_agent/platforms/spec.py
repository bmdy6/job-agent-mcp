"""Declarative platform capability & policy contract.

Core layers (engine, service, store, monitor, collect) should read capabilities
from this table instead of scattering `if platform == '...'` checks.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class PlatformSpec:
    name: str
    label: str
    module_dir: str
    action_id: str = 'apply_resume'
    supports_followup: bool = False
    preflight_attachment: bool = True
    supports_application_receipt: bool = False
    supports_readonly_requeue: bool = False
    resume_flow_verified: bool = True
    requires_attachment_for_resume: bool = False
    safe_daily_cap: int | None = None
    summary_note: str = ''


SPECS: dict[str, PlatformSpec] = {
    'boss': PlatformSpec(
        name='boss',
        label='BOSS',
        module_dir='boss',
        action_id='greeting',
        supports_followup=True,
        safe_daily_cap=150,
    ),
    'zhilian': PlatformSpec(
        name='zhilian',
        label='智联',
        module_dir='zhilian',
        supports_application_receipt=True,
        supports_readonly_requeue=True,
        safe_daily_cap=100,
    ),
    'liepin': PlatformSpec(
        name='liepin',
        label='猎聘',
        module_dir='liepin',
        resume_flow_verified=False,
    ),
    '51job': PlatformSpec(
        name='51job',
        label='51job',
        module_dir='job51',
        preflight_attachment=False,
        requires_attachment_for_resume=True,
        summary_note='51job：成功表示申请后指定附件回执已确认；桌面流程未发送招呼语，已申请和招呼语送达分别统计。',
    ),
    'nowcoder': PlatformSpec(
        name='nowcoder',
        label='牛客',
        module_dir='nowcoder',
    ),
    'maimai': PlatformSpec(
        name='maimai',
        label='脉脉',
        module_dir='maimai',
    ),
}

PLATFORM_LABELS: dict[str, str] = {name: spec.label for name, spec in SPECS.items()}
SAFE_DAILY_CAPS: dict[str, int] = {
    name: spec.safe_daily_cap for name, spec in SPECS.items() if spec.safe_daily_cap is not None
}


def spec_for(platform: str) -> PlatformSpec:
    if platform not in SPECS:
        raise ValueError(f'Unknown platform: {platform}')
    return SPECS[platform]
