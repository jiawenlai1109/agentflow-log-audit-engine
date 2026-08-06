from agentflow.core.messages import AgentMessage


def test_roundtrip_to_dict():
    msg = AgentMessage(
        run_id="run_1",
        sender="planner",
        receiver="executor",
        kind="task_list",
        content="{}",
        artifacts=["plan.json"],
    )
    restored = AgentMessage.from_dict(msg.to_dict())
    assert restored == msg
