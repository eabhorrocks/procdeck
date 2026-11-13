"""Only one daemon may own the port; a second must exit before its job loop starts."""
import argparse

import pytest

from procdeck import cli


def test_claim_port_is_exclusive():
    first = cli.claim_port(0)  # any free port
    assert first is not None
    port = first.getsockname()[1]
    try:
        assert cli.claim_port(port) is None
    finally:
        first.close()
    second = cli.claim_port(port)  # free again once released
    assert second is not None
    second.close()


def test_serve_exits_before_starting_loop_when_port_taken(monkeypatch, tmp_path):
    holder = cli.claim_port(0)
    port = holder.getsockname()[1]
    started = []

    import procdeck.daemon
    monkeypatch.setattr(procdeck.daemon.Daemon, "__init__", lambda self: started.append("init"))
    monkeypatch.setattr(cli, "DAEMON_LOG", tmp_path / "daemon.log")
    try:
        with pytest.raises(SystemExit):
            cli.cmd_serve(argparse.Namespace(port=port))
    finally:
        holder.close()
    assert started == []
