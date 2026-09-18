from .json_parser import JsonParser
from .jsonl_parser import JsonlParser
from .csv_parser import CsvParser
from .kv_parser import KvParser
from .syslog_parser import SyslogParser
from .sap_parser import SapParser
from .text_parser import TextParser

PARSERS = {p.name: p for p in (JsonParser(), JsonlParser(), CsvParser(), SyslogParser(), SapParser(), KvParser(), TextParser())}

__all__ = ["PARSERS"]
