import json

from sapbot.agent.llm import ToolCall, Turn
from sapbot.agent.loop import AgentLoop


class ScriptedLLM:
    """Plays a fixed list of turns; records tool results it receives."""
    def __init__(self, turns, model="scripted"):
        self.turns = list(turns)
        self.model = model
        self.results = []
        self.messages = []

    def start(self, system, user):
        self.system, self.user = system, user
        self.messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]

    def step(self):
        return self.turns.pop(0) if self.turns else Turn(text="done")

    def add_tool_results(self, results):
        self.results.extend(results)


def tc(name, **args):
    return ToolCall(f"id{name}{len(args)}", name, args)


def va01_turns():
    return [
        Turn(text="open VA01", tool_calls=[tc("sap_act", ok_code="/nVA01", reasoning="start")]),
        Turn(tool_calls=[tc("sap_act", field_updates_json=json.dumps({"wnd[0]/usr/ctxtVBAK-AUART": "OR"}), vkey=0,
                            reasoning="type")]),
        Turn(tool_calls=[tc("sap_act", field_updates_json=json.dumps({"wnd[0]/usr/ctxtKUAGV-KUNNR": "1000"}),
                            vkey=11, reasoning="save")]),
        Turn(tool_calls=[tc("sap_act", ok_code="/nCOR3", reasoning="verify")]),
        Turn(tool_calls=[tc("sap_act", field_updates_json=json.dumps({"wnd[0]/usr/ctxtCAUFVD-AUFNR": "12345"}),
                            vkey=0, reasoning="verify")]),
        Turn(tool_calls=[tc("sap_observe_screen", reasoning="read back")]),
        Turn(text="Created standard order 12345 and verified it."),
    ]


def test_loop_gold_run(ex):
    ex.gov.store.set_flag("claude_api_mode", True)
    events = []
    llm = ScriptedLLM(va01_turns())
    res = AgentLoop(ex, on_event=events.append, llm_factory=lambda b: llm).run("Create order for customer 1000")
    assert res.outcome == "success" and res.quality == "gold", res
    assert any(e["type"] == "tool_result" for e in events) and events[-1]["type"] == "final"
    assert ex.channel == "cowork"          # channel restored after the run


def test_loop_feature_off(ex):
    import pytest
    with pytest.raises(PermissionError):
        AgentLoop(ex, llm_factory=lambda b: ScriptedLLM([])).run("x")


def test_loop_turn_budget(ex):
    from sapbot import config
    ex.gov.store.set_flag("claude_api_mode", True)
    ex.s.agent.max_turns = 3
    turns = [Turn(tool_calls=[tc("sap_observe_screen", reasoning=f"look {i}")]) for i in range(10)]
    res = AgentLoop(ex, llm_factory=lambda b: ScriptedLLM(turns)).run("look around")
    assert res.needs_human and "turn budget" in res.summary and res.quality == "failed"


def test_loop_pauses_on_policy_refusal(ex, approvals):
    ex.gov.store.set_flag("claude_api_mode", True)
    approvals.answer = False
    turns = [Turn(tool_calls=[tc("sap_act", ok_code="/nSE14", reasoning="db utility")]),
             Turn(text="should not get here")]
    res = AgentLoop(ex, llm_factory=lambda b: ScriptedLLM(turns)).run("adjust table")
    assert res.needs_human and res.outcome == "failure"


def test_escalation_records_dpo(ex, home):
    ex.gov.store.set_flag("claude_api_mode", True)
    ex.gov.store.set_flag("local_model", True)
    local = ScriptedLLM([Turn(tool_calls=[tc("sap_act", click_id="wnd[0]/usr/btnNOPE", reasoning="guess")])],
                        model="qwen")
    claude = ScriptedLLM([Turn(tool_calls=[tc("sap_observe_screen", reasoning="look first")]),
                          Turn(text="Nothing to do.")], model="claude")
    res = AgentLoop(ex, force_brain="local", llm_factory=lambda b: local if b == "local" else claude).run("do x")
    assert res.escalated and res.outcome == "success"
    from sapbot import workspaces
    pairs = (workspaces.load("acme").trajectories_dir / "dpo_pairs.jsonl").read_text().splitlines()
    p = json.loads(pairs[0])
    assert p["rejected"]["name"] == "sap_act" and p["chosen"]["name"] == "sap_observe_screen"
