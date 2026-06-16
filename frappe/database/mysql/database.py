"""
MySQL 8 database backend for Frappe.

Extends MariaDB's mysqlclient backend (MySQLdb C library) to work with
MySQL 8.x instead of MariaDB. Most SQL is compatible; we only override
the handful of places where MariaDB and MySQL diverge.

Key incompatibilities fixed here vs MariaDB:
  - Statement timeout: MariaDB uses `max_statement_time` (seconds, error 1969)
                       MySQL 8 uses `max_execution_time` (ms, error 3024)
  - Sequences:         MariaDB has native SEQUENCE objects;
                       MySQL 8 uses AUTO_INCREMENT (emulated via a helper table)
  - CAST(x AS varchar): MySQL 8 requires CAST(x AS CHAR)
"""

import MySQLdb
from MySQLdb.constants import ER

import frappe
from frappe.database.mariadb.mysqlclient import MariaDBConnectionUtil, MariaDBDatabase, MariaDBExceptionUtil
from frappe.database.mysql.schema import MySQLTable

# MySQL 8 error code for query execution timeout (cf. MariaDB 1969)
ER_QUERY_TIMEOUT = 3024


class MySQLExceptionUtil(MariaDBExceptionUtil):
	"""Override only the error codes that differ between MariaDB and MySQL 8."""

	@staticmethod
	def is_statement_timeout(e: MySQLdb.Error) -> bool:
		return e.args and e.args[0] == ER_QUERY_TIMEOUT


class MySQLConnectionUtil(MariaDBConnectionUtil):
	"""MySQL 8 connection tweaks."""

	def set_execution_timeout(self, seconds: int):
		# MySQL 8 uses max_execution_time in *milliseconds* (not seconds like MariaDB)
		self.sql("SET SESSION max_execution_time = %s", int(seconds * 1000))


