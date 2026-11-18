"""Real SDK + wrapper regression tests; only the desktop transport is replaced."""

from typing import Any, cast
from unittest.mock import Mock

import anyio
import pytest
from mcp.shared import exceptions as mcp_exceptions

from tools.computer_use.cua_backend_session import _AsyncBridge, _CuaDriverSession


def sdk_error(code=-32000, message="Connection closed"):
    if hasattr(mcp_exceptions, "MCPError"):
        return mcp_exceptions.MCPError(code, message)
    from mcp.types import ErrorData
    return getattr(mcp_exceptions, "McpError")(ErrorData(code=code, message=message))


class MemoryTransport:
    def __init__(self, effects):
        self.effects = iter(effects)
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        effect = next(self.effects)
        if isinstance(effect, Exception):
            raise effect
        return effect


@pytest.fixture
def session_factory(monkeypatch):
    bridge = _AsyncBridge()
    bridge.start()

    def make(*effects):
        session = _CuaDriverSession(bridge)
        session._started = True
        transport = MemoryTransport(effects)
        session._session = cast(Any, transport)
        # No driver, windows, sockets or permissions: replace only lifecycle IO.
        stop, start = Mock(), Mock()
        monkeypatch.setattr(session, "_stop_lifecycle_locked", stop)
        monkeypatch.setattr(session, "_start_lifecycle_locked", start)
        return session, transport, stop, start

    yield make
    bridge.stop()


@pytest.mark.parametrize("name", ["ClosedResourceError", "BrokenResourceError", "EndOfStream"])
def test_application_exception_with_transport_name_is_not_closed(name):
    error = type(name, (Exception,), {})("Connection closed")
    assert not _CuaDriverSession._is_closed_session_error(error)


def test_sdk_closed_read_recovers_and_next_call_uses_session(session_factory):
    session, transport, stop, start = session_factory(
        sdk_error(), {"content": [], "isError": False}, {"content": [], "isError": False})
    assert session.call_tool("get_window_state", {})["isError"] is False
    assert session.call_tool("list_windows", {})["isError"] is False
    assert transport.calls == [("get_window_state", {}), ("get_window_state", {}), ("list_windows", {})]
    stop.assert_called_once_with()
    start.assert_called_once_with()


@pytest.mark.parametrize("name", ["click", "type_text", "press_key", "scroll", "unknown_tool"])
def test_sdk_closed_mutation_is_never_replayed(session_factory, name):
    session, transport, stop, start = session_factory(sdk_error(), {"content": []})
    result = session.call_tool(name, {})
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "transport_outcome_unknown"
    assert result["structuredContent"]["next_step"] == "fresh_state"
    assert transport.calls == [(name, {})]
    stop.assert_called_once_with()
    start.assert_called_once_with()
    assert session.call_tool("list_windows", {})["isError"] is False
    start.assert_called_once_with()


def test_second_sdk_closure_does_not_reconnect_again(session_factory):
    second = sdk_error()
    session, transport, stop, start = session_factory(sdk_error(), second)
    with pytest.raises(type(second)) as caught:
        session.call_tool("get_window_state", {})
    assert caught.value is second
    assert transport.calls == [("get_window_state", {}), ("get_window_state", {})]
    stop.assert_called_once_with()
    start.assert_called_once_with()


@pytest.mark.parametrize("error", [
    sdk_error(-32000, "Application failed"),
    sdk_error(-32603, "Connection closed"),
    sdk_error(-32000, "Connection closed by application policy"),
    RuntimeError("Connection closed"),
])
def test_application_errors_propagate_without_reconnect(session_factory, error):
    session, transport, stop, start = session_factory(error)
    assert not session._is_closed_session_error(error)
    with pytest.raises(type(error)) as caught:
        session.call_tool("get_window_state", {})
    assert caught.value is error
    assert transport.calls == [("get_window_state", {})]
    stop.assert_not_called()
    start.assert_not_called()


@pytest.mark.parametrize("error", [
    anyio.ClosedResourceError(), anyio.BrokenResourceError(), anyio.EndOfStream(),
    BrokenPipeError(), EOFError(), sdk_error(),
])
def test_real_transport_errors_remain_reconnectable(error):
    assert _CuaDriverSession._is_closed_session_error(error)


def test_sdk_reconnect_restores_label_before_read(session_factory):
    session, transport, stop, start = session_factory(
        sdk_error(), {"content": []}, {"content": []})
    session._declared_session_id = "test-label"
    assert session.call_tool("list_windows", {})["isError"] is False
    assert transport.calls == [
        ("list_windows", {}), ("start_session", {"session": "test-label"}), ("list_windows", {})]
    stop.assert_called_once_with()
    start.assert_called_once_with()


def test_failed_reconnect_never_replays_mutation(session_factory):
    session, transport, stop, start = session_factory(sdk_error())
    start.side_effect = RuntimeError("replacement unavailable")
    with pytest.raises(RuntimeError, match="replacement unavailable"):
        session.call_tool("click", {})
    assert transport.calls == [("click", {})]
    assert session._started is False
    stop.assert_called_once_with()
    start.assert_called_once_with()
