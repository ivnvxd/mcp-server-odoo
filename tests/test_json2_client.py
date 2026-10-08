"""Json2Client against a fake JSON-2 server.

The error statuses, names and messages are the ones a real Odoo 20 sent
(captured 2026-10-07). Each ``debug`` field is replaced by a traceback full of
sentinels: none of them may reach an exception.
"""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.json2_client import Json2Client, json2_arguments
from mcp_server_odoo.odoo_connection import (
    DATABASE_LISTING_FAILED,
    OdooConnection,
    OdooConnectionError,
    OdooRequestFault,
    OdooUnreachableError,
    OdooValidationFault,
)

SECRET = "sk-sentinel-4f1d"
DEBUG = (
    "Traceback (most recent call last):\n"
    '  File "/opt/odoo/sentinel_path/odoo/http.py", line 2150, in _serve_db\n'
    f"    password = {SECRET!r}\n"
    "odoo.exceptions.Sentinel: internal detail"
)
SENTINELS = (SECRET, "sentinel_path", "internal detail")


def odoo_error(name, message, *arguments):
    return {
        "name": name,
        "message": message,
        "arguments": list(arguments) or [message],
        "timestamp": 1791375789,
        "context": {"sentinel_context": SECRET},
        "debug": DEBUG,
    }


