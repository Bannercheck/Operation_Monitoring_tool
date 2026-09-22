from .json_parser import JsonParser
from .jsonl_parser import JsonlParser
from .csv_parser import CsvParser
from .kv_parser import KvParser
from .syslog_parser import SyslogParser
from .sap_parser import SapParser
from .text_parser import TextParser
from .access_parser import AccessParser
from .cef_parser import CefParser
from .winevt_parser import WinEvtParser
from .w3c_parser import W3cParser

PARSERS = {p.name: p for p in (JsonParser(), JsonlParser(), CsvParser(), SyslogParser(), SapParser(), KvParser(), TextParser(), AccessParser(), CefParser(), WinEvtParser(), W3cParser())}

__all__ = ["PARSERS"]
