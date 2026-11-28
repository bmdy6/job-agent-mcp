"""Compatibility context for one-use approvals in the owned store."""
from contextlib import contextmanager
from job_agent.store import Store


@contextmanager
def token_db():
    with Store() as store:
        try:
            yield store.connection
        except BaseException:
            if store.connection.in_transaction:
                store.connection.rollback()
            raise
