"""Read-only optional anecdotes. Unconfirmed entries never leave this module."""
from typing import Literal
import re

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from job_agent.config import job_agent_home


class Anecdote(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: str = Field(min_length=1, max_length=80, pattern=r'^[a-z0-9][a-z0-9_-]*$')
    text: str = Field(min_length=1, max_length=100)
    tags: list[str] = Field(min_length=1, max_length=20)
    when: str = Field(default='', max_length=500)
    confirmed: bool = False
    source: str = Field(min_length=1, max_length=500)

    @field_validator('text', 'source')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('blank anecdote field')
        return value.strip()

    @field_validator('tags')
    @classmethod
    def tags_valid(cls, values):
        if any(not tag.strip() or len(tag) > 40 for tag in values):
            raise ValueError('invalid tag')
        return [tag.strip() for tag in values]


class GreetingFacts(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    version: Literal[1]
    facts: list[Anecdote] = Field(max_length=100)

    @model_validator(mode='after')
    def unique_ids(self):
        if len({fact.id for fact in self.facts}) != len(self.facts):
            raise ValueError('duplicate anecdote id')
        return self


def confirmed_texts():
    """Texts of all confirmed anecdotes; empty when the file is missing or invalid."""
    path = job_agent_home() / 'greeting-facts.yaml'
    try:
        with path.open('rb') as stream:
            raw = stream.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            return []
        data = GreetingFacts.model_validate(yaml.safe_load(raw.decode('utf-8')))
    except (OSError, UnicodeError, ValueError, ValidationError, yaml.YAMLError):
        return []
    return [fact.text for fact in data.facts if fact.confirmed is True]


def select_anecdote(job):
    """Choose at most one confirmed story by literal tag overlap; stable file-order ties."""
    path = job_agent_home() / 'greeting-facts.yaml'
    try:
        with path.open('rb') as stream:
            raw = stream.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise ValueError('oversized file')
        data = GreetingFacts.model_validate(yaml.safe_load(raw.decode('utf-8')))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, ValueError, ValidationError, yaml.YAMLError):
        raise ValueError('invalid_greeting_facts') from None
    context = ' '.join(str(job.get(k) or '') for k in ('jd', 'title')).casefold()
    # The when field is human guidance, not executable instructions. Only literal
    # JD keywords qualify; broad personality labels never establish relevance.
    generic = {'主动性', '务实', '省钱', '效率'}
    scored = []
    for fact in data.facts:
        if fact.confirmed is not True:
            continue
        keywords = set(fact.tags) - generic
        keywords.update(re.findall(r'[A-Za-z]+|[\u4e00-\u9fff]+', fact.when.removeprefix('JD 提到').removeprefix('JD提到').split('或者')[0]))
        score = sum(term.casefold() in context for term in keywords if term)
        scored.append((score, fact))
    if not scored:
        return None
    relevance, selected = max(scored, key=lambda pair: pair[0])
    return selected.model_dump() if relevance else None
