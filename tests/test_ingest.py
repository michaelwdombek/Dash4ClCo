import pytest

from d4cc_server import board
from d4cc_server.ingest import InvalidEvent

from conftest import load_fixture, to_event


def replay(ingestor, host_id, raws, t0=1000.0, **extra):
    for i, raw in enumerate(raws):
        ingestor.apply(host_id, to_event(raw, t0 + i, **extra), received_at=t0 + i)


def session(conn, sid):
    return board.get_session(conn, sid)


def test_headless_fixture_full_lifecycle(conn, host, ingestor):
    raws = load_fixture("hook-payloads-2.1.330.jsonl")
    sid = raws[0]["session_id"]
    replay(ingestor, host["id"], raws)
    s = session(conn, sid)
    assert s["state"] == "ended"
    assert s["end_reason"] == "other"
    assert len(s["subagents"]) == 1
    sa = s["subagents"][0]
    assert sa["agent_type"] == "Explore"
    assert sa["description"] == "List files in cwd"
    assert sa["ended_at"] is not None
    events = conn.execute("SELECT COUNT(*) FROM events WHERE session_id = ?", (sid,)).fetchone()[0]
    assert events == len(raws)


def test_headless_fixture_states_step_by_step(conn, host, ingestor):
    raws = load_fixture("hook-payloads-2.1.330.jsonl")
    sid = raws[0]["session_id"]
    seen = []
    for i, raw in enumerate(raws):
        ingestor.apply(host["id"], to_event(raw, 1000 + i), received_at=1000 + i)
        s = session(conn, sid)
        seen.append((raw["hook_event_name"], s["state"], s["activity"]))
    assert seen[0][1] == "idle"                                   # SessionStart
    assert seen[1] == ("UserPromptSubmit", "working", "Thinking")
    assert seen[2] == ("PreToolUse", "working", "Bash: Print hello")
    assert seen[4] == ("PreToolUse", "working", "Agent: List files in cwd")
    # tool calls of the subagent do not change the main activity, they go on the subagent row
    assert seen[6][2] == "Agent: List files in cwd"
    assert seen[14] == ("UserPromptSubmit", "working", "Processing subagent result")
    assert seen[15] == ("Stop", "idle", None)


def test_subagent_activity_tracked(conn, host, ingestor):
    raws = load_fixture("hook-payloads-2.1.330.jsonl")
    sid = raws[0]["session_id"]
    replay(ingestor, host["id"], raws[:7])  # up to the subagent's first PreToolUse
    sa = session(conn, sid)["subagents"][0]
    assert sa["agent_id"] == "a03b3c3bc60ed929d"
    assert sa["ended_at"] is None
    assert sa["activity"].startswith("Bash")


def test_interactive_fixture_permission_and_idle(conn, host, ingestor):
    raws = load_fixture("hook-payloads-2.1.330-interactive.jsonl")
    sid = raws[0]["session_id"]
    states = []
    for i, raw in enumerate(raws):
        ingestor.apply(host["id"], to_event(raw, 1000 + i, tmux_pane="%0"), received_at=1000 + i)
        states.append((raw["hook_event_name"], raw.get("notification_type"), session(conn, sid)["state"]))
    assert ("Notification", "permission_prompt", "needs_you") in states
    after_permission = states[states.index(("Notification", "permission_prompt", "needs_you")) + 1]
    assert after_permission == ("PostToolUse", None, "working")
    assert ("Notification", "idle_prompt", "idle") in states
    s = session(conn, sid)
    assert s["state"] == "ended"
    assert s["model"] == "claude-haiku-5-5"
    assert s["subagents"] == []  # the internal helper agent (empty type) is ignored


def test_out_of_order_events_do_not_regress_state(conn, host, ingestor):
    sid = "s-ooo"
    ingestor.apply(host["id"], {"event": "SessionStart", "session_id": sid, "ts": 1, "source": "startup"}, 1)
    ingestor.apply(host["id"], {"event": "Stop", "session_id": sid, "ts": 5}, 2)
    # PreToolUse fired before Stop but arrived after it
    ingestor.apply(host["id"], {"event": "PreToolUse", "session_id": sid, "ts": 4, "tool_name": "Bash"}, 3)
    s = session(conn, sid)
    assert s["state"] == "idle"
    assert conn.execute("SELECT COUNT(*) FROM events WHERE session_id = ?", (sid,)).fetchone()[0] == 3


def test_new_session_in_same_pane_replaces_old(conn, host, ingestor):
    ingestor.apply(host["id"], {"event": "SessionStart", "session_id": "old", "ts": 1, "source": "startup",
                                "tmux_pane": "%3"}, 1)
    res = ingestor.apply(host["id"], {"event": "SessionStart", "session_id": "new", "ts": 2, "source": "clear",
                                      "tmux_pane": "%3"}, 2)
    assert res.sessions == {"new", "old"}
    old = session(conn, "old")
    assert old["state"] == "ended" and old["end_reason"] == "replaced" and old["replaced_by"] == "new"
    assert session(conn, "new")["state"] == "idle"


