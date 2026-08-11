from sqlalchemy import Engine, inspect, text


def ensure_compatible_schema(engine: Engine) -> None:
    with engine.begin() as connection:
        inspector = inspect(connection)
        table_names = inspector.get_table_names()
        if "workspaces" not in table_names:
            boolean_type = "BOOLEAN" if engine.dialect.name != "mysql" else "TINYINT(1)"
            identity = (
                "INTEGER PRIMARY KEY AUTOINCREMENT"
                if engine.dialect.name == "sqlite"
                else "INTEGER PRIMARY KEY AUTO_INCREMENT"
            )
            connection.execute(
                text(
                    "CREATE TABLE workspaces ("
                    f"id {identity}, "
                    "name VARCHAR(255) NOT NULL UNIQUE, "
                    "slug VARCHAR(128) NOT NULL UNIQUE, "
                    f"equipment_review_enabled {boolean_type} NOT NULL DEFAULT 1, "
                    "equipment_area_filter VARCHAR(255) NOT NULL DEFAULT '', "
                    f"legacy_policy_adopted {boolean_type} NOT NULL DEFAULT 0, "
                    f"archived {boolean_type} NOT NULL DEFAULT 0, "
                    "created_at DATETIME NOT NULL, "
                    "updated_at DATETIME NULL"
                    ")"
                )
            )
        workspace_columns = {
            column["name"] for column in inspect(connection).get_columns("workspaces")
        }
        if "legacy_policy_adopted" not in workspace_columns:
            connection.execute(
                text(
                    "ALTER TABLE workspaces ADD COLUMN legacy_policy_adopted "
                    "BOOLEAN NOT NULL DEFAULT 0"
                )
            )
        default_workspace_id = connection.execute(
            text("SELECT id FROM workspaces ORDER BY id LIMIT 1")
        ).scalar()
        if default_workspace_id is None:
            connection.execute(
                text(
                    "INSERT INTO workspaces "
                    "(name, slug, equipment_review_enabled, equipment_area_filter, "
                    "legacy_policy_adopted, archived, created_at, updated_at) "
                    "VALUES ('Testing', 'testing', 1, '', 0, 0, CURRENT_TIMESTAMP, "
                    "CURRENT_TIMESTAMP)"
                )
            )
            default_workspace_id = connection.execute(
                text("SELECT id FROM workspaces ORDER BY id LIMIT 1")
            ).scalar_one()

        if "alm_runs" in table_names:
            columns = {column["name"] for column in inspector.get_columns("alm_runs")}
            if "workspace_id" not in columns:
                connection.execute(
                    text("ALTER TABLE alm_runs ADD COLUMN workspace_id INTEGER NULL")
                )
            if "alm_run_id" not in columns:
                connection.execute(text("ALTER TABLE alm_runs ADD COLUMN alm_run_id BIGINT NULL"))
            connection.execute(
                text(
                    "UPDATE alm_runs SET workspace_id = :workspace_id "
                    "WHERE workspace_id IS NULL"
                ),
                {"workspace_id": default_workspace_id},
            )
            connection.execute(
                text("UPDATE alm_runs SET alm_run_id = run_id WHERE alm_run_id IS NULL")
            )
            indexes = {index["name"] for index in inspector.get_indexes("alm_runs")}
            if "ix_alm_runs_workspace_id" not in indexes:
                connection.execute(
                    text(
                        "CREATE INDEX ix_alm_runs_workspace_id "
                        "ON alm_runs (workspace_id)"
                    )
                )
            if "ix_alm_runs_alm_run_id" not in indexes:
                connection.execute(
                    text(
                        "CREATE INDEX ix_alm_runs_alm_run_id "
                        "ON alm_runs (alm_run_id)"
                    )
                )
            if "uq_workspace_alm_run" not in indexes:
                connection.execute(
                    text(
                        "CREATE UNIQUE INDEX uq_workspace_alm_run "
                        "ON alm_runs (workspace_id, alm_run_id)"
                    )
                )

        for table_name in (
            "sync_configs",
            "evidence_configs",
            "sync_jobs",
            "review_jobs",
            "review_results",
            "manual_decisions",
            "sync_history",
        ):
            if table_name not in table_names:
                continue
            columns = {column["name"] for column in inspector.get_columns(table_name)}
            if "workspace_id" not in columns:
                connection.execute(
                    text(f"ALTER TABLE {table_name} ADD COLUMN workspace_id INTEGER NULL")
                )
            connection.execute(
                text(
                    f"UPDATE {table_name} SET workspace_id = :workspace_id "
                    "WHERE workspace_id IS NULL"
                ),
                {"workspace_id": default_workspace_id},
            )
            indexes = {index["name"] for index in inspector.get_indexes(table_name)}
            index_name = f"ix_{table_name}_workspace_id"
            if index_name not in indexes:
                connection.execute(
                    text(f"CREATE INDEX {index_name} ON {table_name} (workspace_id)")
                )

        if "review_jobs" in table_names:
            columns = {column["name"] for column in inspector.get_columns("review_jobs")}
            if "batch_id" not in columns:
                connection.execute(
                    text(
                        "ALTER TABLE review_jobs ADD COLUMN batch_id "
                        "VARCHAR(36) NULL"
                    )
                )
            if "claimed_by" not in columns:
                connection.execute(
                    text(
                        "ALTER TABLE review_jobs ADD COLUMN claimed_by "
                        "VARCHAR(255) NULL"
                    )
                )
            if "lease_expires_at" not in columns:
                connection.execute(
                    text(
                        "ALTER TABLE review_jobs ADD COLUMN lease_expires_at "
                        "DATETIME NULL"
                    )
                )
            indexes = {index["name"] for index in inspector.get_indexes("review_jobs")}
            if "ix_review_jobs_batch_id" not in indexes:
                connection.execute(
                    text(
                        "CREATE INDEX ix_review_jobs_batch_id "
                        "ON review_jobs (batch_id)"
                    )
                )
            if "ix_review_jobs_claimed_by" not in indexes:
                connection.execute(
                    text(
                        "CREATE INDEX ix_review_jobs_claimed_by "
                        "ON review_jobs (claimed_by)"
                    )
                )
            if "ix_review_jobs_lease_expires_at" not in indexes:
                connection.execute(
                    text(
                        "CREATE INDEX ix_review_jobs_lease_expires_at "
                        "ON review_jobs (lease_expires_at)"
                    )
                )

        if "sync_jobs" in table_names:
            columns = {column["name"] for column in inspector.get_columns("sync_jobs")}
            if "active_key" not in columns:
                connection.execute(
                    text(
                        "ALTER TABLE sync_jobs ADD COLUMN active_key "
                        "VARCHAR(64) NULL"
                    )
                )
            sync_progress_columns = {
                "progress_stage": "VARCHAR(32) NOT NULL DEFAULT 'queued'",
                "progress_message": "VARCHAR(1500) NOT NULL DEFAULT ''",
                "folders_discovered": "INTEGER NOT NULL DEFAULT 0",
                "folders_processed": "INTEGER NOT NULL DEFAULT 0",
                "test_sets_discovered": "INTEGER NOT NULL DEFAULT 0",
                "runs_discovered": "INTEGER NOT NULL DEFAULT 0",
            }
            for column_name, column_definition in sync_progress_columns.items():
                if column_name not in columns:
                    connection.execute(
                        text(
                            f"ALTER TABLE sync_jobs ADD COLUMN {column_name} "
                            f"{column_definition}"
                        )
                    )
            unique_constraints = inspector.get_unique_constraints("sync_jobs")
            indexes = inspector.get_indexes("sync_jobs")
            active_key_is_unique = any(
                constraint.get("column_names") == ["active_key"]
                for constraint in unique_constraints
            ) or any(
                index.get("unique") and index.get("column_names") == ["active_key"]
                for index in indexes
            )
            if not active_key_is_unique:
                connection.execute(
                    text(
                        "CREATE UNIQUE INDEX uq_sync_jobs_active_key "
                        "ON sync_jobs (active_key)"
                    )
                )

        if "equipment_registry" in table_names:
            columns = {
                column["name"]: column
                for column in inspector.get_columns("equipment_registry")
            }
            serial_column = columns.get("serial_number")
            serial_length = getattr(
                serial_column.get("type") if serial_column else None,
                "length",
                None,
            )
            if engine.dialect.name == "mysql" and serial_length and serial_length > 255:
                connection.execute(
                    text(
                        "ALTER TABLE equipment_registry MODIFY COLUMN serial_number "
                        "VARCHAR(255) NOT NULL DEFAULT ''"
                    )
                )
            indexes = {
                index["name"] for index in inspector.get_indexes("equipment_registry")
            }
            if "ix_equipment_registry_serial_number" not in indexes:
                connection.execute(
                    text(
                        "CREATE INDEX ix_equipment_registry_serial_number "
                        "ON equipment_registry (serial_number)"
                    )
                )

        if "review_results" in table_names:
            columns = {
                column["name"] for column in inspector.get_columns("review_results")
            }
            column_type = "LONGTEXT" if engine.dialect.name == "mysql" else "TEXT"
            required_columns = ("criteria_json", "step_results_json", "warnings_json")
            for column in required_columns:
                if column not in columns:
                    connection.execute(
                        text(
                            f"ALTER TABLE review_results ADD COLUMN {column} "
                            f"{column_type} NULL"
                        )
                    )
            if "review_policy_key" not in columns:
                connection.execute(
                    text(
                        "ALTER TABLE review_results ADD COLUMN review_policy_key "
                        "VARCHAR(64) NOT NULL DEFAULT ''"
                    )
                )

        if "evidence_configs" in table_names:
            evidence_columns = {
                column["name"]: column
                for column in inspector.get_columns("evidence_configs")
            }
            columns = set(evidence_columns)
            id_column = evidence_columns.get("id")
            if (
                engine.dialect.name == "mysql"
                and id_column
                and not id_column.get("autoincrement")
            ):
                connection.execute(
                    text(
                        "ALTER TABLE evidence_configs MODIFY COLUMN id "
                        "INTEGER NOT NULL AUTO_INCREMENT"
                    )
                )
            required_flags = (
                "network_evidence_enabled",
                "image_review_enabled",
                "allow_insecure_image_transport",
            )
            for column in required_flags:
                if column not in columns:
                    connection.execute(
                        text(
                            f"ALTER TABLE evidence_configs ADD COLUMN {column} "
                            "BOOLEAN NOT NULL DEFAULT 0"
                        )
                    )