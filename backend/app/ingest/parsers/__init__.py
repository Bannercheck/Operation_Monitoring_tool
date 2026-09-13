from .jsonl import JsonlParser
from .csv import CsvParser
from .syslog import SyslogParser
from .kv import KvParser
from .plain import PlainParser

PARSERS = {p.name: p for p in (JsonlParser(), CsvParser(), SyslogParser(), KvParser(), PlainParser())}

__all__ = ["PARSERS"]