def test_resume_revives_ended_session(conn, host, ingestor):
    ingestor.apply(host["id"], {"event": "SessionStart", "session_id": "s", "ts": 1, "source": "startup"}, 1)
    ingestor.apply(host["id"], {"event": "SessionEnd", "session_id": "s", "ts": 2, "reason": "prompt_input_exit"}, 2)
    assert session(conn, "s")["state"] == "ended"
    ingestor.apply(host["id"], {"event": "SessionStart", "session_id": "s", "ts": 3, "source": "resume"}, 3)
    s = session(conn, "s")
    assert s["state"] == "idle" and s["ended_at"] is None


def test_unknown_session_is_created_on_first_event(conn, host, ingestor):
    ingestor.apply(host["id"], {"event": "UserPromptSubmit", "session_id": "late", "ts": 1}, 1)
    s = session(conn, "late")
    assert s["state"] == "working" and s["source"] == "observed"


def test_events_from_other_host_do_not_change_state(conn, host, ingestor):
    from d4cc_server import db
    other = db.host_for_token(conn, db.create_host(conn, "ws2"))["id"]
    ingestor.apply(host["id"], {"event": "SessionStart", "session_id": "s", "ts": 1}, 1)
    ingestor.apply(other, {"event": "UserPromptSubmit", "session_id": "s", "ts": 2}, 2)
    assert session(conn, "s")["state"] == "idle"


def test_background_agent_survives_its_tool_call(conn, host, ingestor):
    sid = "bg"
    h = host["id"]
    ingestor.apply(h, {"event": "PreToolUse", "session_id": sid, "ts": 1, "tool_name": "Agent",
                       "tool_use_id": "t1", "tool_summary": "long job", "subagent_type": "general-purpose"}, 1)
    ingestor.apply(h, {"event": "SubagentStart", "session_id": sid, "ts": 2, "agent_id": "a1",
                       "agent_type": "general-purpose"}, 2)
    ingestor.apply(h, {"event": "PostToolUse", "session_id": sid, "ts": 3, "tool_name": "Agent", "tool_use_id": "t1"}, 3)
    sa = session(conn, sid)["subagents"][0]
    assert sa["description"] == "long job" and sa["ended_at"] is None
    ingestor.apply(h, {"event": "SubagentStop", "session_id": sid, "ts": 9, "agent_id": "a1",
                       "agent_type": "general-purpose"}, 9)
    assert session(conn, sid)["subagents"][0]["ended_at"] == 9


@pytest.mark.parametrize("bad", [
    {"event": "Bogus", "session_id": "x"},
    {"event": "Stop"},
    {"event": "Stop", "session_id": ""},
    "not-an-object",
])
def test_invalid_events_rejected(ingestor, host, bad):
    with pytest.raises(InvalidEvent):
        ingestor.apply(host["id"], bad, 1)


AGENT_PRE = {"event": "PreToolUse", "tool_name": "Agent", "tool_use_id": "t1",
             "tool_summary": "Write tmux haiku", "subagent_type": "general-purpose"}
AGENT_POST = {"event": "PostToolUse", "tool_name": "Agent", "tool_use_id": "t1"}
SA_START = {"event": "SubagentStart", "agent_id": "a1", "agent_type": "general-purpose"}


@pytest.mark.parametrize("order", [
    ("pre", "start", "post"),   # client order (as captured live, background agent)
    ("pre", "post", "start"),   # PostToolUse overtook SubagentStart (seen live)
    ("start", "pre", "post"),   # SubagentStart overtook PreToolUse
    ("post", "start", "pre"),
])
def test_agent_pairing_is_order_independent(conn, host, ingestor, order):
    msgs = {"pre": (AGENT_PRE, 1.000), "start": (SA_START, 1.045), "post": (AGENT_POST, 1.046)}
    for i, key in enumerate(order):
        ev, ts = msgs[key]
        ingestor.apply(host["id"], {**ev, "session_id": "s", "ts": ts}, received_at=100 + i * 0.01)
    subs = session(conn, "s")["subagents"]
    assert len(subs) == 1
    assert subs[0]["agent_id"] == "a1"
    assert subs[0]["description"] == "Write tmux haiku"
    assert subs[0]["ended_at"] is None


def test_agent_call_without_subagent_closed_on_stop(conn, host, ingestor):
    ingestor.apply(host["id"], {**AGENT_PRE, "session_id": "s", "ts": 1}, received_at=100)
    ingestor.apply(host["id"], {**AGENT_POST, "session_id": "s", "ts": 2}, received_at=101)
    ingestor.apply(host["id"], {"event": "Stop", "session_id": "s", "ts": 30}, received_at=130)
    assert session(conn, "s")["subagents"][0]["ended_at"] == 130