class MySQLDatabase(MySQLConnectionUtil, MySQLExceptionUtil, MariaDBDatabase):
	"""
	MySQL 8 database class.

	Inherits all MariaDB/mysqlclient logic and selectively overrides
	MySQL-incompatible behaviour.
	"""

	db_type = "mysql"

	class SequenceGeneratorLimitExceeded(Exception):
		"""Raised when a sequence reaches its max_value with cycle=False."""
		pass

	def setup_type_map(self):
		super().setup_type_map()
		# Keep db_type as mysql (super sets it to 'mariadb')
		self.db_type = "mysql"

	# ── Schema class ──────────────────────────────────────────────────
	def get_table_class(self):
		return MySQLTable

	# ── Sequences (autoname = "autoincrement") ─────────────────────────
	# MySQL 8 does not have CREATE SEQUENCE / NEXTVAL.
	# We emulate sequences with a dedicated helper table.

	_SEQUENCE_TABLE = "`__frappe_sequences`"

	def _ensure_sequence_table(self):
		self.sql_ddl(
			f"""CREATE TABLE IF NOT EXISTS {self._SEQUENCE_TABLE} (
				`name`        varchar(255) NOT NULL,
				`next_val`    bigint NOT NULL DEFAULT 1,
				`increment_by` bigint NOT NULL DEFAULT 1,
				`min_value`   bigint NOT NULL DEFAULT 1,
				`max_value`   bigint,
				`cycle`       tinyint(1) NOT NULL DEFAULT 0,
				PRIMARY KEY (`name`)
			) ENGINE=InnoDB CHARACTER SET=utf8mb4"""
		)

	def create_sequence(
		self,
		doctype,
		*,
		check_not_exists=False,
		temporary=False,
		start=None,
		start_value=None,
		cache=0,
		cycle=False,
		increment_by=1,
		min_value=1,
		max_value=None,
	):
		"""
		Emulate MariaDB/Postgres SEQUENCE using a helper table.

		MySQL 8 has no native SEQUENCE type; we store per-sequence state in
		__frappe_sequences.  `temporary` is accepted but silently ignored —
		MySQL has no TEMPORARY tables with the same transaction scope as
		a TEMPORARY SEQUENCE, so callers relying on automatic cleanup (e.g.
		tests) should drop the sequence explicitly or rely on rollback.
		"""
		self._ensure_sequence_table()
		effective_start = start or start_value or min_value or 1
		if check_not_exists:
			if self.sql(f"SELECT 1 FROM {self._SEQUENCE_TABLE} WHERE `name` = %s", (doctype,)):
				return
		self.sql(
			f"""INSERT INTO {self._SEQUENCE_TABLE}
				(`name`, `next_val`, `increment_by`, `min_value`, `max_value`, `cycle`)
			VALUES (%s, %s, %s, %s, %s, %s)
			ON DUPLICATE KEY UPDATE
				`next_val`    = VALUES(`next_val`),
				`increment_by`= VALUES(`increment_by`),
				`min_value`   = VALUES(`min_value`),
				`max_value`   = VALUES(`max_value`),
				`cycle`       = VALUES(`cycle`)""",
			(doctype, effective_start, increment_by, min_value, max_value or 0, int(cycle)),
		)

	def get_next_sequence_val(self, doctype, slug="_id_seq"):
		"""
		Atomically advance the sequence and return the new value.
		Respects max_value, cycle, and increment_by.
		Raises SequenceGeneratorLimitExceeded when the sequence is exhausted.
		"""
		self._ensure_sequence_table()
		row = self.sql(
			f"SELECT `next_val`, `increment_by`, `min_value`, `max_value`, `cycle` "
			f"FROM {self._SEQUENCE_TABLE} WHERE `name` = %s FOR UPDATE",
			(doctype,),
		)
		if not row:
			return None

		current, step, min_val, max_val, do_cycle = row[0]
		next_val = current + step

		if max_val and next_val > max_val:
			if do_cycle:
				next_val = min_val
			else:
				raise self.SequenceGeneratorLimitExceeded

		self.sql(
			f"UPDATE {self._SEQUENCE_TABLE} SET `next_val` = %s WHERE `name` = %s",
			(next_val, doctype),
		)
		return current

	def set_next_sequence_val(self, doctype, next_val, *, slug="_id_seq", is_val_used=False):
		"""Set the next value for the sequence (equivalent to SETVAL)."""
		self._ensure_sequence_table()
		# is_val_used=True means next_val has already been used, so advance by one step
		row = self.sql(
			f"SELECT `increment_by` FROM {self._SEQUENCE_TABLE} WHERE `name` = %s", (doctype,)
		)
		step = row[0][0] if row else 1
		effective = next_val + (step if is_val_used else 0)
		self.sql(
			f"UPDATE {self._SEQUENCE_TABLE} SET `next_val` = %s WHERE `name` = %s",
			(effective, doctype),
		)

	# ── Version check ─────────────────────────────────────────────────
	def get_version(self):
		return self.sql("SELECT VERSION()", as_list=True)[0][0]

	def multisql(self, sql_dict, values=(), **kwargs):
		"""
		Override to fall back to 'mariadb' key when no 'mysql' key exists.
		ERPNext/HRMS code only has 'mariadb' and 'postgres' keys.
		"""
		query = sql_dict.get("mysql") or sql_dict.get("mariadb") or sql_dict.get("*")
		return self.sql(query, values, **kwargs)

	def updatedb(self, doctype, meta=None):
		"""Override to use MySQLTable instead of MariaDBTable."""
		res = self.sql("select issingle from `tabDocType` where name=%s", (doctype,))
		if not res:
			raise Exception(f"Wrong doctype {doctype} in updatedb")

		if not res[0][0]:
			db_table = MySQLTable(doctype, meta)
			db_table.validate()
			db_table.sync()
			self.commit()

	def add_index(self, doctype: str, fields: list, index_name: str | None = None):
		"""
		MySQL 8 does not support ADD INDEX IF NOT EXISTS.
		Pre-check whether the index exists; skip if it does.
		"""
		from frappe.utils import get_table_name

		index_name = index_name or self.get_index_name(fields)
		table_name = get_table_name(doctype)
		if not self.has_index(table_name, index_name):
			self.commit()
			self.sql(
				"ALTER TABLE `{}` ADD INDEX `{}`({})".format(
					table_name, index_name, ", ".join(fields)
				)
			)
			from frappe.custom.doctype.property_setter.property_setter import make_property_setter
			if len(fields) == 1 and not (frappe.flags.in_install or frappe.flags.in_migrate):
				make_property_setter(
					doctype,
					fields[0],
					property="search_index",
					value="1",
					property_type="Check",
				)
