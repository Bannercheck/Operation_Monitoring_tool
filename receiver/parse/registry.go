package parse

// Registry is parsers.PARSERS (P01 section 6): stateless parser instances by name.
var Registry = map[string]Parser{
	"json":   JsonParser{},
	"jsonl":  JsonlParser{},
	"csv":    CsvParser{},
	"syslog": SyslogParser{},
	"sap":    SapParser{},
	"kv":     KvParser{},
	"text":   TextParser{},
	"access": AccessParser{},
	"cef":    CefParser{},
	"winevt": WinEvtParser{},
	"w3c":    W3cParser{},
	"grok":   GrokParser{},
}

// RegistryOrder is the PARSERS insertion order.
var RegistryOrder = []string{"json", "jsonl", "csv", "syslog", "sap", "kv", "text", "access", "cef", "winevt", "w3c", "grok"}
