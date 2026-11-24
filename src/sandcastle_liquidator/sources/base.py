from typing import Protocol

from sandcastle_liquidator.models import Log, LogQuery


class LogSource(Protocol):
    def get_logs(self, query: LogQuery) -> list[Log]:
        """Every matching log in the query's inclusive block range."""
        ...
