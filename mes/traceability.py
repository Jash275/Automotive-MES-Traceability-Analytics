"""Discrete-manufacturing MES core: serialization, routing interlocks, part validation,
torque/press validation, non-conformance and rework, genealogy and andon.

All state lives in SQL (see sql/schema.sql) as it would in a FactoryTalk or Ignition
based MES. Station results are written transactionally, so the database is always
the single source of truth for traceability.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


class TraceabilityMES:
    """Core MES traceability and interlock engine."""

    def __init__(
        self,
        route_path: str | Path = ROOT / "config/route_FSM-200.json",
        db: str = ":memory:",
        clock: datetime | None = None,
    ) -> None:
        route_file = Path(route_path)
        self.route: dict[str, Any] = json.loads(route_file.read_text(encoding="utf-8"))
        self.ops: list[str] = [s["op"] for s in self.route["stations"]]
        self.stations: dict[str, dict[str, Any]] = {s["op"]: s for s in self.route["stations"]}
        self.con = sqlite3.connect(db)
        self.con.row_factory = sqlite3.Row
        schema_text = (ROOT / "sql/schema.sql").read_text(encoding="utf-8")
        self.con.executescript(schema_text)
        self.clock: datetime = clock or datetime(2022, 3, 7, 6, 0, 0)
        self._seq: int = 0

    def close(self) -> None:
        """Close underlying database connection."""
        if self.con:
            self.con.close()

    def __enter__(self) -> TraceabilityMES:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    @staticmethod
    def load_queries() -> dict[str, str]:
        """Load and parse named SQL queries from sql/traceability_queries.sql."""
        text = (ROOT / "sql/traceability_queries.sql").read_text(encoding="utf-8")
        parts = re.split(r"^-- name: (\w+)\s*$", text, flags=re.M)
        return {parts[i]: parts[i + 1].strip().rstrip(";") for i in range(1, len(parts), 2)}

    # ----------------------------------------------------------------- utils
    def now(self, advance_s: float = 0) -> str:
        """Advance simulation clock and return ISO-formatted timestamp."""
        self.clock += timedelta(seconds=advance_s)
        return self.clock.isoformat(timespec="seconds")

    def _unit(self, serial: str) -> sqlite3.Row | None:
        """Retrieve unit record by serial number."""
        return self.con.execute("SELECT * FROM unit WHERE serial = ?", (serial,)).fetchone()

    def _passed_ops(self, serial: str) -> set[str]:
        """Retrieve set of operations successfully completed by a unit."""
        rows = self.con.execute(
            "SELECT op FROM station_result WHERE serial = ? AND result = 'OK'", (serial,)
        ).fetchall()
        return {r["op"] for r in rows}

    def _log_result(self, serial: str, op: str, result: str, detail: str = "") -> None:
        """Insert a station result log entry."""
        self.con.execute(
            "INSERT INTO station_result (serial, op, result, detail, ts) VALUES (?,?,?,?,?)",
            (serial, op, result, detail, self.now(1)),
        )

    # ---------------------------------------------------------- serialization
    def register_unit(self, variant: str) -> str:
        """Serialize a new unit at OP10, store DMC payload, and register in WIP."""
        if variant not in self.route["variants"]:
            raise ValueError(f"Unknown variant {variant}")
        self._seq += 1
        serial = f"{self.route['serial_prefix']}{self.clock:%y%m%d}{self._seq:05d}"
        dmc = f"[)>06P{variant}S{serial}"  # ISO/IEC 15434 style data-matrix payload
        self.con.execute(
            "INSERT INTO unit (serial, variant, dmc, status, created_at) VALUES (?,?,?,?,?)",
            (serial, variant, dmc, "WIP", self.now(2)),
        )
        self._log_result(serial, "OP10", "OK", "serialized")
        return serial

    # --------------------------------------------------------- route interlock
    def station_request(self, serial: str, op: str) -> tuple[bool, int]:
        """Interlock decision returned to PLC: (is_released, reason_code).

        Reason Codes:
            0: OK (station released)
            1: Unknown serial
            2: Previous operation not complete
            3: Operation already complete
            4: Unit on non-conformance hold
            8: Unit scrapped
        """
        unit = self._unit(serial)
        if unit is None:
            return False, 1
        if unit["status"] == "SCRAP":
            return False, 8
        if unit["status"] == "NC_HOLD":
            return False, 4
        passed = self._passed_ops(serial)
        if op in passed:
            return False, 3
        if op not in self.ops:
            return False, 1
        idx = self.ops.index(op)
        if any(prev not in passed for prev in self.ops[:idx]):
            return False, 2
        return True, 0

    # ------------------------------------------------------ part validation
    def scan_component(self, serial: str, op: str, barcode: str) -> tuple[bool, int]:
        """Barcode format: <part_number>|<lot>|<component_serial>.

        Returns (is_valid, reason_code):
            0: Valid, component genealogy recorded
            1: Unknown unit serial
            4: Unit on non-conformance hold
            5: Wrong part for variant (or malformed barcode)
            6: Component lot on containment hold
            7: Component serial already consumed
            8: Unit scrapped
        """
        unit = self._unit(serial)
        if unit is None:
            return False, 1
        if unit["status"] == "SCRAP":
            return False, 8
        if unit["status"] == "NC_HOLD":
            return False, 4

        parts = barcode.split("|")
        if len(parts) != 3:
            self._log_result(serial, op, "SCAN_REJECT", f"malformed barcode '{barcode}'")
            return False, 5

        part_no, lot, comp_sn = parts
        kind = self.stations.get(op, {}).get("scan_component")
        if not kind:
            return False, 5

        expected = self.route["variants"][unit["variant"]][kind]
        if part_no != expected:
            self._log_result(serial, op, "SCAN_REJECT", f"expected {expected}, scanned {part_no}")
            return False, 5

        if self.con.execute(
            "SELECT 1 FROM lot_hold WHERE lot = ? AND released_at IS NULL", (lot,)
        ).fetchone():
            self._log_result(serial, op, "SCAN_REJECT", f"lot {lot} on hold")
            return False, 6

        if self.con.execute(
            "SELECT 1 FROM genealogy WHERE component_serial = ?", (comp_sn,)
        ).fetchone():
            self._log_result(serial, op, "SCAN_REJECT", f"component {comp_sn} already consumed")
            return False, 7

        self.con.execute(
            "INSERT INTO genealogy (serial, op, component_type, part_number, lot, component_serial, ts) "
            "VALUES (?,?,?,?,?,?,?)",
            (serial, op, kind, part_no, lot, comp_sn, self.now(3)),
        )
        return True, 0

    # --------------------------------------------------------- process data
    def record_press(self, serial: str, force_kn: float, depth_mm: float) -> bool:
        """Validate press fit parameters for OP20 against engineering window."""
        unit = self._unit(serial)
        if unit is None:
            raise ValueError(f"Unknown serial {serial}")
        spec = self.stations["OP20"]
        ok = (
            spec["press_force_kn"]["low"] <= force_kn <= spec["press_force_kn"]["high"]
            and spec["press_depth_mm"]["low"] <= depth_mm <= spec["press_depth_mm"]["high"]
        )
        self.con.execute(
            "INSERT INTO press_result (serial, force_kn, depth_mm, result, ts) VALUES (?,?,?,?,?)",
            (serial, force_kn, depth_mm, "OK" if ok else "NOK", self.now(25)),
        )
        if ok:
            self._log_result(serial, "OP20", "OK")
        else:
            self._raise_nc(
                serial,
                "OP20",
                f"press force {force_kn} kN / depth {depth_mm} mm out of window",
            )
        return ok

    def record_torque(
        self, serial: str, op: str, bolt: int, torque: float, angle: float, attempt: int
    ) -> str:
        """Validate torque and angle against engineering limits.

        Returns:
            "OK": In tolerance
            "RETRY": Out of window, retry attempt allowed under station limit
            "NC": Retry limit exceeded -> Unit placed on NC hold
        """
        unit = self._unit(serial)
        if unit is None:
            raise ValueError(f"Unknown serial {serial}")
        spec = self.stations[op]
        t, a = spec["torque_nm"], spec["angle_deg"]
        ok = t["low"] <= torque <= t["high"] and a["low"] <= angle <= a["high"]
        self.con.execute(
            "INSERT INTO torque_result (serial, op, bolt, attempt, torque_nm, angle_deg, result, ts) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (serial, op, bolt, attempt, torque, angle, "OK" if ok else "NOK", self.now(6)),
        )
        if ok:
            return "OK"
        if attempt <= spec["max_retries"]:
            return "RETRY"
        self._raise_nc(
            serial,
            op,
            f"bolt {bolt}: {torque} Nm / {angle}\u00b0 out of window after {attempt} attempts",
        )
        return "NC"

    def complete_torque_op(self, serial: str, op: str) -> None:
        """Mark torque operation fully completed when all bolts pass."""
        self._log_result(serial, op, "OK", f"{self.stations[op]['bolts']} bolts OK")

    def record_inspection(self, serial: str, checks: dict[str, bool]) -> bool:
        """Record manual/automated inspection checklist at OP50."""
        unit = self._unit(serial)
        if unit is None:
            raise ValueError(f"Unknown serial {serial}")
        failed = [k for k, v in checks.items() if not v]
        if failed:
            self._raise_nc(serial, "OP50", "failed: " + ", ".join(failed))
            return False
        self._log_result(serial, "OP50", "OK")
        return True

    def pack(self, serial: str) -> str:
        """Assign shipping rack, generate SGTIN-96 RFID EPC, and mark packed."""
        unit = self._unit(serial)
        if unit is None:
            raise ValueError(f"Unknown serial {serial}")
        if unit["status"] == "SCRAP":
            raise ValueError(f"Cannot pack scrapped unit {serial}")
        if unit["status"] == "NC_HOLD":
            raise ValueError(f"Cannot pack unit {serial} on non-conformance hold")

        cap = self.stations["OP60"]["rack_capacity"]
        packed = self.con.execute("SELECT COUNT(*) FROM unit WHERE status = 'PACKED'").fetchone()[0]
        rack = f"RACK-{packed // cap + 1:04d}"
        epc = f"3034{int(serial[-5:]):020X}"  # simplified SGTIN-96 style EPC
        self.con.execute(
            "UPDATE unit SET status = 'PACKED', rack = ?, rfid_epc = ? WHERE serial = ?",
            (rack, epc, serial),
        )
        self._log_result(serial, "OP60", "OK", rack)
        return rack

    # ---------------------------------------------- non-conformance & rework
    def _raise_nc(self, serial: str, op: str, description: str) -> None:
        """Log open non-conformance, flag unit to NC_HOLD, and trigger quality andon."""
        self.con.execute(
            "INSERT INTO nonconformance (serial, op, description, status, opened_at) "
            "VALUES (?,?,?,?,?)",
            (serial, op, description, "OPEN", self.now(1)),
        )
        self.con.execute("UPDATE unit SET status = 'NC_HOLD' WHERE serial = ?", (serial,))
        self._log_result(serial, op, "NOK", description)
        self.andon(op, "QUALITY", description)

    def rework(
        self, serial: str, technician: str, action: str, disposition: str = "REWORK"
    ) -> None:
        """Process technician disposition at RW01 rework bench."""
        nc = self.con.execute(
            "SELECT * FROM nonconformance WHERE serial = ? AND status = 'OPEN'", (serial,)
        ).fetchone()
        if nc is None:
            raise ValueError(f"No open NC for {serial}")
        self.con.execute(
            "UPDATE nonconformance SET status = 'CLOSED', disposition = ?, action = ?, "
            "closed_by = ?, closed_at = ? WHERE nc_id = ?",
            (disposition, action, technician, self.now(300), nc["nc_id"]),
        )
        new_status = "SCRAP" if disposition == "SCRAP" else "WIP"
        self.con.execute("UPDATE unit SET status = ? WHERE serial = ?", (new_status, serial))
        self._log_result(serial, "RW01", disposition, action)

    def andon(self, op: str, kind: str, message: str) -> None:
        """Log an andon alert event."""
        self.con.execute(
            "INSERT INTO andon_event (op, kind, message, ts) VALUES (?,?,?,?)",
            (op, kind, message, self.now()),
        )

    # ----------------------------------------------------------- containment
    def hold_lot(self, lot: str, reason: str) -> list[str]:
        """Supplier lot containment: block further use and return every affected unit."""
        self.con.execute(
            "INSERT INTO lot_hold (lot, reason, held_at) VALUES (?,?,?)", (lot, reason, self.now())
        )
        rows = self.con.execute(
            "SELECT DISTINCT serial FROM genealogy WHERE lot = ?", (lot,)
        ).fetchall()
        return [r["serial"] for r in rows]

    # --------------------------------------------------- analytics & queries
    def get_birth_history(self, serial: str) -> list[sqlite3.Row]:
        """Fetch chronologically ordered birth history for a serialized unit."""
        q = self.load_queries()
        return self.con.execute(q["unit_birth_history"], {"serial": serial}).fetchall()

    def get_containment_records(self, lot: str) -> list[sqlite3.Row]:
        """Fetch list of units containing a specified component lot."""
        q = self.load_queries()
        return self.con.execute(q["containment_by_lot"], {"lot": lot}).fetchall()
