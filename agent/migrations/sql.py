"""Execute constant schema SQL without executescript's implicit commit."""

import sqlite3


def execute_script(db: sqlite3.Connection, sql: str) -> None:
    statement = ""
    for line in sql.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ""
    if statement.strip():
        raise ValueError("Incomplete migration SQL")