class FakeOdoo(ThreadingHTTPServer):
    """Answers each POST from ``self.script`` (a list, consumed in order)."""

    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.script = []
        self.requests = []
        self.connections = 0
        self.closed = threading.Event()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keepalive, so connection reuse is tested

    def setup(self):
        super().setup()
        self.server.connections += 1

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.requests.append((self.path, dict(self.headers), None))
        self._answer()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.server.requests.append((self.path, dict(self.headers), json.loads(body or b"{}")))
        self._answer()

    def _answer(self):
        step = self.server.script.pop(0)
        if step.get("delay"):
            time.sleep(step["delay"])
        status = step.get("status", 200)
        payload = step.get("raw")
        if payload is None:
            payload = json.dumps(step.get("json")).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", step.get("content_type", "application/json"))
            for key, value in step.get("headers", {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
        except OSError:
            return  # the client gave up (a timeout test)
        if step.get("close_after"):
            # Close while the client still believes the connection is alive
            self.close_connection = True

    def finish(self):
        super().finish()
        if self.close_connection:
            self.server.closed.set()


@pytest.fixture
def server():
    server = FakeOdoo()
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def client_for(server, timeout=2.0, path="", database="odoo"):
    return Json2Client(f"{server.url}{path}", "key-123", database, timeout)


class TestCalls:
    def test_success_sends_bearer_database_and_json(self, server):
        server.script = [{"json": [{"id": 3, "name": "Administrator"}]}]
        client = client_for(server)

        result = client.call(
            "res.partner", "search_read", {"domain": [["id", "=", 3]], "fields": ["name"]}
        )

        assert result == [{"id": 3, "name": "Administrator"}]
        path, headers, body = server.requests[0]
        assert path == "/json/2/res.partner/search_read"
        assert headers["Authorization"] == "bearer key-123"
        assert headers["X-Odoo-Database"] == "odoo"
        assert headers["Content-Type"].startswith("application/json")
        assert body == {"domain": [["id", "=", 3]], "fields": ["name"]}

    def test_url_path_prefix_is_kept(self, server):
        server.script = [{"json": [3]}]

        client_for(server, path="/odoo/").call("res.partner", "search", {"domain": []})

        assert server.requests[0][0] == "/odoo/json/2/res.partner/search"

    def test_no_database_header_without_a_database(self, server):
        server.script = [{"json": []}]

        client_for(server, database=None).call("res.partner", "search", {})

        assert "X-Odoo-Database" not in server.requests[0][1]

    def test_keepalive_reuses_one_connection(self, server):
        server.script = [{"json": [1]}, {"json": [2]}, {"json": [3]}]
        client = client_for(server)

        results = [client.call("res.partner", "search", {}) for _ in range(3)]

        assert results == [[1], [2], [3]]
        assert server.connections == 1

    def test_invalid_json_in_a_success(self, server):
        server.script = [{"raw": b"<html>ok</html>"}]

        with pytest.raises(OdooConnectionError, match="invalid JSON"):
            client_for(server).call("res.partner", "search", {})


class TestRetry:
    def test_stale_keepalive_connection_is_retried_once(self, server):
        server.script = [{"json": [1], "close_after": True}, {"json": [2]}]
        client = client_for(server)
        client.call("res.partner", "search", {})
        assert server.closed.wait(2), "the server did not close the idle connection"

        result = client.call("res.partner", "write", {"ids": [1], "vals": {}})

        assert result == [2]
        assert server.connections == 2
        assert len(server.requests) == 2

    def test_timeout_on_a_reused_connection_retries_a_read(self, server):
        server.script = [{"json": [1]}, {"json": [2], "delay": 0.6}, {"json": [3]}]
        client = client_for(server, timeout=0.3)
        client.call("res.partner", "search", {})

        result = client.call("res.partner", "search", {}, retry_safe=True)

        assert result == [3]
        assert len(server.requests) == 3

    def test_timeout_is_not_retried_for_a_write(self, server):
        server.script = [{"json": [1]}, {"json": True, "delay": 0.6}]
        client = client_for(server, timeout=0.3)
        client.call("res.partner", "search", {})

        with pytest.raises(OdooConnectionError, match="Operation timeout after 0.3 seconds"):
            client.call("res.partner", "write", {"ids": [1], "vals": {}}, retry_safe=False)
        assert len(server.requests) == 2

    def test_timeout_on_a_fresh_connection_is_not_retried(self, server):
        server.script = [{"json": [1], "delay": 0.6}]

        # Unreachable, so the server keeps running and retries with a backoff
        with pytest.raises(OdooUnreachableError, match="Operation timeout"):
            client_for(server, timeout=0.3).call("res.partner", "search", {}, retry_safe=True)
        assert len(server.requests) == 1

    def test_the_connection_works_again_after_an_error(self, server):
        server.script = [{"json": [1], "delay": 0.6}, {"json": [2]}]
        client = client_for(server, timeout=0.3)
        with pytest.raises(OdooConnectionError):
            client.call("res.partner", "search", {})

        assert client.call("res.partner", "search", {}) == [2]

    def test_nothing_listening_is_unreachable(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        client = Json2Client(f"http://127.0.0.1:{port}", "key", "odoo", 1.0)

        with pytest.raises(OdooUnreachableError, match="Cannot connect to Odoo") as caught:
            client.call("res.partner", "search", {})
        assert str(caught.value).count("Cannot connect to Odoo") == 1


# (status, Odoo error body, exception, message part, fault code), from Odoo 20
ERRORS = [
    pytest.param(
        422,
        odoo_error(
            "odoo.exceptions.ValidationError",
            "The operation cannot be completed: Contacts require a name",
        ),
        OdooValidationFault,
        "Contacts require a name",
        2,
        id="validation-error",
    ),
    pytest.param(
        403,
        odoo_error(
            "odoo.exceptions.AccessError",
            'You do not have enough rights to access the field "credit" on Contact '
            "(res.partner). Please contact your system administrator.\n\n"
            "Operation: read\nUser: 16665",
        ),
        OdooValidationFault,
        'access the field "credit"',
        4,
        id="access-error",
    ),
    pytest.param(
        403,
        odoo_error(
            "odoo.exceptions.AccessError",
            "Private methods (such as 'res.partner._compute_display_name') cannot be "
            "called remotely.",
        ),
        OdooValidationFault,
        "Private methods",
        4,
        id="private-method",
    ),
    pytest.param(
        404,
        odoo_error("werkzeug.exceptions.NotFound", "the model 'x.nope' does not exist"),
        OdooValidationFault,
        "the model 'x.nope' does not exist",
        2,
        id="unknown-model",
    ),
    pytest.param(
        404,
        odoo_error("werkzeug.exceptions.NotFound", "The method 'res.partner.nope' does not exist"),
        OdooValidationFault,
        "The method 'res.partner.nope' does not exist",
        2,
        id="unknown-method",
    ),
    pytest.param(
        422,
        odoo_error(
            "werkzeug.exceptions.UnprocessableEntity",
            "got an unexpected keyword argument 'bogus'",
        ),
        OdooValidationFault,
        "unexpected keyword argument 'bogus'",
        2,
        id="unknown-argument",
    ),
    pytest.param(
        500,
        odoo_error(
            "builtins.ValueError", "Invalid field res.partner.nope in condition ('nope', '=', 1)"
        ),
        OdooValidationFault,
        "Invalid field 'res.partner.nope'",
        1,
        id="invalid-field-500",
    ),
]


class TestErrors:
    @pytest.mark.parametrize("status,body,exception,message,code", ERRORS)
    def test_business_errors(self, server, status, body, exception, message, code):
        server.script = [{"status": status, "json": body}]

        with pytest.raises(exception) as caught:
            client_for(server).call("res.partner", "search", {})

        assert message in str(caught.value)
        assert caught.value.fault_code == code
        self._assert_no_sentinel(caught.value)

    def test_a_400_is_an_odoo_error(self, server):
        server.script = [
            {
                "status": 400,
                "json": odoo_error("werkzeug.exceptions.BadRequest", "bad arguments", 400),
            }
        ]

        with pytest.raises(
            OdooRequestFault, match="Odoo refused the request: Bad arguments"
        ) as caught:
            client_for(server).call("res.partner", "write", {})
        assert not isinstance(caught.value, OdooValidationFault)

    def test_a_500_that_is_not_business_is_an_odoo_error(self, server):
        server.script = [{"status": 500, "json": odoo_error("builtins.KeyError", "nope")}]

        with pytest.raises(OdooRequestFault, match="Odoo error: KeyError: nope") as caught:
            client_for(server).call("res.partner", "write", {})
        assert not isinstance(caught.value, OdooValidationFault)
        self._assert_no_sentinel(caught.value)

    def test_a_refused_key(self, server):
        server.script = [
            {
                "status": 401,
                "json": odoo_error("werkzeug.exceptions.Unauthorized", "Invalid apikey", 401),
            }
        ]

        with pytest.raises(OdooConnectionError, match="refused the API key") as caught:
            client_for(server).call("res.partner", "search", {})
        assert not isinstance(caught.value, OdooValidationFault)
        assert "needs the rpc scope" in str(caught.value)
        self._assert_no_sentinel(caught.value)

    def test_access_denied_stays_connection_flavored(self, server):
        server.script = [
            {"status": 403, "json": odoo_error("odoo.exceptions.AccessDenied", "Access Denied")}
        ]

        with pytest.raises(OdooConnectionError) as caught:
            client_for(server).call("res.users", "check", {})
        assert not isinstance(caught.value, OdooValidationFault)

    @pytest.mark.parametrize("status", [502, 503, 504])
    def test_gateway_errors_are_unreachable(self, server, status):
        server.script = [
            {"status": status, "raw": b"<html>Bad Gateway</html>", "content_type": "text/html"}
        ]

        with pytest.raises(OdooUnreachableError, match=f"HTTP {status}"):
            client_for(server).call("res.partner", "search", {})

    def test_html_not_found_names_the_status(self, server):
        """An unknown database: Odoo answers 404 with its HTML page."""
        server.script = [
            {
                "status": 404,
                "raw": b"<!DOCTYPE html><title>Not Found</title>",
                "content_type": "text/html",
            }
        ]

        with pytest.raises(OdooConnectionError, match="HTTP 404 without a JSON error"):
            client_for(server).call("res.partner", "search", {})

    def test_a_redirect_names_the_target_only_in_the_log(self, server, caplog):
        """The target can be an internal host; the error reaches the client."""
        server.script = [
            {
                "status": 301,
                "raw": b"",
                "headers": {"Location": "https://odoo.example.com/json/2/res.partner/search"},
            }
        ]

        with pytest.raises(OdooConnectionError, match=r"redirected the request \(HTTP 301\)") as e:
            client_for(server).call("res.partner", "search", {})
        assert "odoo.example.com" not in str(e.value)
        assert "https://odoo.example.com" in caplog.text

    @staticmethod
    def _assert_no_sentinel(error):
        for text in (str(error), str(error.__cause__), repr(error.__cause__)):
            for sentinel in SENTINELS:
                assert sentinel not in text


VERSION_20 = {"version_info": [20, 0, 0, "final", 0, ""], "version": "20.0"}
HTML_404 = {
    "status": 404,
    "raw": b"<!DOCTYPE html><title>Not Found</title>",
    "content_type": "text/html",
}


def json2_connection(server, **config):
    config = OdooConfig(
        **{
            "url": server.url,
            "api_key": "key-123",
            "yolo_mode": "read",
            "rpc_transport": "json2",
            **config,
        }
    )
    return OdooConnection(config, timeout=2)


def paths(server):
    return [path for path, _, _ in server.requests]


class TestVersion:
    def test_web_version(self, server):
        server.script = [{"json": VERSION_20}]

        assert client_for(server).version() == VERSION_20
        assert server.requests[0][0] == "/web/version"
        assert "Authorization" not in server.requests[0][1]

    def test_no_version_route_means_no_json2(self, server):
        """Odoo 18 and older answer /web/version with the HTML 404 page."""
        server.script = [HTML_404]

        with pytest.raises(OdooConnectionError, match="JSON-2 needs Odoo 19 or later"):
            client_for(server).version()

    def test_a_gateway_error_is_unreachable(self, server):
        server.script = [{"status": 503, "raw": b"down", "content_type": "text/plain"}]

        with pytest.raises(OdooUnreachableError):
            client_for(server).version()


class TestJson2Connection:
    def test_connect_and_authenticate_send_no_xmlrpc(self, server):
        server.script = [{"json": VERSION_20}, {"json": {"lang": "en_US", "tz": "UTC", "uid": 7}}]
        connection = json2_connection(server, database="odoo")

        connection.connect()
        connection.authenticate()

        assert paths(server) == ["/web/version", "/json/2/res.users/context_get"]
        assert server.requests[1][1]["X-Odoo-Database"] == "odoo"
        assert connection.rpc_transport == "json2"
        assert connection.get_major_version() == 20
        assert (connection.uid, connection.database, connection.auth_method) == (
            7,
            "odoo",
            "api_key",
        )
        assert connection.is_authenticated

    def test_the_database_comes_from_the_web_listing(self, server):
        server.script = [
            {"json": VERSION_20},
            {"json": {"jsonrpc": "2.0", "id": None, "result": ["odoo"]}},
            {"json": {"uid": 2}},
        ]
        connection = json2_connection(server)

        connection.connect()
        connection.authenticate()

        assert paths(server) == [
            "/web/version",
            "/web/database/list",
            "/json/2/res.users/context_get",
        ]
        assert connection.database == "odoo"

    def test_a_failed_listing_has_no_xmlrpc_fallback(self, server):
        server.script = [{"json": VERSION_20}, HTML_404]
        connection = json2_connection(server)
        connection.connect()

        with pytest.raises(OdooConnectionError) as caught:
            connection.authenticate()
        assert str(caught.value) == DATABASE_LISTING_FAILED
        assert not any(path.startswith("/xmlrpc") for path in paths(server))

    def test_a_refused_key(self, server):
        server.script = [
            {"json": VERSION_20},
            {
                "status": 401,
                "json": odoo_error("werkzeug.exceptions.Unauthorized", "Invalid apikey", 401),
            },
        ]
        connection = json2_connection(server, database="odoo")
        connection.connect()

        with pytest.raises(OdooConnectionError, match="refused the API key"):
            connection.authenticate()
        assert not connection.is_authenticated

    @pytest.mark.parametrize("answer", [{"lang": "en_US"}, {"uid": False}, {"uid": True}, []])
    def test_no_user_for_the_key(self, server, answer):
        server.script = [{"json": VERSION_20}, {"json": answer}]
        connection = json2_connection(server, database="odoo")
        connection.connect()

        with pytest.raises(OdooConnectionError, match="named no user for the API key"):
            connection.authenticate()
        assert not connection.is_authenticated

    def test_connect_on_odoo_18_fails_cleanly(self, server):
        server.script = [HTML_404]
        connection = json2_connection(server)

        with pytest.raises(OdooConnectionError, match="Odoo 19 or later"):
            connection.connect()
        assert not connection.is_connected
        assert connection.rpc_transport == "xmlrpc"

    def test_execute_kw_goes_over_json2(self, server):
        server.script = [
            {"json": VERSION_20},
            {"json": {"uid": 2}},
            {"json": [3, 4]},
            {"json": True},
        ]
        connection = json2_connection(server, database="odoo")
        connection.connect()
        connection.authenticate()

        found = connection.execute_kw(
            "res.partner", "search", [], {"domain": [], "limit": 2, "context": {"lang": "de_DE"}}
        )
        connection.execute_kw("res.partner", "unlink", [[3, 4]], {})

        assert found == [3, 4]
        assert server.requests[2][0] == "/json/2/res.partner/search"
        assert server.requests[2][2] == {"domain": [], "limit": 2, "context": {"lang": "de_DE"}}
        assert server.requests[3][0] == "/json/2/res.partner/unlink"
        assert server.requests[3][2] == {"ids": [3, 4]}

    def test_disconnect_closes_the_client(self, server):
        server.script = [{"json": VERSION_20}, {"json": {"uid": 2}}]
        connection = json2_connection(server, database="odoo")
        connection.connect()
        connection.authenticate()

        connection.disconnect()

        assert connection.rpc_transport == "xmlrpc"
        assert not connection.is_connected and not connection.is_authenticated


class TestJson2Arguments:
    """Positional XML-RPC arguments get their JSON-2 parameter names."""

    @pytest.mark.parametrize(
        "method,args,kwargs,body",
        [
            ("search", [[["id", "=", 3]]], {"limit": 1}, {"domain": [["id", "=", 3]], "limit": 1}),
            ("search", [[]], {}, {"domain": []}),
            (
                "search_read",
                [[["active", "=", True]], ["name"]],
                {},
                {"domain": [["active", "=", True]], "fields": ["name"]},
            ),
            (
                "search_count",
                [[]],
                {"context": {"active_test": False}},
                {"domain": [], "context": {"active_test": False}},
            ),
            (
                "fields_get",
                [["name"]],
                {"attributes": ["type"]},
                {"allfields": ["name"], "attributes": ["type"]},
            ),
            ("fields_get", [], {}, {}),
            ("create", [{"name": "A"}], {}, {"vals_list": {"name": "A"}}),
            (
                "create",
                [[{"name": "A"}, {"name": "B"}]],
                {},
                {"vals_list": [{"name": "A"}, {"name": "B"}]},
            ),
            ("read", [[3], ["name", "active"]], {}, {"ids": [3], "fields": ["name", "active"]}),
            ("read", [[3]], {"fields": ["name"]}, {"ids": [3], "fields": ["name"]}),
            ("write", [[3, 4], {"name": "B"}], {}, {"ids": [3, 4], "vals": {"name": "B"}}),
            ("unlink", [[3]], {}, {"ids": [3]}),
            ("unlink", [[]], {}, {"ids": []}),
            (
                "web_save_multi",
                [[3], [{"name": "B"}], {"display_name": {}}],
                {},
                {"ids": [3], "vals_list": [{"name": "B"}], "specification": {"display_name": {}}},
            ),
            ("message_post", [7], {"body": "Hi"}, {"ids": [7], "body": "Hi"}),
            (
                "formatted_read_group",
                [[]],
                {"groupby": ["is_company"], "aggregates": ["__count"]},
                {"domain": [], "groupby": ["is_company"], "aggregates": ["__count"]},
            ),
            ("context_get", [], {}, {}),
            # call_model_method: any other method, a leading id list only
            ("action_archive", [[5, 6]], {}, {"ids": [5, 6]}),
            (
                "action_confirm",
                [5],
                {"context": {"lang": "de_DE"}},
                {"ids": [5], "context": {"lang": "de_DE"}},
            ),
            ("get_import_templates", [], {}, {}),
        ],
    )
    def test_table(self, method, args, kwargs, body):
        assert json2_arguments("res.partner", method, args, kwargs) == body

    @pytest.mark.parametrize(
        "method,args,message",
        [
            ("action_confirm", [[5], "x"], "pass the arguments of res.partner.action_confirm"),
            ("action_confirm", [True], "keyword_arguments"),
            ("action_confirm", [[]], "keyword_arguments"),
            ("name_create", ["Acme"], "keyword_arguments"),
            ("read", [["name"]], "needs the record ids first"),
            ("write", [], "needs the record ids first"),
            ("read", [[True]], "needs the record ids first"),
            ("search", [[], 0, 10, "name", "extra"], "takes at most 4 arguments"),
        ],
    )
    def test_refusals(self, method, args, message):
        with pytest.raises(OdooValidationFault, match=message):
            json2_arguments("res.partner", method, args, {})

    def test_a_name_given_twice(self):
        with pytest.raises(OdooValidationFault, match="domain is given twice"):
            json2_arguments("res.partner", "search", [[]], {"domain": []})


def connected_json2(server, **config):
    server.script = [{"json": VERSION_20}, {"json": {"uid": 2}}] + server.script
    connection = json2_connection(server, database="odoo", **config)
    connection.connect()
    connection.authenticate()
    return connection


class TestJson2ResultsAndFallbacks:
    def test_check_health_over_json2(self, server):
        """There is no XML-RPC proxy on a JSON-2 connection."""
        server.script = [{"json": VERSION_20}, {"json": VERSION_20}]
        connection = connected_json2(server)

        healthy, message = connection.check_health()

        assert healthy, message
        assert message.startswith("Connected to Odoo 20")
        assert connection.test_connection()

    def test_create_with_one_dict_returns_the_id(self, server):
        server.script = [{"json": [41]}, {"json": [42, 43]}]
        connection = connected_json2(server)

        assert connection.create("res.partner", {"name": "A"}) == 41
        assert connection.create_many("res.partner", [{"name": "B"}, {"name": "C"}]) == [42, 43]

    def test_an_invalid_language_is_dropped_and_retried(self, server):
        invalid = odoo_error("odoo.exceptions.UserError", "Invalid language code: xx_XX")
        server.script = [{"status": 422, "json": invalid}, {"json": [3]}]
        connection = connected_json2(server, locale="xx_XX")

        assert connection.search("res.partner", [["id", "=", 3]]) == [3]
        assert server.requests[2][2]["context"] == {"lang": "xx_XX"}
        assert "lang" not in server.requests[3][2].get("context", {})
        assert connection.config.locale is None

    def test_another_user_error_is_not_retried(self, server):
        server.script = [
            {"status": 422, "json": odoo_error("odoo.exceptions.UserError", "Nope")},
        ]
        connection = connected_json2(server, locale="de_DE")

        with pytest.raises(OdooValidationFault, match="Nope"):
            connection.search("res.partner", [])
        assert connection.config.locale == "de_DE"

    def test_the_company_check_reads_over_json2(self, server):
        """check_allowed_companies passes the fields of read positionally."""
        server.script = [{"json": [{"id": 2, "company_ids": [1, 3]}]}]
        connection = connected_json2(server, allowed_companies=[3])

        connection.check_allowed_companies()

        path, _, body = server.requests[2]
        assert path == "/json/2/res.users/read"
        assert body == {"ids": [2], "fields": ["company_ids"]}


def xmlrpc_proxies(monkeypatch, connection, version="18.0"):
    """Stand-in XML-RPC proxies, so a fallback can finish without a real Odoo."""
    from unittest.mock import MagicMock

    proxy = MagicMock()
    proxy.version.return_value = {"server_version": version}
    proxy.authenticate.return_value = 2
    monkeypatch.setattr(
        connection._performance_manager, "get_optimized_connection", lambda endpoint: proxy
    )
    return proxy


def auto_connection(server, **config):
    return json2_connection(server, **{"rpc_transport": "auto", **config})


class TestAutoSelection:
    def test_odoo_19_and_later_with_a_key_take_json2(self, server):
        server.script = [{"json": VERSION_20}, {"json": {"uid": 2}}]
        connection = auto_connection(server, database="odoo")

        connection.connect()
        connection.authenticate()

        assert connection.rpc_transport == "json2"

    def test_no_web_version_falls_back_to_xmlrpc(self, server, monkeypatch, caplog):
        caplog.set_level("INFO", logger="mcp_server_odoo.odoo_connection")
        server.script = [HTML_404]
        connection = auto_connection(server, database="odoo", username="admin")
        proxy = xmlrpc_proxies(monkeypatch, connection)

        connection.connect()
        connection.authenticate()

        assert connection.rpc_transport == "xmlrpc"
        assert connection.server_version == "18.0"
        assert proxy.authenticate.called
        assert "JSON-2 is not available" in caplog.text

    def test_an_old_version_string_falls_back_to_xmlrpc(self, server, monkeypatch):
        """A server that answers /web/version but runs Odoo 18."""
        server.script = [{"json": {"version_info": [18, 0], "version": "saas~18.4"}}]
        connection = auto_connection(server, database="odoo", username="admin")
        xmlrpc_proxies(monkeypatch, connection)

        connection.connect()

        assert connection.rpc_transport == "xmlrpc"

    def test_the_fallback_needs_odoo_user(self, server):
        server.script = [HTML_404]
        connection = auto_connection(server, database="odoo")

        with pytest.raises(OdooConnectionError, match="needs ODOO_USER with the API key"):
            connection.connect()
        assert not connection.is_connected

    def test_a_blocked_json2_route_falls_back_to_xmlrpc(self, server, monkeypatch):
        """/web/version answers, but a proxy answers /json/2 with an HTML page."""
        listing = {"json": {"jsonrpc": "2.0", "id": None, "result": ["odoo"]}}
        server.script = [{"json": VERSION_20}, HTML_404, listing]
        connection = auto_connection(server, database="odoo", username="admin")
        xmlrpc_proxies(monkeypatch, connection, version="20.0")

        connection.connect()
        connection.authenticate()

        assert connection.rpc_transport == "xmlrpc"
        assert connection.is_authenticated

    def test_a_failed_listing_still_falls_back(self, server, monkeypatch):
        server.script = [{"json": VERSION_20}, HTML_404, HTML_404]
        connection = auto_connection(server, database="odoo", username="admin")
        xmlrpc_proxies(monkeypatch, connection, version="20.0")

        connection.connect()
        connection.authenticate()

        assert connection.rpc_transport == "xmlrpc"

    @pytest.mark.parametrize("transport", ["auto", "json2"])
    def test_an_unknown_database_is_named_not_a_fallback(self, server, transport):
        """Odoo answers /json/2 for an unknown database with its HTML 404 page."""
        listing = {"json": {"jsonrpc": "2.0", "id": None, "result": ["odoo"]}}
        server.script = [{"json": VERSION_20}, HTML_404, listing]
        connection = json2_connection(server, database="nope", rpc_transport=transport)
        connection.connect()

        with pytest.raises(OdooConnectionError, match="Database 'nope' does not exist"):
            connection.authenticate()
        assert connection.rpc_transport == "json2"

    @pytest.mark.parametrize("transport", ["auto", "json2"])
    def test_a_redirect_is_not_a_fallback(self, server, monkeypatch, transport):
        """http:// redirected to https:// on an Odoo 19: not a server without JSON-2."""
        server.script = [
            {
                "status": 301,
                "raw": b"",
                "headers": {"Location": "https://odoo.example.com/web/version"},
            }
        ]
        connection = json2_connection(
            server, database="odoo", username="admin", rpc_transport=transport
        )
        proxy = xmlrpc_proxies(monkeypatch, connection)

        with pytest.raises(OdooConnectionError, match="https://odoo.example.com") as caught:
            connection.connect()
        assert "Odoo 19 or later" not in str(caught.value)
        assert not proxy.version.called

    def test_a_refused_key_does_not_fall_back(self, server):
        server.script = [
            {"json": VERSION_20},
            {
                "status": 401,
                "json": odoo_error("werkzeug.exceptions.Unauthorized", "Invalid apikey", 401),
            },
        ]
        connection = auto_connection(server, database="odoo", username="admin")
        connection.connect()

        with pytest.raises(OdooConnectionError, match="refused the API key"):
            connection.authenticate()

    def test_a_refused_key_falls_back_to_the_password(self, server, monkeypatch, caplog):
        """As over XML-RPC: with ODOO_USER and ODOO_PASSWORD a refused key is not fatal."""
        caplog.set_level("INFO", logger="mcp_server_odoo.odoo_connection")
        server.script = [
            {"json": VERSION_20},
            {
                "status": 401,
                "json": odoo_error("werkzeug.exceptions.Unauthorized", "Invalid apikey", 401),
            },
        ]
        connection = auto_connection(server, database="odoo", username="admin", password="admin")
        proxy = xmlrpc_proxies(monkeypatch, connection, version="20.0")
        proxy.authenticate.side_effect = [False, 2]  # the key as password, then the password
        connection.connect()

        connection.authenticate()

        assert connection.rpc_transport == "xmlrpc"
        assert connection.is_authenticated
        # The server has JSON-2: the log blames the key, not a missing route
        assert "JSON-2 is not available" not in caplog.text

    def test_forced_json2_never_falls_back_on_a_refused_key(self, server):
        server.script = [
            {"json": VERSION_20},
            {
                "status": 401,
                "json": odoo_error("werkzeug.exceptions.Unauthorized", "Invalid apikey", 401),
            },
        ]
        connection = json2_connection(server, database="odoo", username="admin", password="admin")
        connection.connect()

        with pytest.raises(OdooConnectionError, match="refused the API key"):
            connection.authenticate()

    def test_forced_json2_never_falls_back(self, server):
        server.script = [{"json": VERSION_20}, HTML_404]
        connection = json2_connection(server, database="odoo", username="admin")
        connection.connect()

        with pytest.raises(OdooConnectionError, match="HTTP 404"):
            connection.authenticate()

    def test_odoo_down_is_unreachable_not_a_fallback(self, server, monkeypatch):
        server.script = [{"status": 503, "raw": b"down", "content_type": "text/plain"}]
        connection = auto_connection(server, database="odoo", username="admin")
        proxy = xmlrpc_proxies(monkeypatch, connection)

        with pytest.raises(OdooUnreachableError):
            connection.connect()
        assert not proxy.version.called

    @pytest.mark.parametrize(
        "config",
        [
            {"yolo_mode": "off"},  # standard mode: the MCP module's XML-RPC
            {"api_key": None, "username": "admin", "password": "admin"},  # no key
            {"rpc_transport": "xmlrpc", "username": "admin"},
        ],
        ids=["standard-mode", "password-only", "forced-xmlrpc"],
    )
    def test_xmlrpc_without_trying_json2(self, server, monkeypatch, config):
        connection = auto_connection(server, database="odoo", **config)
        xmlrpc_proxies(monkeypatch, connection)
        monkeypatch.setattr(connection, "_resolve_and_set_database", lambda: None)

        connection.connect()

        assert connection.rpc_transport == "xmlrpc"
        assert server.requests == []


class TestHealth:
    def test_health_names_the_transport(self, server):
        from unittest.mock import patch

        from mcp_server_odoo.server import OdooMCPServer

        server.script = [{"json": VERSION_20}, {"json": {"uid": 2}}]
        config = OdooConfig(url=server.url, api_key="key-123", yolo_mode="read", database="odoo")
        with patch("mcp_server_odoo.server.build_user_context", return_value="ctx"):
            mcp_server = OdooMCPServer(config)
            mcp_server._connect()

        assert mcp_server.get_health_status()["connection"] == {
            "connected": True,
            "rpc_transport": "json2",
        }
