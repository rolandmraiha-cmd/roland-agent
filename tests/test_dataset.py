"""A8.4 export checks operate on real labelled rows and on-disk downloads."""

import json

import pytest

from agent.training.dataset import Datasets, NotEnoughData


def labelled(agent, n=80):
    agent.memory.set_meta("training_capture", "1")
    chat = agent.memory.new_chat()
    for index in range(n):
        step = {
            "run_id": f"run-{index}",
            "step": 0,
            "chat_id": chat,
            "messages": [
                {"role": "system", "content": "Follow the approval gate"},
                {"role": "user", "content": f"Test question {index}"},
            ],
            "tools": [],
            "output": {"action": "reply", "text": f"Test answer {index}"},
            "tainted": False,
            "model_version_id": "test-base",
            "prompt_version_id": 1,
            "created": 1000 + index,
        }
        agent.capture.persist(step, "thumbs_up", target=step["output"])
    return chat


def test_export_format_matches_schema(make_agent):
    agent = make_agent()
    labelled(agent)
    datasets = Datasets(agent)
    manifest = datasets.build()
    path = datasets.root / manifest["id"]
    assert manifest["counts"]["seed"] / manifest["counts"]["sft"] >= 0.3
    assert manifest["counts"]["sft"] >= 50
    records = [json.loads(line) for line in (path / "sft.jsonl").read_text().splitlines()]
    assert all(row["schema"] == 1 and row["messages"][-1]["role"] == "assistant" for row in records)
    assert datasets.archive(manifest["id"]).is_file()


def test_seed_ratio_enforced(make_agent):
    agent = make_agent(training_seed_ratio=0.2)
    labelled(agent)
    with pytest.raises(ValueError, match="at least"):
        Datasets(agent).build()


def test_minimums_block_run(make_agent):
    agent = make_agent()
    labelled(agent, 3)
    with pytest.raises(NotEnoughData):
        Datasets(agent).build()
    assert not agent.memory._all("SELECT * FROM training_datasets")
    assert all(row[0] is None for row in agent.memory._all("SELECT used_in_dataset FROM training_examples"))


def test_private_eval_never_in_train(make_agent):
    agent = make_agent()
    labelled(agent)
    datasets = Datasets(agent)
    result = datasets.build()
    directory = datasets.root / result["id"]
    train = {json.loads(line)["id"] for line in (directory / "sft.jsonl").read_text().splitlines()}
    private = {json.loads(line)["id"] for line in (directory / "eval_private.jsonl").read_text().splitlines()}
    assert private and not train.intersection(private)


def test_only_last_assistant_turn_is_target(make_agent):
    agent = make_agent()
    labelled(agent)
    row = dict(agent.memory._all("SELECT * FROM training_examples LIMIT 1")[0])
    row["messages_json"] = json.dumps(
        [{"role": "assistant", "content": "old context"}, {"role": "user", "content": "new request"}]
    )
    from agent.training.dataset import sft_record

    result = sft_record(row)
    assert result["messages"][0]["content"] == "old context"
    assert result["messages"][-1]["content"] == row["target_json"]


def test_targets_are_valid_actions(make_agent):
    agent = make_agent()
    labelled(agent)
    agent.memory._exec("UPDATE training_examples SET target_json='{}'")
    with pytest.raises(ValueError, match="valid reply"):
        Datasets(agent).build()


def test_export_scrubs_again_and_aborts_on_secret(make_agent, monkeypatch):
    agent = make_agent()
    labelled(agent)
    secret = "synthetic confidential test phrase"
    agent.capture.scrubber.secrets = (secret,)
    agent.memory._exec(
        "UPDATE training_examples SET messages_json=?", (json.dumps([{"role": "user", "content": secret}]),)
    )
    monkeypatch.setattr(agent.capture.scrubber, "scrub", lambda value: (value, {}))
    with pytest.raises(ValueError, match="survived"):
        Datasets(agent).build()
    assert not agent.memory._all("SELECT * FROM training_datasets")
