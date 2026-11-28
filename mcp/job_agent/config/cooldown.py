"""Identity cooldown configuration."""
import re
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
