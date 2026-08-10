from __future__ import annotations

import io
import zipfile
from datetime import date

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import EquipmentImportHistory, EquipmentRegistry
from app.services.equipment_registry import import_equipment_workbook


def workbook(rows: list[list[str | int]]) -> bytes:
    shared: list[str] = []
    indexes: dict[str, int] = {}

    def shared_index(value: str) -> int:
        if value not in indexes:
            indexes[value] = len(shared)
            shared.append(value)
        return indexes[value]

    row_xml = []
    for row_number, values in enumerate(rows, start=1):
        cells = []
        for column_number, value in enumerate(values, start=1):
            column = chr(ord("A") + column_number - 1)
            if isinstance(value, str):
                cells.append(
                    f'<c r="{column}{row_number}" t="s"><v>{shared_index(value)}</v></c>'
                )
            else:
                cells.append(f'<c r="{column}{row_number}"><v>{value}</v></c>')
        row_xml.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    shared_xml = "".join(f"<si><t>{value}</t></si>" for value in shared)
    files = {
        "xl/workbook.xml": (
            '<?xml version="1.0"?><workbook '
            'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Master Calibration List" sheetId="1" r:id="rId1"/>'
            "</sheets></workbook>"
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0"?><Relationships '
            'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/></Relationships>'
        ),
        "xl/worksheets/sheet1.xml": (
            '<?xml version="1.0"?><worksheet '
            'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(row_xml)}</sheetData></worksheet>'
        ),
        "xl/sharedStrings.xml": (
            '<?xml version="1.0"?><sst '
            'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"{shared_xml}</sst>"
        ),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, value)
    return output.getvalue()


def test_import_inserts_then_updates_by_equipment_id() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    headers = [
        "No / 序号",
        "Equipment Identification Number / 设备识别编号",
        "Equipment Name / 设备名称",
        "Equipment Serial Number / 设备序列号",
        "Calibration Date / 校准日期",
        "Calibration Due Date / 校准到期日",
        "Equipment Status / 设备状态",
    ]
    first = workbook(
        [headers, [1, "PHSZ-RD-VV-1-0064", "秒表", "746356", 46117, 46482, "In Use"]]
    )
    changed = workbook(
        [headers, [1, "phsz-rd-vv-1-0064", "Stopwatch", "746356", 46117, 46482, "In Use"]]
    )

    with Session(engine) as db:
        initial = import_equipment_workbook(db, "first.xlsx", first)
        repeated = import_equipment_workbook(db, "first.xlsx", first)
        modified = import_equipment_workbook(db, "changed.xlsx", changed)

        equipment = db.scalar(select(EquipmentRegistry))
        histories = db.scalars(select(EquipmentImportHistory)).all()

    assert (initial.inserted, initial.updated, initial.unchanged) == (1, 0, 0)
    assert (repeated.inserted, repeated.updated, repeated.unchanged) == (0, 0, 1)
    assert (modified.inserted, modified.updated, modified.unchanged) == (0, 1, 0)
    assert equipment is not None
    assert equipment.equipment_id == "PHSZ-RD-VV-1-0064"
    assert equipment.description == "Stopwatch"
    assert equipment.calibration_date == date(2026, 4, 5)
    assert equipment.calibration_due_date == date(2027, 4, 5)
    assert len(histories) == 3


def test_import_does_not_clear_columns_missing_from_a_workbook() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    full = workbook(
        [
            [
                "Equipment Identification Number",
                "Equipment Description",
                "Equipment Manufacturer",
            ],
            ["EQ-1", "Meter", "Fluke"],
        ]
    )
    reduced = workbook(
        [
            ["Equipment Identification Number", "Equipment Name"],
            ["EQ-1", "Digital Meter"],
        ]
    )

    with Session(engine) as db:
        import_equipment_workbook(db, "full.xlsx", full)
        import_equipment_workbook(db, "reduced.xlsx", reduced)
        equipment = db.scalar(select(EquipmentRegistry))

    assert equipment is not None
    assert equipment.description == "Digital Meter"
    assert equipment.manufacturer == "Fluke"