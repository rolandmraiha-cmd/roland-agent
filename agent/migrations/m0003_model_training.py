"""Persona, feedback and opt-in training records (spec §7.4)."""

import os
import sqlite3
import time

from .sql import execute_script

SCHEMA = """
CREATE TABLE prompt_versions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 agent_name TEXT NOT NULL CHECK(length(agent_name) BETWEEN 1 AND 60),
 persona TEXT NOT NULL CHECK(length(persona) <= 2000),
 instructions TEXT NOT NULL CHECK(length(instructions) <= 4000),
 token_count INTEGER, created REAL NOT NULL, active INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX prompt_versions_one_active ON prompt_versions(active) WHERE active=1;
CREATE TABLE feedback (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
 message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
 rating INTEGER NOT NULL CHECK(rating IN (-1,1)),
 correction TEXT CHECK(length(correction) <= 8000), correction_action TEXT,
 created REAL NOT NULL, updated REAL NOT NULL, used_in_dataset TEXT,
 UNIQUE(message_id)
);
CREATE TABLE training_examples (
 id TEXT PRIMARY KEY, chat_id INTEGER REFERENCES chats(id) ON DELETE CASCADE,
 job_id INTEGER, run_id TEXT NOT NULL, step INTEGER NOT NULL,
 source TEXT NOT NULL CHECK(source IN ('thumbs_up','thumbs_down','correction','approved_call','rejected_call')),
 messages_json TEXT NOT NULL, tools_json TEXT NOT NULL, output_json TEXT NOT NULL,
 target_json TEXT, approval_id TEXT, tainted INTEGER NOT NULL DEFAULT 0,
 include INTEGER NOT NULL DEFAULT 1, model_version_id TEXT NOT NULL,
 prompt_version_id INTEGER NOT NULL, created REAL NOT NULL, used_in_dataset TEXT,
 message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
 scrub_counts_json TEXT NOT NULL DEFAULT '{}', outcome_json TEXT NOT NULL DEFAULT '{}', UNIQUE(run_id,step,source)
);
CREATE INDEX training_examples_new ON training_examples(used_in_dataset,include,created);
CREATE TABLE preference_pairs (
 id TEXT PRIMARY KEY, example_id TEXT NOT NULL REFERENCES training_examples(id) ON DELETE CASCADE,
 source TEXT NOT NULL CHECK(source IN ('correction','gate_reject_with_alternative')),
 chosen_json TEXT NOT NULL, rejected_json TEXT NOT NULL,
 include INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL, used_in_dataset TEXT
);
CREATE TABLE training_datasets (
 id TEXT PRIMARY KEY, created REAL NOT NULL,
 n_sft INTEGER NOT NULL,n_dpo INTEGER NOT NULL,n_eval INTEGER NOT NULL,n_seed INTEGER NOT NULL,
 manifest_sha256 TEXT NOT NULL,scrub_counts_json TEXT NOT NULL
);
CREATE TABLE training_runs (
 id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES training_datasets(id),
 mode TEXT NOT NULL CHECK(mode IN ('manual','ssh','hook')),
 status TEXT NOT NULL CHECK(status IN ('waiting_manual','launching','running','importing','evaluated',
 'rejected_auto','awaiting_roland','promoted','discarded','failed','cancelled')),
 candidate_version_id TEXT,eval_summary_json TEXT,error TEXT,created REAL NOT NULL,finished REAL
);
CREATE UNIQUE INDEX training_one_active ON training_runs((1))
 WHERE status IN ('launching','running','importing');
CREATE TABLE model_promotions (
 id TEXT PRIMARY KEY,run_id TEXT REFERENCES training_runs(id),version_id TEXT NOT NULL,
 from_version_id TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','promoting','promoted','discarded','rolled_back_auto','expired')),
 created REAL NOT NULL,decided REAL,decided_by TEXT,confirm_started REAL
);
ALTER TABLE chats ADD COLUMN training_mode TEXT NOT NULL DEFAULT 'follow'
 CHECK(training_mode IN ('follow','on','never'));
INSERT INTO meta(key,value) VALUES ('training_capture','0'),('training_loop_enabled','0'),('last_dataset_id','');
"""


def apply(db: sqlite3.Connection) -> None:
    execute_script(db, SCHEMA)
    from ..persona import DEFAULT_PERSONA

    db.execute(
        "INSERT INTO prompt_versions(agent_name,persona,instructions,created,active) VALUES (?,?,?, ?,1)",
        (os.environ.get("AGENT_NAME", "Agent")[:60] or "Agent", DEFAULT_PERSONA, "", time.time()),
    )
