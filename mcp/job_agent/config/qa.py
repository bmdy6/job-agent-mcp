"""Private standard answers, separate from portable repository defaults."""
from pydantic import BaseModel, ConfigDict, Field


class QASettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str = Field(default='$JOB_AGENT_HOME/qa.yaml', min_length=1)
