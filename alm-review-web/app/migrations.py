from sqlalchemy import Engine, inspect, text


def ensure_compatible_schema(engine: Engine) -> None:
    inspector = inspect(engine)
    table_names = inspector.get_table_names()
    with engine.begin() as connection:
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
            columns = {
                column["name"] for column in inspector.get_columns("evidence_configs")
            }
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