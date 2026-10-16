"""Which technology each integration runs on: "REST API · FastAPI", "Oracle Net · Oracle Database 19c (Oracle JDBC)",
"SAP RFC · SAP ECC 6.0 (SAP JCo)", "HTTPS · nginx → uvicorn (FastAPI)".

Evidence comes from every source Watchover already has, each with a weight and a Turkish + English sentence:

  port       the wire protocol only (1521 Oracle Net, 1433 TDS, 9092 Kafka ...). A port never names a framework: 8000 is "HTTP API".
  process    the client process name the agent saw (java, python3, w3wp, nginx ...): the client runtime.
  listener   the process listening on the target's port when the target runs an agent too (uvicorn, sqlservr, tnslsnr, gwrd ...).
  agent      the agent's fingerprint tags of that process (flow.tech): a fixed dictionary matched on the server itself against the
             process's command line and mapped libraries (agent.py FINGERPRINTS); only the tag names ever leave the host.
  trace      span / event attributes seen for the host at ingest (db.system, messaging.system, rpc.system, instrumentation scope,
             telemetry.sdk.language, webengine.name, http.route), kept as canonical tokens in tech_seen.
  log        what the host's log files are (IIS W3C, nginx / Apache access, catalina, SAP work process, Oracle alert ...) and logger
             name prefixes (org.springframework, uvicorn ...), kept the same way.
  inventory  os / application / role / vendor_model of the host.
  banner     the vulnerability scanner's passive banner of that port (OpenSSH_8.9p1, Microsoft-IIS/10.0 ...).
  manual     an override a user typed (integ_tech: one row per team and integration).

Confidence: "kesin" with direct proof (agent fingerprint, trace instrumentation, banner, manual); "yüksek" when two independent
sources agree; "olası" otherwise. Unknown parts stay null: nothing is invented. Evidence is cached per host for a minute; the
flows themselves are read once per request by FlowStore.host_map, which hands the listeners and proxy upstreams over in `ctx`.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from .flows import category, port_name

UTC = timezone.utc
CONF = ("olası", "yüksek", "kesin")
W = {"port": 1, "process": 2, "listener": 2, "inventory": 2, "log": 2, "agent": 3, "trace": 3, "banner": 3, "manual": 5}
DIRECT = {"agent", "trace", "banner", "manual"}


def _t(name: str, layer: str, role: str, rt: str = "", proto: str = "", api: bool = False) -> dict:
    return {"name": name, "layer": layer, "role": role, "rt": rt, "proto": frozenset(proto.split()), "api": api}


# ---------------------------------------------------------------- the technology catalogue
# role: runtime | server (application / web server) | proxy | framework | worker | app (business application) | db | broker |
#       dir (directory / identity) | file | mail | lib (client library: only on the client side of a matching protocol)
# proto: the protocol keys (flows.port_name) the technology serves; "http" stands for every HTTP-ish port.
TECH: dict[str, dict] = {
    "python": _t("Python", "runtime", "runtime"), "java": _t("Java", "runtime", "runtime"), "node": _t("Node.js", "runtime", "runtime"),
    "dotnet": _t(".NET", "runtime", "runtime"), "php": _t("PHP", "runtime", "runtime"), "erlang": _t("Erlang/OTP", "runtime", "runtime"), "go": _t("Go", "runtime", "runtime"),
    "uvicorn": _t("uvicorn", "web", "server", "python", "http"), "gunicorn": _t("gunicorn", "web", "server", "python", "http"),
    "hypercorn": _t("Hypercorn", "web", "server", "python", "http"), "uwsgi": _t("uWSGI", "web", "server", "python", "http"),
    "tomcat": _t("Tomcat", "web", "server", "java", "http"), "weblogic": _t("WebLogic", "web", "server", "java", "http"),
    "jboss": _t("JBoss / WildFly", "web", "server", "java", "http"), "websphere": _t("WebSphere", "web", "server", "java", "http"),
    "jetty": _t("Jetty", "web", "server", "java", "http"), "iis": _t("IIS", "web", "server", "dotnet", "http"), "kestrel": _t("Kestrel", "web", "server", "dotnet", "http"),
    "php-fpm": _t("PHP-FPM", "web", "server", "php", "http"),
    "nginx": _t("nginx", "web", "proxy", "", "http"), "apache-httpd": _t("Apache httpd", "web", "proxy", "", "http"), "haproxy": _t("HAProxy", "web", "proxy", "", "http"),
    "envoy": _t("Envoy", "web", "proxy", "", "http"), "traefik": _t("Traefik", "web", "proxy", "", "http"),
    "fastapi": _t("FastAPI", "app", "framework", "python", "http", api=True), "starlette": _t("Starlette", "app", "framework", "python", "http", api=True),
    "django": _t("Django", "app", "framework", "python", "http"), "flask": _t("Flask", "app", "framework", "python", "http"),
    "aiohttp": _t("aiohttp", "app", "framework", "python", "http"), "tornado": _t("Tornado", "app", "framework", "python", "http"),
    "celery": _t("Celery", "app", "worker", "python"), "spring-boot": _t("Spring Boot", "app", "framework", "java", "http"),
    "aspnet": _t("ASP.NET", "app", "framework", "dotnet", "http"), "aspnetcore": _t("ASP.NET Core", "app", "framework", "dotnet", "http", api=True),
    "express": _t("Express", "app", "framework", "node", "http", api=True), "nestjs": _t("NestJS", "app", "framework", "node", "http", api=True),
    "sap-java": _t("SAP NetWeaver Java", "app", "server", "java", "http sap-http sap-https"), "keycloak": _t("Keycloak", "identity", "dir", "java", "http"),
    "oracle-ebs": _t("Oracle E-Business Suite", "app", "app"), "sap-app": _t("SAP", "app", "app"), "dynamics": _t("Microsoft Dynamics", "app", "app"),
    "oracle-db": _t("Oracle Database", "db", "db", "", "oracle"), "sqlserver": _t("SQL Server", "db", "db", "", "sqlserver"),
    "postgres": _t("PostgreSQL", "db", "db", "", "postgres"), "mysql": _t("MySQL", "db", "db", "", "mysql"), "mariadb": _t("MariaDB", "db", "db", "", "mysql"),
    "hana": _t("SAP HANA", "db", "db", "", "hana"), "mongodb": _t("MongoDB", "db", "db", "", "mongodb"), "redis": _t("Redis", "db", "db", "", "redis"),
    "memcached": _t("Memcached", "db", "db", "", "memcached"), "cassandra": _t("Cassandra", "db", "db", "java", "cassandra"),
    "elasticsearch": _t("Elasticsearch", "db", "db", "java", "elasticsearch"),
    "kafka-broker": _t("Apache Kafka", "messaging", "broker", "java", "kafka"), "rabbitmq": _t("RabbitMQ", "messaging", "broker", "erlang", "amqp rabbitmq"),
    "ibm-mq": _t("IBM MQ", "messaging", "broker", "", "ibm-mq"), "activemq": _t("ActiveMQ", "messaging", "broker", "java", "amqp"),
    "zookeeper": _t("ZooKeeper", "messaging", "broker", "java", "zookeeper"), "nats": _t("NATS", "messaging", "broker", "", "nats"),
    "sap-abap": _t("SAP NetWeaver ABAP", "app", "server", "", "sap-dialog sap-rfc sap-ms sap-http sap-https"),
    "sap-gateway": _t("SAP Gateway", "app", "server", "", "sap-rfc"), "sap-ms-server": _t("SAP Message Server", "app", "server", "", "sap-ms"),
    "sap-icm": _t("SAP ICM", "web", "server", "", "sap-http sap-https http"),
    "ad": _t("Active Directory", "identity", "dir", "", "ldap ldaps kerberos"), "openldap": _t("OpenLDAP", "identity", "dir", "", "ldap ldaps"),
    "openssh": _t("OpenSSH", "file", "file", "", "ssh"), "samba": _t("Samba", "file", "file", "", "smb"),
    "exchange": _t("Exchange", "mail", "mail", "dotnet", "smtp smtps imap pop3"), "postfix": _t("Postfix", "mail", "mail", "", "smtp smtps"),
    # client libraries
    "ojdbc": _t("Oracle JDBC", "lib", "lib", "java", "oracle"), "oci": _t("Oracle OCI", "lib", "lib", "", "oracle"),
    "oracledb": _t("python-oracledb", "lib", "lib", "python", "oracle"), "cx_oracle": _t("cx_Oracle", "lib", "lib", "python", "oracle"),
    "odp.net": _t("ODP.NET", "lib", "lib", "dotnet", "oracle"), "libpq": _t("libpq", "lib", "lib", "", "postgres"),
    "psycopg": _t("psycopg", "lib", "lib", "python", "postgres"), "asyncpg": _t("asyncpg", "lib", "lib", "python", "postgres"),
    "pgjdbc": _t("PostgreSQL JDBC", "lib", "lib", "java", "postgres"), "npgsql": _t("Npgsql", "lib", "lib", "dotnet", "postgres"),
    "mysqlclient": _t("libmysqlclient", "lib", "lib", "", "mysql"), "mariadb-client": _t("MariaDB Connector/C", "lib", "lib", "", "mysql"),
    "mysql-connector-j": _t("MySQL Connector/J", "lib", "lib", "java", "mysql"), "sqlclient": _t("ADO.NET SqlClient", "lib", "lib", "dotnet", "sqlserver"),
    "msodbcsql": _t("Microsoft ODBC Driver", "lib", "lib", "", "sqlserver"), "mssql-jdbc": _t("Microsoft JDBC", "lib", "lib", "java", "sqlserver"),
    "pyodbc": _t("pyodbc", "lib", "lib", "python", "sqlserver oracle postgres mysql hana"), "odbc": _t("ODBC", "lib", "lib", "", "sqlserver oracle postgres mysql hana"),
    "jdbc": _t("JDBC", "lib", "lib", "java", "oracle sqlserver postgres mysql hana"),
    "sapjco": _t("SAP JCo", "lib", "lib", "java", "sap-rfc sap-dialog sap-ms"), "sapnwrfc": _t("SAP NW RFC SDK", "lib", "lib", "", "sap-rfc sap-dialog sap-ms"),
    "pyrfc": _t("PyRFC", "lib", "lib", "python", "sap-rfc sap-dialog sap-ms"), "hana-jdbc": _t("SAP HANA JDBC", "lib", "lib", "java", "hana"),
    "hana-client": _t("SAP HANA client", "lib", "lib", "", "hana"), "librdkafka": _t("librdkafka", "lib", "lib", "", "kafka"),
    "kafka-clients": _t("Kafka Java client", "lib", "lib", "java", "kafka"), "spring-kafka": _t("Spring Kafka", "lib", "lib", "java", "kafka"),
    "amqp-client": _t("AMQP client", "lib", "lib", "", "amqp"), "spring-rabbit": _t("Spring AMQP", "lib", "lib", "java", "amqp"),
    "grpc": _t("gRPC", "lib", "lib", "", "http"), "cxf": _t("Apache CXF", "lib", "lib", "java", "http"), "axis2": _t("Apache Axis2", "lib", "lib", "java", "http"),
    "jaxws": _t("JAX-WS", "lib", "lib", "java", "http"), "wcf": _t("WCF", "lib", "lib", "dotnet", "http"), "http-client": _t("HTTP client", "lib", "lib", "", "http"),
    "redis-client": _t("Redis client", "lib", "lib", "", "redis"), "mongo-client": _t("MongoDB driver", "lib", "lib", "", "mongodb"),
    "libldap": _t("OpenLDAP client", "lib", "lib", "", "ldap ldaps"), "jsch": _t("JSch", "lib", "lib", "java", "ssh"), "libssh": _t("libssh", "lib", "lib", "", "ssh"),
    "sftp": _t("SFTP", "lib", "lib", "", "ssh"),
}
HELPER_TAGS = {"pydantic_core", "uvloop", "httptools"}       # fingerprints that support a guess but name nothing on their own
AMBIGUOUS_TAGS = {"aiohttp", "tornado"}                      # a loaded library that is as often an HTTP client: never proof of the server framework
SOAP_LIBS = {"cxf", "axis2", "jaxws", "wcf"}
SOAP_ONLY = SOAP_LIBS - {"cxf"}                              # Apache CXF serves JAX-RS (REST) too: on one side only it proves nothing
GENERIC_SERVERS = {"uvicorn", "gunicorn", "hypercorn", "uwsgi", "kestrel"}   # a single hop shows the framework they run
SUPERSEDES = {"fastapi": {"starlette"}, "spring-kafka": {"kafka-clients"}, "spring-rabbit": {"amqp-client"}, "aspnetcore": {"kestrel"}}
ROLE_RANK = {"app": 0, "framework": 1, "server": 2, "proxy": 3, "worker": 4}
LAYERS = ("app", "web", "db", "messaging", "identity", "file", "mail", "runtime", "os")

# process name (agent /proc comm) -> technology
PROC = {"java": "java", "python": "python", "python2": "python", "python3": "python", "uvicorn": "uvicorn", "gunicorn": "gunicorn", "hypercorn": "hypercorn",
        "uwsgi": "uwsgi", "celery": "celery", "node": "node", "nodejs": "node", "dotnet": "dotnet", "w3wp": "iis", "w3wp.exe": "iis", "php-fpm": "php-fpm",
        "php": "php", "nginx": "nginx", "httpd": "apache-httpd", "apache2": "apache-httpd", "haproxy": "haproxy", "envoy": "envoy", "traefik": "traefik",
        "sqlservr": "sqlserver", "sqlservr.exe": "sqlserver", "oracle": "oracle-db", "tnslsnr": "oracle-db", "postgres": "postgres", "postmaster": "postgres",
        "mysqld": "mysql", "mariadbd": "mariadb", "redis-server": "redis", "mongod": "mongodb", "memcached": "memcached", "hdbindexserver": "hana",
        "hdbnameserver": "hana", "beam.smp": "erlang", "disp+work": "sap-abap", "gwrd": "sap-gateway", "msg_server": "sap-ms-server", "icman": "sap-icm",
        "jstart": "sap-java", "sshd": "openssh", "sftp-server": "openssh", "slapd": "openldap", "lsass": "ad", "lsass.exe": "ad", "smbd": "samba",
        "sftp": "sftp", "scp": "sftp"}
CLIENT_PROTO_PROC = {"sftp": "SFTP", "scp": "SCP", "rsync": "rsync"}

# protocol key -> display name; HTTP-ish keys; the product a product-specific wire protocol implies (olası on its own)
PROTO = {"http": "HTTP", "https": "HTTPS", "weblogic": "HTTP", "sap-http": "HTTP (SAP ICM)", "sap-https": "HTTPS (SAP ICM)", "kibana": "HTTP",
         "elasticsearch": "HTTP (REST)", "rabbitmq": "HTTP", "watchover": "HTTP", "kubernetes": "HTTPS", "oracle": "Oracle Net", "sqlserver": "TDS",
         "postgres": "PostgreSQL", "mysql": "MySQL", "hana": "HANA SQL", "mongodb": "MongoDB", "redis": "Redis (RESP)", "memcached": "Memcached",
         "cassandra": "CQL", "kafka": "Kafka", "amqp": "AMQP", "ibm-mq": "IBM MQ", "nats": "NATS", "zookeeper": "ZooKeeper", "sap-dialog": "SAP DIAG",
         "sap-rfc": "SAP RFC", "sap-ms": "SAP Message Server", "ldap": "LDAP", "ldaps": "LDAPS", "kerberos": "Kerberos", "smtp": "SMTP", "smtps": "SMTPS",
         "imap": "IMAP", "pop3": "POP3", "ssh": "SSH", "smb": "SMB", "nfs": "NFS", "dns": "DNS", "ntp": "NTP", "rdp": "RDP"}
HTTPISH = {"http", "https", "weblogic", "sap-http", "sap-https", "kibana", "elasticsearch", "rabbitmq", "watchover", "kubernetes"}
TLS_PROTOS = {"https", "ldaps", "smtps", "sap-https", "kubernetes"}
TLS_PORTS = {443, 8443, 636, 465, 993, 995, 50001, 2484, 5671, 9093, 6443}
IMPLIED = {"oracle": "oracle-db", "sqlserver": "sqlserver", "postgres": "postgres", "mysql": "mysql", "hana": "hana", "mongodb": "mongodb", "redis": "redis",
           "memcached": "memcached", "cassandra": "cassandra", "kafka": "kafka-broker", "ibm-mq": "ibm-mq", "nats": "nats", "zookeeper": "zookeeper",
           "sap-dialog": "sap-abap", "sap-rfc": "sap-abap", "sap-ms": "sap-abap"}

# ---------------------------------------------------------------- what traces and logs say (canonical tokens only are stored)
TRACE_ATTRS = ("db.system", "db.system.name", "messaging.system", "rpc.system", "otel.scope.name", "otel.library.name", "instrumentation.scope",
               "otel.instrumentation_scope.name", "telemetry.sdk.language", "process.runtime.name", "webengine.name", "http.route")
LOGGER_ATTRS = ("logger", "logger_name", "logger.name", "log.logger", "loggername", "category")
DB_SYSTEM = {"oracle": "oracle-db", "mssql": "sqlserver", "sqlserver": "sqlserver", "microsoft.sql_server": "sqlserver", "postgresql": "postgres",
             "mysql": "mysql", "mariadb": "mariadb", "hanadb": "hana", "sap.hana": "hana", "redis": "redis", "mongodb": "mongodb",
             "elasticsearch": "elasticsearch", "cassandra": "cassandra", "memcached": "memcached"}
MSG_SYSTEM = {"kafka": "kafka-broker", "rabbitmq": "rabbitmq", "activemq": "activemq", "ibm_mq": "ibm-mq", "ibmmq": "ibm-mq", "nats": "nats"}
RPC_SYSTEM = {"grpc": "grpc", "dotnet_wcf": "wcf"}
LANG = {"python": "python", "java": "java", "dotnet": "dotnet", "nodejs": "node", "node": "node", "go": "go", "php": "php", "erlang": "erlang", "cpython": "python"}
SCOPES = [(re.compile(p), k) for p, k in (
    (r"aiohttp[_-]client|requests|httpx|urllib|okhttp|httpclient|http-url-connection|undici|fetch", "http-client"), (r"fastapi", "fastapi"),
    (r"starlette", "starlette"), (r"flask", "flask"), (r"django", "django"), (r"aiohttp", "aiohttp"), (r"tornado", "tornado"), (r"celery", "celery"),
    (r"spring-kafka", "spring-kafka"), (r"spring-rabbit", "spring-rabbit"), (r"spring", "spring-boot"), (r"tomcat", "tomcat"), (r"jetty", "jetty"),
    (r"aspnetcore", "aspnetcore"), (r"express", "express"), (r"nestjs", "nestjs"), (r"psycopg", "psycopg"), (r"asyncpg", "asyncpg"),
    (r"oracledb|cx[_-]oracle", "oracledb"), (r"pyodbc|pymssql", "pyodbc"), (r"sqlclient", "sqlclient"), (r"npgsql", "npgsql"), (r"jdbc", "jdbc"),
    (r"confluent", "librdkafka"), (r"kafka", "kafka-clients"), (r"rabbitmq|pika|amqp", "amqp-client"), (r"grpc", "grpc"), (r"redis", "redis-client"),
    (r"mongo", "mongo-client"))]
WEBENGINES = [(re.compile(p), k) for p, k in ((r"tomcat", "tomcat"), (r"weblogic", "weblogic"), (r"jetty", "jetty"), (r"undertow|wildfly|jboss", "jboss"),
                                               (r"websphere|liberty", "websphere"), (r"\biis\b", "iis"), (r"kestrel", "kestrel"))]
LOGGERS = (("org.springframework.kafka", "spring-kafka"), ("org.springframework.amqp", "spring-rabbit"), ("org.springframework", "spring-boot"),
           ("org.apache.catalina", "tomcat"), ("org.apache.coyote", "tomcat"), ("org.apache.tomcat", "tomcat"), ("weblogic", "weblogic"),
           ("org.jboss", "jboss"), ("org.wildfly", "jboss"), ("io.undertow", "jboss"), ("com.ibm.ws", "websphere"), ("org.eclipse.jetty", "jetty"),
           ("org.apache.kafka", "kafka-clients"), ("kafka.", "kafka-broker"), ("org.apache.zookeeper", "zookeeper"), ("com.sap.conn.jco", "sapjco"),
           ("oracle.jdbc", "ojdbc"), ("org.postgresql", "pgjdbc"), ("com.microsoft.sqlserver", "mssql-jdbc"), ("com.zaxxer.hikari", "jdbc"),
           ("org.keycloak", "keycloak"), ("org.apache.cxf", "cxf"), ("uvicorn", "uvicorn"), ("gunicorn", "gunicorn"), ("fastapi", "fastapi"),
           ("django", "django"), ("flask", "flask"), ("werkzeug", "flask"), ("celery", "celery"), ("microsoft.aspnetcore", "aspnetcore"),
           ("microsoft.data.sqlclient", "sqlclient"), ("system.data.sqlclient", "sqlclient"))
LOG_FAMILY = {"iis": "iis", "nginx": "nginx", "apache": "apache-httpd", "postgres": "postgres", "mysql": "mysql", "sqlserver": "sqlserver",
              "oracle": "oracle-db", "oracle-ebs": "oracle-ebs", "sap": "sap-abap", "redis": "redis", "kafka": "kafka-broker", "zookeeper": "zookeeper",
              "elasticsearch": "elasticsearch", "haproxy": "haproxy", "postfix": "postfix", "sshd": "openssh", "java": "java", "python": "python",
              "dotnet": "dotnet", "golang": "go", "dynamics-ax": "dynamics", "dynamics-365": "dynamics", "dynamics-nav": "dynamics"}
LOG_PARSER = {"w3c": "iis", "sap": "sap-abap"}
LOG_FILES = (("catalina", "tomcat"), ("localhost_access", "tomcat"), ("systemout", "websphere"), ("dev_w", "sap-abap"), ("dev_disp", "sap-abap"),
             ("dev_rfc", "sap-abap"), ("defaulttrace", "sap-java"), ("alert_", "oracle-db"), ("errorlog", "sqlserver"), ("u_ex", "iis"),
             ("indexserver_", "hana"), ("uvicorn", "uvicorn"), ("gunicorn", "gunicorn"), ("rabbit@", "rabbitmq"), ("slapd", "openldap"))
_SAFE = re.compile(r"^@?[\w.\-/:+]{1,80}$")                 # an instrumentation scope name: no spaces, no e-mail-like values

# ---------------------------------------------------------------- inventory and banner text
_VER = r"[\s/_-]*v?(\d+(?:\.\d+)*[a-z]?\d*(?:\s*(?:sps|sp|ehp|r)\s*\d+)?)"
INV_RULES = [(re.compile(p, re.I), k) for p, k in (
    (r"oracle\s*e-?business\s*suite|oracle\s+ebs\b|\bebs\b", "oracle-ebs"),
    (r"\bs/?4\s?hana\b|\bsap\s+(?:ecc|erp|bw|crm|srm|pi|po|scm|ewm|gts|solution manager|solman)\b", "sap-app"),
    (r"\bnetweaver\b|\bsap\s+(?:application|app|abap)\b", "sap-abap"), (r"\bhana\b", "hana"), (r"sql\s*server|\bmssql\b", "sqlserver"),
    (r"\boracle\b(?!\s*(?:linux|e-?business|ebs|weblogic|http|fusion))(?:\s*(?:database|db|rdbms))?", "oracle-db"), (r"postgre(?:sql|s)", "postgres"),
    (r"\bmariadb\b", "mariadb"), (r"\bmysql\b", "mysql"), (r"\bmongo(?:db)?\b", "mongodb"), (r"\bredis\b", "redis"), (r"\bmemcached?\b", "memcached"),
    (r"elasticsearch|opensearch", "elasticsearch"), (r"\bkafka\b", "kafka-broker"), (r"rabbitmq", "rabbitmq"), (r"\bibm\s*mq\b|websphere\s*mq", "ibm-mq"),
    (r"activemq", "activemq"), (r"zookeeper", "zookeeper"), (r"active\s*directory|domain\s*controller|\bad\s*ds\b", "ad"), (r"openldap", "openldap"),
    (r"keycloak", "keycloak"), (r"weblogic", "weblogic"), (r"tomcat", "tomcat"), (r"jboss|wildfly", "jboss"), (r"websphere(?!\s*mq)", "websphere"),
    (r"\biis\b", "iis"), (r"\bnginx\b", "nginx"), (r"apache\s*(?:httpd|http server|web server)", "apache-httpd"), (r"haproxy", "haproxy"),
    (r"openssh|\bsftp\b", "openssh"), (r"\bexchange\b", "exchange"), (r"postfix", "postfix"), (r"\bsamba\b", "samba"),
    (r"dynamics\s*(?:ax|365|nav)|business central", "dynamics"), (r"fastapi", "fastapi"), (r"spring\s*boot", "spring-boot"), (r"\bdjango\b", "django"),
    (r"asp\.net", "aspnet"), (r"node\.?js", "node"))]
BANNERS = [(re.compile(p, re.I), k) for p, k in (
    (r"openssh[_ ]([\d.]+p?\d*)", "openssh"), (r"microsoft-iis/([\d.]+)", "iis"), (r"apache-coyote/([\d.]+)", "tomcat"), (r"nginx/?([\d.]+)?", "nginx"),
    (r"apache/([\d.]+)", "apache-httpd"), (r"\buvicorn\b", "uvicorn"), (r"gunicorn/?([\d.]+)?", "gunicorn"), (r"kestrel", "kestrel"), (r"weblogic\s*([\d.]+)?", "weblogic"),
    (r"jetty\(?([\d.]+)?", "jetty"), (r"postfix", "postfix"), (r"microsoft esmtp", "exchange"), (r"([\d.]+)-mariadb", "mariadb"), (r"redis", "redis"),
    (r"mysql_native_password|^\W*([5-9]\.\d+\.\d+)", "mysql"))]
OS_BANNER = re.compile(r"openssh[_ ][\d.]+p?\d*\s+(ubuntu|debian|raspbian|freebsd)", re.I)


def compatible(key: str, pk: Optional[str]) -> bool:
    """Does technology `key` serve protocol `pk`? (no protocol, or a technology without a protocol list: yes)"""
    if pk is None:
        return True
    pr = TECH[key]["proto"]
    return not pr or pk in pr or ("http" in pr and pk in HTTPISH)


def proc_key(proc: str) -> Optional[str]:
    p = (proc or "").lower().split(":", 1)[0].strip()       # process titles: "gunicorn: worker [app]", "nginx: master process"
    if p in PROC:
        return PROC[p]
    if p.startswith("python"):
        return "python"
    if p.startswith(("ora_", "oracle")):
        return "oracle-db"
    if p.startswith("php-fpm"):
        return "php-fpm"
    for pre, k in (("dw.sap", "sap-abap"), ("gw.sap", "sap-gateway"), ("ms.sap", "sap-ms-server")):   # dw.sapPRD_DVEBMGS00 ...
        if p.startswith(pre):
            return k
    return None


def _arrow(what: str, key: str) -> str:
    """"fastapi → FastAPI", but just "nginx" when the token already is the name."""
    n = TECH[key]["name"]
    return n if _norm(what) == _norm(n) else f"{what} → {n}"


def _drop_superseded(keys: list) -> list:
    gone = set().union(*[SUPERSEDES.get(k, set()) for k in keys]) if keys else set()
    return [k for k in keys if k not in gone]


def _fingerprint(cl, tg: str, where: str) -> None:
    """One agent fingerprint tag as a claim: direct proof, except a library that is as often a client (aiohttp, tornado)."""
    if tg in AMBIGUOUS_TAGS:
        n = TECH[tg]["name"]
        cl.add(tg, _ev("agent", f"Ajan parmak izi ({where}): {n} kütüphanesi yüklü (HTTP istemcisi de olabilir; tek başına kanıt değil)",
                       f"Agent fingerprint ({where}): the {n} library is loaded (may be an HTTP client; not proof on its own)", 1))
    else:
        cl.add(tg, _ev("agent", f"Ajan parmak izi ({where}): {_arrow(tg, tg)}", f"Agent fingerprint ({where}): {_arrow(tg, tg)}"), direct=True)


def _ev(source: str, tr: str, en: str, weight: Optional[int] = None) -> dict:
    return {"source": source, "tr": tr, "en": en, "weight": W.get(source, 1) if weight is None else weight}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _ver_after(text: str, end: int) -> Optional[str]:
    m = re.match(_VER, text[end:end + 28], re.I)
    return m.group(1) if m else None


def _scan(text: str) -> list:
    """(key, version, matched text) for every product named in a free-text inventory field."""
    out, seen = [], set()
    for rx, k in INV_RULES:
        m = rx.search(text or "")
        if m and k not in seen:
            seen.add(k)
            out.append((k, _ver_after(text, m.end()), m.group(0).strip()))
    return out


# ---------------------------------------------------------------- claims: candidate technologies with the evidence behind each
class _Claims:
    def __init__(self):
        self.c: dict[str, dict] = {}

    def add(self, key: str, ev: dict, direct: bool = False, ver: Optional[str] = None, name: Optional[str] = None) -> None:
        if key not in TECH:
            return
        d = self.c.get(key)
        if d is None:
            d = self.c[key] = {"src": {}, "ev": [], "direct": False, "ver": None, "name": None}
        s = ev["source"]
        d["src"][s] = max(d["src"].get(s, 0), ev["weight"])
        if not any(e["source"] == s and e["en"] == ev["en"] for e in d["ev"]):
            d["ev"].append(ev)
        d["direct"] = d["direct"] or direct
        d["ver"] = d["ver"] or ver
        d["name"] = d["name"] or name

    def score(self, k: str) -> tuple:
        d = self.c[k]
        return (d["direct"], sum(d["src"].values()), len(d["src"]))

    def ranked(self, roles, pk: Optional[str] = None, rt: Optional[str] = None) -> list:
        ks = [k for k in self.c if TECH[k]["role"] in roles and compatible(k, pk) and (not rt or not TECH[k]["rt"] or TECH[k]["rt"] == rt)]
        ks = [k for k in ks if not any(k in SUPERSEDES.get(o, ()) and TECH[o]["role"] == TECH[k]["role"] for o in ks)]
        return sorted(ks, key=self.score, reverse=True)

    def best(self, roles, pk: Optional[str] = None, rt: Optional[str] = None) -> Optional[str]:
        r = self.ranked(roles, pk, rt)
        return r[0] if r else None

    def unique(self, roles, pk: Optional[str] = None, rt: Optional[str] = None) -> Optional[str]:
        """best(), but None when two candidates tie on indirect evidence only (Django and Flask loggers on one host): name neither."""
        r = self.ranked(roles, pk, rt)
        if len(r) > 1 and not self.c[r[0]]["direct"] and self.score(r[0]) == self.score(r[1]):
            return None
        return r[0] if r else None

    def conf(self, k: Optional[str]) -> str:
        if not k or k not in self.c:
            return "olası"
        d = self.c[k]
        return "kesin" if d["direct"] else ("yüksek" if len(d["src"]) >= 2 else "olası")

    def name(self, k: Optional[str], with_ver: bool = True) -> Optional[str]:
        if not k:
            return None
        d = self.c.get(k, {})
        n = d.get("name") or TECH[k]["name"]
        v = d.get("ver")
        return f"{n} {v}" if with_ver and v and v not in n else n


def _worst(*cs: str) -> str:
    return min(cs, key=CONF.index) if cs else "olası"


# ---------------------------------------------------------------- the engine
class TechStack:
    TTL = 60.0
    SEEN_DAYS = 14
    MIN_WINDOW_MIN = 1440            # trace / log evidence counts when seen within the map's window, but at least the last day
    BANNER_DAYS = 30                 # an older scan banner is still shown, but is no longer direct proof

    def __init__(self, kb, live=None):
        self.kb, self.live = kb, live
        kb._exec("""CREATE TABLE IF NOT EXISTS integ_tech (host TEXT NOT NULL, target TEXT NOT NULL, port INTEGER NOT NULL, owner TEXT NOT NULL DEFAULT '',
            team_id INTEGER, protocol TEXT DEFAULT '', client TEXT DEFAULT '', server TEXT DEFAULT '', note TEXT DEFAULT '', updated_at TEXT DEFAULT '',
            updated_by TEXT DEFAULT '', PRIMARY KEY (host, target, port, owner))""")
        kb._exec("CREATE INDEX IF NOT EXISTS integ_tech_target ON integ_tech(target)")
        kb._exec("""CREATE TABLE IF NOT EXISTS tech_seen (host TEXT NOT NULL, source TEXT NOT NULL, k TEXT NOT NULL, v TEXT NOT NULL, n INTEGER DEFAULT 0,
            last_seen TEXT, PRIMARY KEY (host, source, k, v))""")
        from .demo import ensure_column
        ensure_column(kb, "tech_seen")                       # 1 = evidence only simulator input gave (demo.py purges these)
        self._plock = threading.Lock()
        self._pending: dict[tuple, list] = {}
        self._flushed_at = time.time()
        self._pruned_at = 0.0
        self._ix: Optional[dict] = None
        self._ix_at = 0.0
        self._ix_lock = threading.Lock()
        self._names: dict = {}
        self._svc: dict = {}

    # ------------------------------------------------------------ ingest: what traces and logs say about each host
    def observe(self, events) -> None:
        """Live events (Services._on_ingest): canonical technology tokens per host. Cheap: a few dict lookups per event, the
        rest per unique (host, value) of the batch."""
        from .demo import is_demo
        uniq: set = set()
        demo_hosts: set = set()
        for o in events:
            h = o.host
            if not h:
                continue
            if is_demo(o):
                demo_hosts.add(h)
            a = o.attributes
            if a:
                for k in TRACE_ATTRS:
                    v = a.get(k)
                    if v:
                        uniq.add((h, "t", k, v if isinstance(v, str) else str(v)))
                for k in LOGGER_ATTRS:
                    v = a.get(k)
                    if v and isinstance(v, str):
                        uniq.add((h, "l", "logger", v))
                        break
            for v in (o.service, o.origin):                  # parsers often take the logger name as the service / component
                if v and "." in v:
                    uniq.add((h, "l", "logger", v))
            uniq.add((h, "s", o.source or "", (o.parser or "", (a or {}).get("agent") or "")))
        if not uniq:
            return
        det = getattr(self.live, "_detect", None) or {}
        out = []
        for h, kind, k, v in uniq:
            if kind == "t":
                c = canon_trace(k, v)
                if c:
                    out.append((h, "trace", c[0], c[1]))
            elif kind == "l":
                lv = v.lower()
                pre = next((p for p, _ in LOGGERS if lv.startswith(p)), None)
                if pre:
                    out.append((h, "log", "logger", pre))
            else:
                parser, agent = v
                fn = k.lower()
                mk = next((m for m, _ in LOG_FILES if m in fn), None)
                if mk:
                    out.append((h, "log", "file", mk))
                if parser in LOG_PARSER:
                    out.append((h, "log", "parser", parser))
                fam = (det.get((agent, k)) or {}).get("family") if agent else None
                if fam in LOG_FAMILY:
                    out.append((h, "log", "family", fam))
        self._note([(*r, 1) for r in out if r[0] in demo_hosts] + [r for r in out if r[0] not in demo_hosts])

    def observe_spans(self, spans) -> None:
        """TraceStore.add_spans hook: the services each host runs (for the integration map) and SOAP-looking operations."""
        out = set()
        for sp in spans:
            h = sp.get("host")
            if not h or sp.get("kind") in ("user", "mobile", "log"):
                continue
            s = sp.get("service")
            dm = (1,) if sp.get("demo") else ()
            if s:
                out.add((h, "trace", "service", str(s)[:120], *dm))
            op = (sp.get("operation") or "").lower()
            if sp.get("kind") == "server" and (".asmx" in op or ".svc" in op or "wsdl" in op or "/soap" in op):
                out.add((h, "trace", "soap", "1", *dm))
        self._note(out)

    def _note(self, rows) -> None:
        if not rows:
            return
        now = datetime.now(UTC).isoformat()
        with self._plock:
            for r in rows:
                key = (str(r[0])[:120], r[1], r[2], str(r[3])[:120])
                demo = 1 if len(r) > 4 and r[4] else 0
                p = self._pending.get(key)
                if p is None:
                    if len(self._pending) > 20000:
                        continue
                    self._pending[key] = [1, now, demo]
                else:
                    p[0] += 1; p[1] = now; p[2] = p[2] and demo     # real evidence for the key: the row is real
            due = time.time() - self._flushed_at > 30
        if due:
            self.flush()

    def flush(self) -> None:
        with self._plock:
            items, self._pending, self._flushed_at = self._pending, {}, time.time()
        if items:
            try:
                from .demo import keep_real
                self.kb.executemany("INSERT INTO tech_seen (host, source, k, v, n, last_seen, demo) VALUES (?,?,?,?,?,?,?)" + self.kb.upsert_suffix(
                    "host, source, k, v", "n=CASE WHEN tech_seen.n > 1000000000 THEN tech_seen.n ELSE tech_seen.n + excluded.n END, last_seen=excluded.last_seen, "
                    + keep_real("tech_seen")), [(h, s, k, v, n, ts, int(dm)) for (h, s, k, v), (n, ts, *rest) in items.items() for dm in [rest[0] if rest else 0]])
            except Exception:  # noqa: BLE001 - evidence is best effort
                pass
        if time.time() - self._pruned_at > 3600:
            self._pruned_at = time.time()
            try:
                self.kb._exec("DELETE FROM tech_seen WHERE last_seen < ?", ((datetime.now(UTC) - timedelta(days=self.SEEN_DAYS)).isoformat(),))
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------ per-host evidence (rebuilt at most once a minute)
    def invalidate(self) -> None:
        self._ix_at = 0.0
        self._names = {}

    def _index(self) -> dict:
        if self._ix is not None and time.time() - self._ix_at < self.TTL:
            return self._ix
        with self._ix_lock:
            if self._ix is not None and time.time() - self._ix_at < self.TTL:
                return self._ix
            self.flush()
            ix: dict[str, dict] = {}

            def slot(h: str) -> dict:
                return ix.setdefault(str(h).lower(), {"items": [], "route": "", "soap": "", "services": set(), "os": None, "banners": {}})
            try:
                for r in self.kb._exec("SELECT * FROM inventory"):
                    if r.get("hostname"):
                        _inventory(slot(r["hostname"]), r)
            except Exception:  # noqa: BLE001 - no inventory table (tests, fresh databases)
                pass
            try:
                old = (datetime.now(UTC) - timedelta(days=self.BANNER_DAYS)).isoformat()
                for r in self.kb._exec("SELECT hostname, open_ports, scanned_at FROM scan_results"):
                    ports = json.loads(r.get("open_ports") or "[]")
                    if ports and r.get("hostname"):
                        _banners(slot(r["hostname"]), r["hostname"], ports, r.get("scanned_at") or "", old)
            except Exception:  # noqa: BLE001
                pass
            try:
                cut = (datetime.now(UTC) - timedelta(days=self.SEEN_DAYS)).isoformat()
                for r in self.kb._exec("SELECT host, source, k, v, last_seen FROM tech_seen WHERE last_seen >= ?", (cut,)):
                    _seen(slot(r["host"]), r["host"], r["source"], r["k"], r["v"], r.get("last_seen") or "")
            except Exception:  # noqa: BLE001
                pass
            self._ix, self._ix_at = ix, time.time()
            self._names = {}
            return ix

    def since(self, minutes: Optional[int]) -> Optional[str]:
        """Oldest trace / log evidence that counts for a window of `minutes` (None: everything kept, SEEN_DAYS)."""
        return (datetime.now(UTC) - timedelta(minutes=max(int(minutes), self.MIN_WINDOW_MIN))).isoformat() if minutes else None

    def host_ev(self, host: str, since: Optional[str] = None) -> dict:
        e = self._index().get(str(host or "").lower()) or {"items": [], "route": "", "soap": "", "services": set(), "os": None, "banners": {}}
        if not since:
            return e
        return {**e, "items": [it for it in e["items"] if not it.get("at") or it["at"] >= since],       # trace / log evidence older than the window
                "route": e["route"] if (e["route"] or "") >= since else "", "soap": e["soap"] if (e["soap"] or "") >= since else ""}

    def services(self, host: str, minutes: Optional[int] = 1440) -> list:
        """Services seen on the host in the traces (recorded at ingest). Before anything was recorded (a database from an older
        version): one bounded spans query per host and minute."""
        s = self.host_ev(host)["services"]
        if s:
            return sorted(s)[:50]
        hit = self._svc.get(host)
        if hit is not None and time.time() - hit[0] < self.TTL:
            return hit[1]
        try:
            since = (datetime.now(UTC) - timedelta(minutes=minutes or 10080)).isoformat()
            rows = self.kb._exec("SELECT DISTINCT service FROM spans WHERE ts >= ? AND host = ? AND kind NOT IN ('user','mobile','log') LIMIT 50", (since, host))
            out = sorted(r["service"] for r in rows)
        except Exception:  # noqa: BLE001
            out = []
        if len(self._svc) > 2000:
            self._svc = {}
        self._svc[host] = (time.time(), out)
        return out

    # ------------------------------------------------------------ one integration
    def _client(self, host: str, procs: dict, pk: Optional[str], vis: bool, since: Optional[str] = None) -> tuple:
        cl = _Claims()
        for proc, tags in (procs or {}).items():
            k = proc_key(proc)
            if k:
                cl.add(k, _ev("process", f"{host} üzerindeki istemci süreci: {_arrow(proc, k)}", f"Client process on {host}: {_arrow(proc, k)}"))
            for tg in sorted(tags or ()) if vis else ():      # another team's host: its process names only (shown already), not its fingerprints
                if tg in TECH:
                    _fingerprint(cl, tg, f"{host} · {proc}")
        if vis:
            for it in self.host_ev(host, since)["items"]:
                if TECH[it["key"]]["role"] in ("lib", "framework", "runtime", "worker"):
                    cl.add(it["key"], it["ev"], it["direct"], it["ver"])
        tags = set().union(*[set(t or ()) for t in (procs or {}).values()]) if (procs and vis) else set()
        return cl, tags

    def _server(self, ctx: dict, target: str, port: int, pk: Optional[str], visible, client_items: list, depth: int = 0, seen=frozenset()) -> dict:
        cl = _Claims()
        vis = visible is None or visible(target)
        lst = (ctx.get("listen") or {}).get((target, str(port)), {}) if vis else {}
        lrt, lprod = None, None
        for proc, tags in lst.items():
            k = proc_key(proc)
            if k:
                cl.add(k, _ev("listener", f"{target} üzerinde :{port} portunu dinleyen süreç: {_arrow(proc, k)}",
                              f"Process listening on {target}:{port}: {_arrow(proc, k)}"))
            for tg in sorted(tags or ()):
                if tg in TECH and TECH[tg]["role"] != "lib":
                    _fingerprint(cl, tg, f"{target} · {proc}")
            ts = set(tags or ())
            if ("uvicorn" in ts or proc == "uvicorn") and "pydantic_core" in ts and "fastapi" not in ts:
                cl.add("fastapi", _ev("agent", "uvicorn ve pydantic_core birlikte: FastAPI olası (çatı doğrudan görülmedi)",
                                      "uvicorn with pydantic_core: FastAPI likely (the framework itself was not seen)", 1))
        if lst:
            lrt = cl.best(("runtime",))
            lprod = cl.best(("server", "proxy", "db", "broker", "dir", "file", "mail"))
            if lrt is None and lprod and TECH[lprod]["rt"]:
                lrt = TECH[lprod]["rt"]
        pk_eff = pk
        if pk not in PROTO and lprod:                        # unknown port: the listener says what is spoken on it
            pr = TECH[lprod]["proto"]
            pk_eff = "http" if "http" in pr else (sorted(pr)[0] if pr else pk)
        if pk_eff in IMPLIED:
            k = IMPLIED[pk_eff]
            cl.add(k, _ev("port", f"Port {port}: {PROTO[pk_eff]} protokolünü yalnızca {TECH[k]['name']} konuşur (tek başına: olası)",
                          f"Port {port}: {PROTO[pk_eff]} is spoken by {TECH[k]['name']} (on its own: likely)"))
        hev = self.host_ev(target, ctx.get("since")) if vis else {"items": [], "route": "", "soap": "", "banners": {}}
        proxy_front = lprod is not None and TECH[lprod]["role"] == "proxy"
        for it in hev["items"]:
            k = it["key"]
            t = TECH[k]
            if t["role"] == "lib" or not compatible(k, pk_eff) or (pk_eff not in PROTO and not t["proto"]):
                continue
            if t["role"] == "runtime" and pk_eff not in HTTPISH:
                continue
            if lst and (proxy_front and t["role"] in ("framework", "server", "runtime")):
                continue                                     # what runs behind the proxy is followed through its upstream flows
            if lrt and t["rt"] and t["rt"] != lrt:
                continue                                     # the listener is another runtime: this belongs to another service of the host
            if lprod and t["role"] in ("server", "proxy", "db", "broker", "dir", "file", "mail") and k != lprod and TECH[lprod]["role"] == t["role"]:
                continue
            if it.get("port") not in (None, port):
                continue
            cl.add(k, it["ev"], it["direct"], it["ver"], it.get("name"))
        for it in client_items:                               # db.system / messaging.system seen on either side of the call
            if compatible(it["key"], pk_eff) and TECH[it["key"]]["role"] in ("db", "broker"):
                cl.add(it["key"], it["ev"], it["direct"], it["ver"])
        prod = cl.best(("server", "proxy", "db", "broker", "dir", "file", "mail"), pk_eff if pk_eff in PROTO else None)
        rt = cl.best(("runtime",)) if (pk_eff in HTTPISH or lst) else None
        if rt is None and prod and TECH[prod]["rt"] and pk_eff in HTTPISH:
            rt = TECH[prod]["rt"]
        fw = cl.unique(("framework",), pk_eff if pk_eff in PROTO else None, rt) if pk_eff in HTTPISH else None
        if fw and rt is None:
            rt = TECH[fw]["rt"] or None
        if prod and fw and TECH[prod]["rt"] and TECH[fw]["rt"] and TECH[prod]["rt"] != TECH[fw]["rt"]:
            fw = None
        app = cl.best(("app",))
        chain: list = []
        if prod and TECH[prod]["role"] == "proxy" and depth < 2:
            ups: dict = {}
            for proc, ep, n, _tg in (ctx.get("proxy") or {}).get(target, []):
                if proc_key(proc) != prod:
                    continue
                ip, _, p = str(ep).rpartition(":")
                if not p.isdigit():
                    continue
                name = (ctx.get("names") or {}).get(ip) or ip
                ups[(name, int(p))] = ups.get((name, int(p)), 0) + n
            for (uh, up), _n in sorted(ups.items(), key=lambda x: -x[1])[:2]:
                if (uh, up) in seen or uh == target:
                    continue
                sub = self._server(ctx, uh, up, port_name(up), visible, [], depth + 1, seen | {(target, port)})
                if sub["hop"]:
                    chain.append(sub)
        hop = _hop(cl.name(prod), cl.name(fw), bool(chain) or depth > 0)
        conf = cl.conf(fw or prod)
        if chain:
            conf = _worst(cl.conf(prod), *[c["conf"] for c in chain])
        # direct proof of this very port (a scanner banner) that names something else than what was chosen: shown, and the claim is
        # no longer "kesin" (a stale scan, or a product passed through a proxy that is not visible)
        keys = _chain_keys(prod, fw, rt, app, chain)
        conflicts = []
        for it in hev["items"]:
            k = it["key"]
            if it.get("port") == port and it["direct"] and k not in keys and compatible(k, pk_eff) and \
                    TECH[k]["role"] in ("server", "proxy", "framework", "db", "broker", "dir", "file", "mail"):
                chosen = hop or "?"
                conflicts.append({**_ev(it["ev"]["source"], f"Çelişen kanıt: {it['ev']['tr']} (seçilen: {chosen})",
                                        f"Conflicting evidence: {it['ev']['en']} (chosen: {chosen})", it["ev"]["weight"]), "conflict": True})
        if conflicts and conf == "kesin":
            conf = "yüksek"
        ver = cl.c[fw or prod]["ver"] if (fw or prod) else None
        return {"claims": cl, "prod": prod, "fw": fw, "rt": rt, "app": app, "hop": hop, "chain": chain, "conf": conf, "pk": pk_eff, "listened": bool(lst),
                "conflicts": conflicts + [x for c in chain for x in c["conflicts"]],
                "host": target, "port": port,
                "route": bool(hev.get("route")), "soap": bool(hev.get("soap")), "tags": set().union(*[set(t or ()) for t in lst.values()]) if lst else set(),
                "ver": ver, "tls": bool((hev.get("banners") or {}).get(port, {}).get("tls"))}

    def integration(self, ctx: dict, client: str, procs: dict, target: str, port: int, visible=None, manual: Optional[dict] = None) -> dict:
        pk = port_name(port) if port else "tcp"
        cvis = visible is None or visible(client)
        since = ctx.get("since")
        ccl, ctags = self._client(client, procs, pk, cvis, since)
        citems = [it for it in self.host_ev(client, since)["items"] if it.get("proto")] if cvis else []
        if visible is None or visible(target):
            citems += [it for it in self.host_ev(target, since)["items"] if it.get("proto")]
        srv = self._server(ctx, target, port, pk, visible, citems)
        pk_eff = srv["pk"]
        disp = PROTO.get(pk_eff) or (f"TCP {port}" if port else "TCP")
        tls = pk_eff in TLS_PROTOS or port in TLS_PORTS or srv["tls"]
        if pk_eff == "http" and tls:
            disp = "HTTPS"
        ev = [_ev("port", f"Port {port}: {disp}" + ("; port tek başına çatıyı (framework) göstermez" if pk_eff in HTTPISH else ""),
                  f"Port {port}: {disp}" + ("; a port alone never names the framework" if pk_eff in HTTPISH else ""))]
        # client side
        libs = _drop_superseded([k for k in sorted(ccl.ranked(("lib",), pk_eff if pk_eff in PROTO else None), key=lambda k: (ccl.score(k), bool(TECH[k]["rt"])),
                                                     reverse=True) if k != "sftp"])[:3]
        cfw = ccl.unique(("framework", "worker"))
        cprod = ccl.best(("server", "proxy"))
        crt = ccl.best(("runtime",)) or (TECH[cfw]["rt"] if cfw and TECH[cfw]["rt"] else None) or (TECH[cprod]["rt"] if cprod and TECH[cprod]["rt"] else None)
        cname = ccl.name(cfw or cprod or crt, False)
        client_d = {"name": cname, "runtime": TECH[crt]["name"] if crt else None, "libs": [TECH[k]["name"] for k in libs]}
        # protocol kind
        head = disp
        if pk_eff == "ssh":
            p = next((CLIENT_PROTO_PROC[x] for x in procs or {} if x in CLIENT_PROTO_PROC), None)
            head = p or ("SFTP" if ({"sftp", "jsch"} & ctags) else "SSH")
        api_kind = None
        if pk_eff in HTTPISH:
            if "grpc" in ctags or "grpc" in srv["tags"] or any(it["key"] == "grpc" for it in citems):
                api_kind = "gRPC"
            elif srv["soap"] or any(c["soap"] for c in srv["chain"]) or ((SOAP_LIBS & ctags) and (SOAP_LIBS & srv["tags"])):
                api_kind = "SOAP"                            # SOAP operations (.asmx, .svc, ?wsdl), or a SOAP stack on both ends of this port
            elif (srv["fw"] and TECH[srv["fw"]]["api"]) or srv["route"] or any(c["route"] or (c["fw"] and TECH[c["fw"]]["api"]) for c in srv["chain"]):
                api_kind = "REST API"
            elif SOAP_ONLY & srv["tags"]:                    # a SOAP-only stack (JAX-WS, Axis2, WCF) in the process serving this port
                api_kind = "SOAP"
        scl: _Claims = srv["claims"]
        # server display
        sname = srv["hop"]
        if srv["chain"]:
            sname = " → ".join([srv["hop"]] + [_chain_text(c) for c in srv["chain"]])
        app_name = scl.name(srv["app"]) if srv["app"] else None
        prod_name = scl.name(srv["prod"]) if srv["prod"] else None
        server_d = {"name": None, "product": scl.name(srv["prod"], False) if srv["prod"] else None,
                    "framework": scl.name(srv["fw"], False) if srv["fw"] else None, "runtime": TECH[srv["rt"]]["name"] if srv["rt"] else None,
                    "version": srv["ver"], "app": app_name, "chain": [_chain_dict(c) for c in srv["chain"]]}
        # label + confidence
        proto_conf_src = {"port"} | ({"listener"} if srv["listened"] else set()) | ({"trace"} if any(compatible(it["key"], pk_eff) for it in citems) else set())
        proto_conf = "kesin" if "trace" in proto_conf_src else ("yüksek" if len(proto_conf_src) >= 2 else "olası")
        if pk_eff in HTTPISH:
            server_d["name"] = sname or None
            if sname:
                label = f"{api_kind or disp} · {sname}"
                conf = srv["conf"]
            else:
                label = f"{api_kind} ({disp})" if api_kind else f"{disp} API"
                conf = proto_conf
        else:
            main_srv = app_name if (app_name and pk_eff in ("sap-dialog", "sap-rfc", "sap-ms")) else prod_name
            server_d["name"] = main_srv
            base = TECH[srv["prod"]]["name"] if srv["prod"] else ""
            redundant = bool(base) and (_norm(head) in _norm(base) or _norm(base) in _norm(head)) and not (main_srv and main_srv != prod_name)
            lib_txt = " / ".join(TECH[k]["name"] for k in libs[:2]) if libs else None
            if main_srv and not redundant:
                label = f"{head} · {main_srv}" + (f" ({lib_txt})" if lib_txt else "")
                conf = scl.conf(srv["app"] if main_srv == app_name and srv["app"] else srv["prod"]) if srv["prod"] or srv["app"] else proto_conf
                if main_srv == app_name and srv["prod"]:
                    conf = max(conf, scl.conf(srv["prod"]), key=CONF.index)
                if srv["conflicts"] and conf == "kesin":
                    conf = "yüksek"
            elif lib_txt:
                label = f"{head} · {lib_txt}"
                conf = ccl.conf(libs[0])
            else:
                label = head
                conf = scl.conf(srv["prod"]) if srv["prod"] else proto_conf
                if srv["conflicts"] and conf == "kesin":
                    conf = "yüksek"
        # evidence, most telling first: the server side (or the client library when the label names it), the client, the port
        sv: list = []
        for k in (srv["fw"], srv["prod"], srv["app"], srv["rt"]):
            if k in scl.c:
                sv += scl.c[k]["ev"]
        for c in srv["chain"]:
            for k in (c["fw"], c["prod"]):
                if k in c["claims"].c:
                    sv += c["claims"].c[k]["ev"]
        if srv["route"] and api_kind == "REST API":
            sv.append(_ev("trace", f"İz kayıtları ({target}): http.route görüldü → REST uç noktaları", f"Traces ({target}): http.route seen → REST endpoints"))
        lv = [e for c in libs if c in ccl.c for e in ccl.c[c]["ev"]]
        cv = [e for c in (cfw, cprod, crt) if c in ccl.c for e in ccl.c[c]["ev"]]
        lib_main = pk_eff not in HTTPISH and bool(libs) and label.endswith(" / ".join(TECH[k]["name"] for k in libs[:2]))
        xv = srv["conflicts"]
        ev = _dedupe(*((lv, sv, xv, cv, ev) if lib_main else (sv, xv, lv, cv, ev)))
        out = {"label": label, "protocol": {"name": head if pk_eff == "ssh" else disp, "family": category(pk_eff) if pk_eff in PROTO else "other", "port": port,
                                            "tls": bool(tls), "key": pk_eff},
               "client": client_d, "server": server_d, "confidence": conf, "evidence": ev, "manual": False}
        if manual:
            out = _apply_manual(out, manual)
        return out

    # ------------------------------------------------------------ the host map, the host list
    def annotate(self, m: dict, ctx: dict, visible=None, manual_ok=None) -> dict:
        """FlowStore.host_map result -> every outbound / inbound / listening item gets "tech", the host gets "stack" and "services"."""
        host = m["host"]
        ctx["since"] = self.since(ctx.get("minutes"))
        man = self.overrides(host, manual_ok)
        for o in m["outbound"]:
            o["tech"] = self.integration(ctx, host, o.get("_procs") or {}, o["target"], int(o["port"] or 0), visible, man.get((host, o["target"], int(o["port"] or 0))))
        for i in m["inbound"]:
            i["tech"] = self.integration(ctx, i["source"], i.get("_procs") or {}, host, int(i["port"] or 0), visible, man.get((i["source"], host, int(i["port"] or 0))))
        for li in m["listens"]:
            p = int(li["port"]) if str(li["port"]).isdigit() else 0
            li["tech"] = self.integration(ctx, "", {}, host, p, visible)
        m["stack"] = self.host_stack(host, ctx.get("procs") or {}, ctx["since"]) if (visible is None or visible(host)) else []
        return m

    def host_stack(self, host: str, procs: dict, since: Optional[str] = None) -> list:
        """What runs on one host, by layer: runtime | web | app | db | messaging | identity | file | mail | os."""
        cl = _Claims()
        for proc, tags in (procs or {}).items():
            k = proc_key(proc)
            if k and TECH[k]["role"] != "lib":
                cl.add(k, _ev("process", f"{host} üzerindeki süreç: {_arrow(proc, k)}", f"Process on {host}: {_arrow(proc, k)}"))
            for tg in sorted(tags or ()):
                if tg in TECH and TECH[tg]["role"] != "lib":
                    _fingerprint(cl, tg, proc)
            ts = set(tags or ())
            if ("uvicorn" in ts or proc == "uvicorn") and "pydantic_core" in ts and "fastapi" not in ts:
                cl.add("fastapi", _ev("agent", "uvicorn ve pydantic_core birlikte: FastAPI olası", "uvicorn with pydantic_core: FastAPI likely", 1))
        he = self.host_ev(host, since)
        for it in he["items"]:
            if TECH[it["key"]]["role"] != "lib" and not it.get("proto"):      # db.system / messaging.system describe a peer, not this host
                cl.add(it["key"], it["ev"], it["direct"], it["ver"], it.get("name"))
        out = []
        keep = set(_drop_superseded(list(cl.c)))
        for k in sorted(cl.c, key=cl.score, reverse=True):
            t = TECH[k]
            if t["layer"] == "lib" or k not in keep:
                continue
            d = cl.c[k]
            out.append({"layer": t["layer"], "name": d["name"] or t["name"], "version": d["ver"], "confidence": cl.conf(k), "evidence": _dedupe(d["ev"]), "key": k})
        if he["os"]:
            out.append({"layer": "os", **he["os"]})
        rank = {la: i for i, la in enumerate(LAYERS)}
        out.sort(key=lambda x: (rank.get(x["layer"], 99), ROLE_RANK.get(TECH.get(x.get("key") or "", {}).get("role"), 9), -CONF.index(x["confidence"])))
        per: dict = {}
        res = []
        for x in out:
            per[x["layer"]] = per.get(x["layer"], 0) + 1
            if per[x["layer"]] <= 3:
                res.append(x)
        return res

    def host_names(self, procs_by_host: dict, hosts: list, key=None) -> dict:
        """host -> its top three technology names (the host list and its search), each cached a minute per window."""
        self._index()
        now = time.time()
        cache = self._names.setdefault(key, {})
        out = {}
        for h in hosts:
            hit = cache.get(h)
            if hit is None or now - hit[0] >= self.TTL:
                hit = cache[h] = (now, self._top(h, procs_by_host.get(h) or {}, self.since(key[0] if isinstance(key, tuple) else None)))
            out[h] = hit[1]
        if len(self._names) > 32:
            self._names = {}
        return out

    def _top(self, host: str, procs: dict, since: Optional[str] = None) -> list:
        st = self.host_stack(host, procs, since)
        names: list = []
        for x in st:
            if x["layer"] == "runtime" and any(TECH.get(y.get("key") or "", {}).get("rt") == x.get("key") for y in st if y["layer"] != "runtime"):
                continue                                     # "FastAPI, uvicorn" says Python already
            if x["name"] not in names:
                names.append(x["name"])
            if len(names) == 3:
                break
        return names

    # ------------------------------------------------------------ manual overrides (team-owned)
    def overrides(self, host: str, manual_ok=None) -> dict:
        """(client, target, port) -> the override this reader sees: their own team's row first, then the installation-wide one. An
        installation-wide reader (manual_ok None) sees the installation-wide row first, then the newest team row."""
        try:
            rows = self.kb._exec("SELECT * FROM integ_tech WHERE host=? OR target=?", (host, host))
        except Exception:  # noqa: BLE001
            return {}
        best: dict = {}
        for r in rows:
            t = r.get("team_id")
            if manual_ok is not None and not manual_ok(t):
                continue
            k = (r["host"], r["target"], int(r["port"]))
            rank = ((t is None) if manual_ok is None else (t is not None), r.get("updated_at") or "")
            if k not in best or rank > best[k][0]:
                best[k] = (rank, r)
        return {k: v[1] for k, v in best.items()}

    def set_override(self, host: str, target: str, port: int, team_id: Optional[int], by: str, protocol: str = "", client: str = "", server: str = "",
                     note: str = "") -> dict:
        now = datetime.now(UTC).isoformat()
        owner = "" if team_id is None else str(int(team_id))
        self.kb._exec("INSERT INTO integ_tech (host, target, port, owner, team_id, protocol, client, server, note, updated_at, updated_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
                      + self.kb.upsert_suffix("host, target, port, owner", "protocol=excluded.protocol, client=excluded.client, server=excluded.server, "
                                              "note=excluded.note, updated_at=excluded.updated_at, updated_by=excluded.updated_by"),
                      (host, target, int(port), owner, team_id, protocol, client, server, note, now, by))
        return {"host": host, "target": target, "port": int(port), "team_id": team_id, "protocol": protocol, "client": client, "server": server, "note": note,
                "updated_at": now, "updated_by": by}

    def delete_override(self, host: str, target: str, port: int, team_id: Optional[int]) -> int:
        owner = "" if team_id is None else str(int(team_id))
        n = self.kb._exec("SELECT COUNT(*) AS n FROM integ_tech WHERE host=? AND target=? AND port=? AND owner=?", (host, target, int(port), owner))[0]["n"]
        self.kb._exec("DELETE FROM integ_tech WHERE host=? AND target=? AND port=? AND owner=?", (host, target, int(port), owner))
        return int(n)

    def attach_teams(self, teams) -> None:
        """Overrides count as team-owned records when a team is deleted (and move with a transfer)."""
        def count(tid):
            return int(self.kb._exec("SELECT COUNT(*) AS n FROM integ_tech WHERE team_id=?", (tid,))[0]["n"])

        def move(tid, to):
            owner = "" if to is None else str(int(to))
            for r in self.kb._exec("SELECT host, target, port FROM integ_tech WHERE team_id=?", (tid,)):
                k = (r["host"], r["target"], r["port"])
                if self.kb._exec("SELECT 1 FROM integ_tech WHERE host=? AND target=? AND port=? AND owner=?", (*k, owner)):
                    self.kb._exec("DELETE FROM integ_tech WHERE host=? AND target=? AND port=? AND team_id=?", (*k, tid))   # the receiver keeps its own
                else:
                    self.kb._exec("UPDATE integ_tech SET team_id=?, owner=? WHERE host=? AND target=? AND port=? AND team_id=?", (to, owner, *k, tid))
        teams.owned_hooks["integ_tech"] = (count, move)


# ---------------------------------------------------------------- helpers
def canon_trace(k: str, v: str) -> Optional[tuple]:
    """A span / event attribute -> (k, canonical token) when it names a known technology, else None (nothing else is stored)."""
    lv = v.strip().lower()
    if k in ("db.system", "db.system.name"):
        return ("db.system", lv) if lv in DB_SYSTEM else None
    if k == "messaging.system":
        return ("messaging.system", lv) if lv in MSG_SYSTEM else None
    if k == "rpc.system":
        return ("rpc.system", lv) if lv in RPC_SYSTEM else None
    if k in ("telemetry.sdk.language", "process.runtime.name"):
        return ("telemetry.sdk.language", LANG[lv]) if lv in LANG else None
    if k == "webengine.name":
        kk = next((key for rx, key in WEBENGINES if rx.search(lv)), None)
        return ("webengine.name", kk) if kk else None
    if k == "http.route":
        return ("http.route", "1")
    if _SAFE.match(lv) and any(rx.search(lv) for rx, _ in SCOPES):
        return ("scope", lv[:80])
    return None


def _seen(slot: dict, host: str, source: str, k: str, v: str, at: str = "") -> None:
    it = None
    at = at or "9"                                            # unknown time: always counts
    if source == "trace":
        if k == "service":
            slot["services"].add(v); return
        if k == "http.route":
            slot["route"] = max(slot["route"] or "", at); return
        if k == "soap":
            slot["soap"] = max(slot["soap"] or "", at); return
        key = (DB_SYSTEM.get(v) if k == "db.system" else MSG_SYSTEM.get(v) if k == "messaging.system" else RPC_SYSTEM.get(v) if k == "rpc.system"
               else v if k in ("telemetry.sdk.language", "webengine.name") else next((key for rx, key in SCOPES if rx.search(v)), None) if k == "scope" else None)
        if key in TECH:
            it = {"key": key, "direct": True, "ver": None, "proto": k in ("db.system", "messaging.system", "rpc.system"), "at": at,
                  "ev": _ev("trace", f"İz kayıtları ({host}): {k}={v} → {TECH[key]['name']}", f"Traces ({host}): {k}={v} → {TECH[key]['name']}")}
    elif source == "log":
        key = (LOG_FAMILY.get(v) if k == "family" else LOG_PARSER.get(v) if k == "parser" else dict(LOG_FILES).get(v) if k == "file"
               else dict(LOGGERS).get(v) if k == "logger" else None)
        what_tr = {"family": "log ailesi", "parser": "log biçimi", "file": "dosya", "logger": "logger"}.get(k, k)
        what_en = {"family": "log family", "parser": "log format", "file": "file", "logger": "logger"}.get(k, k)
        shown = f"{v}*" if k == "file" else v
        if key in TECH:
            it = {"key": key, "direct": False, "ver": None, "proto": False, "at": at,
                  "ev": _ev("log", f"Loglar ({host}): {what_tr} {shown} → {TECH[key]['name']}", f"Logs ({host}): {what_en} {shown} → {TECH[key]['name']}")}
    if it is not None and not any(x["key"] == it["key"] and x["ev"]["en"] == it["ev"]["en"] for x in slot["items"]):
        slot["items"].append(it)


def _inventory(slot: dict, r: dict) -> None:
    h = r["hostname"]
    for f, tr in (("application", "uygulama"), ("role", "rol"), ("vendor_model", "model")):
        txt = str(r.get(f) or "").strip()
        for k, ver, matched in _scan(txt):
            name = None
            if k == "sap-app":
                mt = re.sub(r"\s+", " ", matched)
                name = "SAP " + mt[4:].upper() if mt.lower().startswith("sap ") else "SAP S/4HANA"
            slot["items"].append({"key": k, "direct": False, "ver": ver, "name": name, "proto": False,
                                  "ev": _ev("inventory", f"Envanter ({h}) {tr}: \"{txt[:60]}\" → {name or TECH[k]['name']}",
                                            f"Inventory ({h}) {f}: \"{txt[:60]}\" → {name or TECH[k]['name']}")})
    os_txt = re.sub(r"\s+", " ", str(r.get("os") or "")).strip()[:48]
    if os_txt:
        slot["os"] = {"name": os_txt, "version": None, "confidence": "olası",
                      "evidence": [_ev("inventory", f"Envanter ({h}) işletim sistemi: \"{os_txt}\"", f"Inventory ({h}) os: \"{os_txt}\"")]}


def _banners(slot: dict, host: str, ports: list, at: str = "", old: str = "") -> None:
    """Scanner banners of one host. A banner from a scan older than `old` is shown with its date but is no longer direct proof."""
    stale = bool(at and old and at < old)
    when = f" · {at[:10]}" if stale else ""
    for p in ports:
        b = str(p.get("banner") or "")
        port = p.get("port")
        if not b:
            continue
        if b.upper().startswith("TLS"):
            slot["banners"].setdefault(port, {})["tls"] = True
        for rx, k in BANNERS:
            m = rx.search(b)
            if m:
                ver = next((g for g in m.groups() if g), None) if m.groups() else None
                slot["items"].append({"key": k, "direct": not stale, "ver": ver, "port": port, "proto": False,
                                      "ev": _ev("banner", f"Tarama banner'ı ({host}:{port}{when}{', eski tarama' if stale else ''}): \"{b[:60]}\" → {TECH[k]['name']}",
                                                f"Scanner banner ({host}:{port}{when}{', old scan' if stale else ''}): \"{b[:60]}\" → {TECH[k]['name']}",
                                                1 if stale else None)})
                break
        m = OS_BANNER.search(b)
        if m and not slot["os"]:
            slot["os"] = {"name": m.group(1).capitalize(), "version": None, "confidence": "olası",
                          "evidence": [_ev("banner", f"Tarama banner'ı ({host}:{port}): \"{b[:60]}\"", f"Scanner banner ({host}:{port}): \"{b[:60]}\"")]}


def _hop(prod: Optional[str], fw: Optional[str], in_chain: bool) -> Optional[str]:
    if prod and fw:
        gen = _norm(prod.split(" ")[0]) in {_norm(TECH[k]["name"]) for k in GENERIC_SERVERS}
        return fw if (gen and not in_chain) else f"{prod} ({fw})"
    return fw or prod


def _chain_keys(prod, fw, rt, app, chain: list) -> set:
    out = {prod, fw, rt, app}
    for c in chain:
        out |= _chain_keys(c["prod"], c["fw"], c["rt"], c["app"], c["chain"])
    return out


def _chain_text(c: dict) -> str:
    return " → ".join([c["hop"]] + [_chain_text(x) for x in c["chain"]])


def _chain_dict(c: dict) -> dict:
    """One upstream hop behind a proxy, flattened: where it is and what runs there."""
    cl = c["claims"]
    return {"host": c["host"], "port": c["port"], "name": c["hop"], "product": cl.name(c["prod"], False) if c["prod"] else None,
            "framework": cl.name(c["fw"], False) if c["fw"] else None, "runtime": TECH[c["rt"]]["name"] if c["rt"] else None,
            "version": c["ver"], "confidence": c["conf"], "chain": [_chain_dict(x) for x in c["chain"]]}


def _dedupe(*groups: list) -> list:
    """Evidence without repeats, group by group (each by weight), at most 14 lines."""
    out, seen = [], set()
    for g in groups:
        for e in sorted(g, key=lambda e: -e["weight"]):
            k = (e["source"], e["en"])
            if k not in seen:
                seen.add(k); out.append(e)
    return out[:14]


def _apply_manual(out: dict, r: dict) -> dict:
    proto = (r.get("protocol") or "").strip()
    cli = (r.get("client") or "").strip()
    srv = (r.get("server") or "").strip()
    if proto:
        out["protocol"] = {**out["protocol"], "name": proto}
    if cli:
        out["client"] = {**out["client"], "name": cli}
    if srv:
        out["server"] = {**out["server"], "name": srv}
    head = proto or out["label"].split(" · ")[0]
    main = srv or out["server"].get("name")
    out["label"] = f"{head} · {main}" if main else head
    when = (r.get("updated_at") or "")[:16].replace("T", " ")
    note = (r.get("note") or "").strip()
    out["evidence"] = [_ev("manual", f"Elle girildi ({r.get('updated_by') or '?'} · {when})" + (f": {note}" if note else ""),
                           f"Set manually ({r.get('updated_by') or '?'} · {when})" + (f": {note}" if note else ""))] + out["evidence"]
    out["confidence"] = "kesin"
    out["manual"] = True
    out["note"] = note or None
    out["team_id"] = r.get("team_id")
    return out


# ---------------------------------------------------------------- demo (simulator): hosts with varied stacks
SIM_TECH_INVENTORY = [   # hostname, ip, os, application, role
    ("pay-api-01", "172.16.5.21", "Ubuntu 22.04 LTS", "Ödeme API", "API"),
    ("portal-01", "172.16.5.10", "Ubuntu 22.04 LTS", "Müşteri portalı", "Reverse proxy"),
    ("crm-web-01", "172.16.5.30", "Ubuntu 22.04 LTS", "CRM web", "Web"),
    ("erp-int-01", "172.16.3.11", "Red Hat Enterprise Linux 8.8", "ERP entegrasyon servisi", "Integration"),
    ("sap-ecc-01", "172.16.3.20", "SUSE Linux Enterprise Server 15 SP4", "SAP ECC 6.0 EHP8", "SAP application server"),
    ("hana-01", "172.16.3.30", "SUSE Linux Enterprise Server 15 SP4", "SAP HANA 2.0 SPS07", "Database"),
    ("hr-app-01", "172.16.2.20", "Oracle Linux 7.9", "Oracle E-Business Suite 12.2", "HR application"),
    ("web-iis-01", "172.16.4.10", "Windows Server 2019", "Bayi portalı", "Web"),
    ("sql-01", "172.16.4.15", "Windows Server 2019", "SQL Server 2019", "Database"),
    ("batch-01", "172.16.6.12", "Red Hat Enterprise Linux 8.8", "Gece batch işleri", "Batch"),
    ("sftp-01", "172.16.6.30", "Ubuntu 22.04 LTS", "OpenSSH 8.9p1", "SFTP"),
    ("dc-01", "172.16.7.5", "Windows Server 2019", "Active Directory", "Domain Controller"),
    ("mq-01", "172.16.8.5", "Ubuntu 22.04 LTS", "RabbitMQ 3.12", "Message broker"),
]
SIM_TECH_EXTRA_INVENTORY = {"db-01": "PostgreSQL 15", "hr-db": "Oracle Database 19c", "kafka-01": "Apache Kafka 3.6", "cache-01": "Redis 7.2"}
_PAY = "fastapi,httptools,librdkafka,oracledb,pydantic_core,python,starlette,uvicorn,uvloop"
_ERP = "cxf,java,kafka-clients,sapjco,spring-boot,spring-kafka,tomcat"
SIM_TECH_TAGS = {   # (host, process) -> what agent.py fingerprint() would report
    ("prd-api-01", "java"): "java,kafka-clients,pgjdbc,spring-boot,spring-kafka,tomcat", ("prd-api-02", "java"): "java,pgjdbc,spring-boot,tomcat",
    ("prd-api-03", "node"): "node", ("worker-01", "python3"): "celery,libpq,librdkafka,psycopg,python", ("hr-app-01", "java"): "java,jaxws,ojdbc,weblogic",
    ("db-01", "postgres"): "postgres", ("pay-api-01", "uvicorn"): _PAY, ("portal-01", "nginx"): "nginx", ("crm-web-01", "node"): "node",
    ("erp-int-01", "java"): _ERP, ("sap-ecc-01", "disp+work"): "hana-client,sap-abap", ("sap-ecc-01", "gwrd"): "sap-gateway",
    ("hana-01", "hdbindexserver"): "hana", ("web-iis-01", "w3wp"): "aspnet,dotnet,iis,sqlclient", ("batch-01", "ssh"): "sftp",
    ("sftp-01", "sshd"): "openssh", ("mq-01", "beam.smp"): "erlang,rabbitmq"}
SIM_TECH_FLOWS = [  # host, process, remote (outbound)
    ("pay-api-01", "uvicorn", "172.16.2.10:1521"), ("pay-api-01", "uvicorn", "172.16.1.70:9092"), ("portal-01", "nginx", "172.16.5.21:8000"),
    ("crm-web-01", "node", "172.16.5.10:443"), ("erp-int-01", "java", "172.16.3.20:3300"), ("erp-int-01", "java", "172.16.2.20:7001"),
    ("erp-int-01", "java", "172.16.7.5:636"), ("erp-int-01", "java", "172.16.1.70:9092"), ("sap-ecc-01", "disp+work", "172.16.3.30:30015"),
    ("web-iis-01", "w3wp", "172.16.4.15:1433"), ("batch-01", "ssh", "172.16.6.30:22"), ("worker-01", "python3", "172.16.8.5:5672")]
SIM_TECH_LISTENS = [  # host, process, port (inbound)
    ("pay-api-01", "uvicorn", "8000"), ("portal-01", "nginx", "443"), ("erp-int-01", "java", "8080"), ("sap-ecc-01", "gwrd", "3300"),
    ("sap-ecc-01", "disp+work", "3200"), ("hana-01", "hdbindexserver", "30015"), ("hr-app-01", "java", "7001"), ("sftp-01", "sshd", "22"),
    ("mq-01", "beam.smp", "5672"), ("web-iis-01", "w3wp", "443")]


def simulate_tech_lines(rng, now: Optional[datetime] = None) -> bytes:
    """Instrumented request logs from two demo services (OpenTelemetry-style attributes), so traces confirm their frameworks."""
    now = now or datetime.now(UTC)
    out = []
    for host, svc, scope, lang, route, extra in (
            ("pay-api-01", "payment-api", "opentelemetry.instrumentation.fastapi", "python", "/payments/{payment_id}", {}),
            ("erp-int-01", "erp-integration", "io.opentelemetry.spring-webmvc-6.0", "java", "/api/orders/{id}", {"webengine.name": "Apache Tomcat"}),
            ("pay-api-01", "payment-api", "opentelemetry.instrumentation.oracledb", "python", None, {"db.system": "oracle"})):
        tid = "%032x" % rng.getrandbits(128)
        d = round(rng.uniform(4, 60), 1)
        rec = {"ts": now.isoformat(), "level": "info", "service": svc, "host": host, "env": "prod", "trace_id": tid, "span_id": "%016x" % rng.getrandbits(64),
               "duration_ms": d, "otel.scope.name": scope, "telemetry.sdk.language": lang, **extra}
        if route:
            rec.update({"msg": f"GET {route} 200", "span.kind": "server", "http.method": "GET", "http.route": route})
        else:
            rec.update({"msg": "SELECT ... FROM payments WHERE id = :1", "span.kind": "client"})
        out.append(json.dumps(rec))
    return ("\n".join(out) + "\n").encode()
