"""Exact model-step snapshots; only human labels enter the training database."""

from __future__ import annotations

import time
from collections import Counter
from contextvars import ContextVar
from pathlib import Path
from uuid import uuid4

from ..models.action import build_action_schema
from ..models.parse import validate
from ..tools import schemas
from .files import atomic_write, encode, read_json
from .scrub import Scrubber

CURRENT_RUN = ContextVar("training_run", default=None)
PENDING_DAYS = 7


def validate_action(action: dict) -> dict:
    error = validate(build_action_schema(schemas()), action)
    if error:
        raise ValueError("The correction must be a valid reply or tool action: " + error)
    if len(encode(action).encode()) > 64 * 1024:
        raise ValueError("Correction action is too large")
    return action


class Capture:
    def __init__(self, agent):
        self.agent = agent
        self.memory = agent.memory
        self.root = Path(agent.config.training_data_dir or agent.config.data_dir / "training-data")
        self.scrubber = Scrubber(
            [
                getattr(agent.config, name)
                for name, field in agent.config.__dataclass_fields__.items()
                if field.metadata.get("secret")
            ],
            agent.config.training_keep_emails,
        )
        self.pending: dict[str, list[dict]] = {}
        self.suppressed: set[str] = set()

    def enabled(self, chat_id: int | None) -> bool:
        if chat_id is None or self.blocked():
            return False
        rows = self.memory._all("SELECT training_mode FROM chats WHERE id=?", (chat_id,))
        return bool(rows and rows[0][0] != "never" and self.memory.get_meta("training_capture") == "1")

    def blocked(self) -> bool:
        # Include watch sessions, and use durable records even if browserd is down.
        return bool(self.memory.active_signin_requests() or self.memory.screen_sessions())

    def record(self, run, messages: list[dict], tools: list[dict], action: dict, *, raw_action=None) -> None:
        if not run or not self.enabled(run.chat_id) or run.run_id in self.suppressed:
            return
        if action.get("tool") == "request_signin":
            self.suppressed.add(run.run_id)
            self.pending.pop(run.run_id, None)
            return
        from ..models.modelreg import load_model_info

        info = load_model_info(self.agent.config.model_provider)
        steps = self.pending.setdefault(run.run_id, [])
        data, counts = self.scrubber.scrub(
            {
                "run_id": run.run_id,
                "chat_id": run.chat_id,
                "step": len(steps),
                "messages": messages,
                "tools": tools,
                "output": action,
                "outcome": {"raw_action": raw_action if raw_action is not None else encode(action)},
                "model_version_id": info.version_id if info else self.agent.config.model_name,
                "prompt_version_id": run.prompt_version_id,
                "tainted": bool(run.tainted),
                "created": time.time(),
            }
        )
        if len(encode(data).encode()) <= 256 * 1024 and len(steps) < 32:
            data["scrub_counts"] = counts
            steps.append(data)

    def persist(self, step: dict, source: str, *, target=None, approval_id=None, message_id=None) -> str:
        if not self.enabled(step["chat_id"]):
            raise ValueError("Capture is disabled for this chat or screen session")
        clean, counts = self.scrubber.scrub(step)
        self.scrubber.check(clean)
        clean_target, more = self.scrubber.scrub(target)
        totals = Counter(step.get("scrub_counts", {})) + Counter(counts) + Counter(more)
        identifier = "ex_" + uuid4().hex
        include = int(not clean["tainted"])
        self.memory._exec(
            "INSERT INTO training_examples(id,chat_id,run_id,step,source,messages_json,tools_json,output_json,"
            "target_json,approval_id,tainted,include,model_version_id,prompt_version_id,created,message_id,"
            "scrub_counts_json,outcome_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                identifier,
                clean["chat_id"],
                clean["run_id"],
                clean["step"],
                source,
                encode(clean["messages"]),
                encode(clean["tools"]),
                encode(clean["output"]),
                encode(clean_target) if target is not None else None,
                approval_id,
                int(clean["tainted"]),
                include,
                clean["model_version_id"],
                clean["prompt_version_id"],
                clean["created"],
                message_id,
                encode(totals),
                encode(clean.get("outcome", {})),
            ),
        )
        if target is not None and source in {"correction", "rejected_call"}:
            self.memory._exec(
                "INSERT INTO preference_pairs(id,example_id,source,chosen_json,rejected_json,include,created) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    "pp_" + uuid4().hex,
                    identifier,
                    "correction" if source == "correction" else "gate_reject_with_alternative",
                    encode(clean_target),
                    encode(clean["output"]),
                    include,
                    time.time(),
                ),
            )
        return identifier

    def gate_label(self, approval: dict, alternative=None) -> None:
        if not self.enabled(approval["chat_id"]):
            return
        steps = self.pending.get(approval["run_id"], [])
        if not steps:
            return
        source = "approved_call" if approval["status"] == "approved" else "rejected_call"
        target = steps[-1]["output"] if source == "approved_call" else alternative
        steps[-1].setdefault("outcome", {})["gate"] = approval["status"]
        self.persist(steps[-1], source, target=target, approval_id=approval["id"])

    def tool_result(self, run, result: str) -> None:
        import hashlib

        steps = self.pending.get(run.run_id, []) if run else []
        if not steps or not self.enabled(run.chat_id):
            return
        clean, counts = self.scrubber.scrub(result)
        steps[-1]["scrub_counts"] = dict(Counter(steps[-1].get("scrub_counts", {})) + Counter(counts))
        steps[-1].setdefault("outcome", {}).update(
            result_sha256=hashlib.sha256(clean.encode()).hexdigest(),
            success=not result.startswith("Error:"),
        )
        self.memory._exec(
            "UPDATE training_examples SET outcome_json=? WHERE run_id=? AND step=?",
            (encode(steps[-1]["outcome"]), run.run_id, steps[-1]["step"]),
        )

    def finish(self, run, message_id: int | None) -> None:
        steps = self.pending.pop(run.run_id, [])
        excluded = run.run_id in self.suppressed
        self.suppressed.discard(run.run_id)
        if not message_id or excluded or not self.enabled(run.chat_id) or not steps:
            return
        # Feedback on the visible reply refers to its last model step, with all actual context.
        steps[-1]["message_id"] = message_id
        self.memory._exec(
            "UPDATE training_examples SET message_id=? WHERE run_id=?", (message_id, run.run_id)
        )
        atomic_write(
            self.root / "pending" / f"{message_id}.json",
            encode({**steps[-1], "unlabelled_steps": steps[:-1]}),
        )

    def find_pending(self, message_id: int) -> dict | None:
        path = self.root / "pending" / f"{message_id}.json"
        if not path.exists():
            return None
        if time.time() - path.stat().st_mtime > PENDING_DAYS * 86400:
            path.unlink()
            return None
        return read_json(path, 8 * 1024 * 1024)

    def prune(self) -> None:
        for path in (self.root / "pending").glob("*.json"):
            if not path.is_symlink() and time.time() - path.stat().st_mtime > PENDING_DAYS * 86400:
                path.unlink()

    def purge_chat(self, chat_id: int) -> None:
        for path in (self.root / "pending").glob("*.json"):
            if read_json(path, 8 * 1024 * 1024).get("chat_id") == chat_id:
                path.unlink()

    def feedback(self, message_id: int, rating: int, correction=None, correction_action=None) -> dict:
        if rating not in {-1, 1} or isinstance(rating, bool):
            raise ValueError("Rating must be -1 or 1")
        if correction is not None and (not isinstance(correction, str) or len(correction) > 8000):
            raise ValueError("Correction must be at most 8000 characters")
        if correction and correction_action:
            raise ValueError("Use a text correction or a tool correction")
        action = validate_action(correction_action) if correction_action is not None else None
        if correction:
            action = {"action": "reply", "text": correction}
        rows = self.memory._all("SELECT chat_id,role,kind FROM messages WHERE id=?", (message_id,))
        if not rows or rows[0]["role"] != "assistant" or rows[0]["kind"] != "text":
            raise KeyError(message_id)
        chat_id = rows[0]["chat_id"]
        with self.memory.transaction():
            self.check_feedback_unlocked(message_id)
            # A changed vote replaces the previous unused labelled example and its pair.
            self.memory._exec(
                "DELETE FROM training_examples WHERE message_id=? AND source IN "
                "('thumbs_up','thumbs_down','correction')",
                (message_id,),
            )
            now = time.time()
            self.memory._exec(
                "INSERT INTO feedback(chat_id,message_id,rating,correction,correction_action,created,updated) "
                "VALUES (?,?,?,?,?,?,?) ON CONFLICT(message_id) DO UPDATE SET rating=excluded.rating,"
                "correction=excluded.correction,correction_action=excluded.correction_action,updated=excluded.updated",
                (
                    chat_id,
                    message_id,
                    rating,
                    correction,
                    encode(action) if correction_action else None,
                    now,
                    now,
                ),
            )
            step = self.find_pending(message_id) if self.enabled(chat_id) else None
            if step:
                self.persist(
                    step,
                    "correction" if action else "thumbs_up" if rating == 1 else "thumbs_down",
                    target=action or (step["output"] if rating == 1 else None),
                    message_id=message_id,
                )
            self.agent.audit.write(
                "roland",
                "feedback_given",
                chat_id=chat_id,
                detail={"message_id": message_id, "rating": rating, "captured": bool(step)},
            )
        return {"message_id": message_id, "rating": rating, "captured": bool(step), "locked": False}

    def saved_feedback(self, message_id: int) -> dict | None:
        """The stored vote for a reply, and whether it also became a training example.

        The page needs `captured` after a reload: the vote alone does not say it.
        """
        rows = self.memory._all(
            "SELECT f.*, EXISTS(SELECT 1 FROM training_examples e WHERE e.message_id=f.message_id "
            "AND e.source IN ('thumbs_up','thumbs_down','correction')) AS captured "
            "FROM feedback f WHERE f.message_id=?",
            (message_id,),
        )
        return {**dict(rows[0]), "captured": bool(rows[0]["captured"])} if rows else None

    def check_feedback_unlocked(self, message_id: int) -> None:
        if self.memory._all(
            "SELECT 1 FROM feedback WHERE message_id=? AND used_in_dataset IS NOT NULL "
            "UNION ALL SELECT 1 FROM training_examples WHERE message_id=? AND used_in_dataset IS NOT NULL",
            (message_id, message_id),
        ):
            raise LookupError("Feedback has already been used in a dataset")
