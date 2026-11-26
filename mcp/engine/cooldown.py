"""Configurable identity comparison. Platform HR IDs are never compared across sites."""
import re
import unicodedata
from pydantic import BaseModel, ConfigDict, Field, field_validator


class CooldownSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    days: int = Field(default=30, ge=1, le=3650, strict=True)
    company_patterns: list[str] = Field(default_factory=lambda: [
        r'[（(][^()（）]*[)）]', r'(?:股份有限责任公司|股份有限公司|有限责任公司|有限公司)$'])
    title_patterns: list[str] = Field(default_factory=lambda: [
        r'急招|诚聘|高薪|招聘', r'\d+(?:\.\d+)?\s*[-~—至]\s*\d+(?:\.\d+)?\s*[kK千万](?:[/·].*)?'])

    @field_validator('company_patterns','title_patterns')
    @classmethod
    def regexes(cls, values):
        for value in values:
            re.compile(value)
        return values


def normalize(value, patterns=()):
    value = unicodedata.normalize('NFKC', str(value or '')).casefold().strip()
    for pattern in patterns:
        value = re.sub(pattern, '', value, flags=re.IGNORECASE).strip()
    return re.sub(r'[\s·,，。:：()（）\[\]【】_-]+', '', value)


def duplicate(job, prior, settings, now):
    if now - prior['attempted_at'] >= settings.days * 86400:
        return False
    for field, patterns in [('company',settings.company_patterns), ('title',settings.title_patterns)]:
        a,b = normalize(job.get(field),patterns), normalize(prior.get(field),patterns)
        if not a or not b or a != b:
            return False
    a,b = str(job.get('hr_id') or ''), str(prior.get('hr_id') or '')
    if job['platform'] == prior['platform'] and a and b:
        return a == b
    a,b = normalize(job.get('hr_name')), normalize(prior.get('hr_name'))
    return a == b if a and b else True
