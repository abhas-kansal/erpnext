from typing import Any

import frappe
from frappe import _dict
from frappe.tests.utils import FrappeTestCase
from frappe.utils import today

from erpnext.stock.doctype.item.test_item import make_item
from erpnext.stock.doctype.serial_and_batch_bundle.test_serial_and_batch_bundle import (
	get_batch_from_bundle,
	get_serial_nos_from_bundle,
)
from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry
from erpnext.stock.doctype.stock_reconciliation.test_stock_reconciliation import (
	create_stock_reconciliation,
)
from erpnext.stock.report.stock_balance.stock_balance import (
	StockBalanceReport,
	execute,
	get_stock_ageing_data,
)


def stock_balance(filters):
	"""Get rows from stock balance report"""
	return [_dict(row) for row in execute(filters)[1]]


class TestStockBalance(FrappeTestCase):
	# ----------- utils

	def setUp(self):
		self.item = make_item()
		self.filters = _dict(
			{
				"company": "_Test Company",
				"item_code": [self.item.name],
				"from_date": "2020-01-01",
				"to_date": str(today()),
			}
		)

	def tearDown(self):
		frappe.db.rollback()

	def assertPartialDictEq(self, expected: dict[str, Any], actual: dict[str, Any]):
		for k, v in expected.items():
			self.assertEqual(v, actual[k], msg=f"{expected=}\n{actual=}")

	def generate_stock_ledger(self, item_code: str, movements):
		for movement in map(_dict, movements):
			if "to_warehouse" not in movement:
				movement.to_warehouse = "_Test Warehouse - _TC"
			make_stock_entry(item_code=item_code, **movement)

	def assertInvariants(self, rows):
		last_balance = frappe.db.sql(
			"""
			WITH last_balances AS (
				SELECT item_code, warehouse,
					stock_value, qty_after_transaction,
					ROW_NUMBER() OVER (PARTITION BY item_code, warehouse
						ORDER BY timestamp(posting_date, posting_time) desc, creation desc)
						AS rn
					FROM `tabStock Ledger Entry`
					where is_cancelled=0
				)
				SELECT * FROM last_balances WHERE rn = 1""",
			as_dict=True,
		)

		item_wh_stock = _dict()

		for line in last_balance:
			item_wh_stock.setdefault((line.item_code, line.warehouse), line)

		for row in rows:
			msg = f"Invariants not met for {rows=}"
			# qty invariant
			self.assertAlmostEqual(row.bal_qty, row.opening_qty + row.in_qty - row.out_qty, msg)

			# value invariant
			self.assertAlmostEqual(row.bal_val, row.opening_val + row.in_val - row.out_val, msg)

			# check against SLE
			last_sle = item_wh_stock[(row.item_code, row.warehouse)]
			self.assertAlmostEqual(row.bal_qty, last_sle.qty_after_transaction, 3)
			self.assertAlmostEqual(row.bal_val, last_sle.stock_value, 3)

			# valuation rate
			if not row.bal_qty:
				continue
			self.assertAlmostEqual(row.val_rate, row.bal_val / row.bal_qty, 3, msg)

	# ----------- tests

	def test_basic_stock_balance(self):
		"""Check very basic functionality and item info"""
		rows = stock_balance(self.filters)
		self.assertEqual(rows, [])

		self.generate_stock_ledger(self.item.name, [_dict(qty=5, rate=10)])

		# check item info
		rows = stock_balance(self.filters)
		self.assertPartialDictEq(
			{
				"item_code": self.item.name,
				"item_name": self.item.item_name,
				"item_group": self.item.item_group,
				"stock_uom": self.item.stock_uom,
				"in_qty": 5,
				"in_val": 50,
				"val_rate": 10,
			},
			rows[0],
		)
		self.assertInvariants(rows)

	def test_opening_balance(self):
		self.generate_stock_ledger(
			self.item.name,
			[
				_dict(qty=1, rate=1, posting_date="2021-01-01"),
				_dict(qty=2, rate=2, posting_date="2021-01-02"),
				_dict(qty=3, rate=3, posting_date="2021-01-03"),
			],
		)
		rows = stock_balance(self.filters)
		self.assertInvariants(rows)

		rows = stock_balance(self.filters.update({"from_date": "2021-01-02"}))
		self.assertInvariants(rows)
		self.assertPartialDictEq({"opening_qty": 1, "in_qty": 5}, rows[0])

		rows = stock_balance(self.filters.update({"from_date": "2022-01-01"}))
		self.assertInvariants(rows)
		self.assertPartialDictEq({"opening_qty": 6, "in_qty": 0}, rows[0])

	def test_uom_converted_info(self):
		self.item.append("uoms", {"conversion_factor": 5, "uom": "Box"})
		self.item.save()

		self.generate_stock_ledger(self.item.name, [_dict(qty=5, rate=10)])

		rows = stock_balance(self.filters.update({"include_uom": "Box"}))
		self.assertEqual(rows[0].bal_qty_alt, 1)
		self.assertInvariants(rows)

	def test_item_group(self):
		self.filters.pop("item_code", None)
		rows = stock_balance(self.filters.update({"item_group": self.item.item_group}))
		self.assertTrue(all(r.item_group == self.item.item_group for r in rows))

	def test_child_warehouse_balances(self):
		# This is default
		self.generate_stock_ledger(self.item.name, [_dict(qty=5, rate=10, to_warehouse="Stores - _TC")])

		self.filters.pop("item_code", None)
		rows = stock_balance(self.filters.update({"warehouse": "All Warehouses - _TC"}))

		self.assertTrue(
			any(r.item_code == self.item.name and r.warehouse == "Stores - _TC" for r in rows),
			msg=f"Expected child warehouse balances \n{rows}",
		)

	def test_show_item_attr(self):
		from erpnext.controllers.item_variant import create_variant

		self.item.has_variants = True
		self.item.append("attributes", {"attribute": "Test Size"})
		self.item.save()

		attributes = {"Test Size": "Large"}
		variant = create_variant(self.item.name, attributes)
		variant.save()

		self.generate_stock_ledger(variant.name, [_dict(qty=5, rate=10)])
		rows = stock_balance(self.filters.update({"show_variant_attributes": 1, "item_code": [variant.name]}))
		self.assertPartialDictEq(attributes, rows[0])
		self.assertInvariants(rows)

	def test_stock_ageing_data_accepts_batchwise_valuation_slots(self):
		fifo_queue = [
			["SA-BATCH-NEWER", 1, 2.0, "2021-12-05", 20.0],
			["SA-BATCH-OLDER", 1, 3.0, "2021-12-01", 30.0],
		]

		stock_ageing_data = get_stock_ageing_data(fifo_queue, "2021-12-10")

		self.assertEqual(stock_ageing_data["average_age"], 7.4)
		self.assertEqual(stock_ageing_data["earliest_age"], 9)
		self.assertEqual(stock_ageing_data["latest_age"], 5)
		self.assertEqual(
			stock_ageing_data["fifo_queue"],
			[[3.0, "2021-12-01", 30.0], [2.0, "2021-12-05", 20.0]],
		)


class TestStockBalanceSerialBatchWise(FrappeTestCase):
	test_warehouse = "_Test Warehouse - _TC"

	def setUp(self):
		self.filters = _dict(
			{
				"company": "_Test Company",
				"warehouse": self.test_warehouse,
				"from_date": "2020-01-01",
				"to_date": str(today()),
			}
		)

	def tearDown(self):
		frappe.db.rollback()

	def make_batch_item(self):
		return make_item(
			properties={
				"is_stock_item": 1,
				"has_batch_no": 1,
				"create_new_batch": 1,
			}
		)

	def receive_batch(self, item_code, qty, rate):
		"""Receive `qty` of a fresh, auto-created batch into the test warehouse."""
		se = make_stock_entry(item_code=item_code, to_warehouse=self.test_warehouse, qty=qty, rate=rate)
		return get_batch_from_bundle(se.items[0].serial_and_batch_bundle)

	def legacy_serial_entry(self, serial_no, actual_qty, stock_value_difference):
		"""A pre-bundle entry: serials in one text field, no Serial and Batch Bundle."""
		return _dict(
			{
				"serial_no": serial_no,
				"serial_and_batch_bundle": None,
				"has_serial_no": 1,
				"actual_qty": actual_qty,
				"stock_value_difference": stock_value_difference,
			}
		)

	def make_batch_and_serial_item(self):
		return make_item(
			properties={
				"is_stock_item": 1,
				"has_batch_no": 1,
				"create_new_batch": 1,
				"has_serial_no": 1,
				"serial_no_series": "SN-.####",
			}
		)

	def report(self, **extra):
		return StockBalanceReport(_dict({**self.filters, **extra}))

	def serial_batch_report(self, **extra):
		return self.report(show_serial_batch_wise=1, **extra)

	def test_default_view_still_aggregates_across_batches(self):
		"""Without the filter, batches of the same item/warehouse stay merged into one row."""
		item = self.make_batch_item()
		self.receive_batch(item.name, 10, 100)
		self.receive_batch(item.name, 20, 200)

		rows = stock_balance(self.filters.update({"item_code": [item.name]}))
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].bal_qty, 30)
		self.assertEqual(rows[0].bal_val, 5000)
		self.assertNotIn("batch_no", rows[0])

	def test_batch_wise_breakdown(self):
		"""Each batch gets its own row, with its own qty/value; Serial No stays blank for it."""
		item = self.make_batch_item()
		batch1 = self.receive_batch(item.name, 10, 100)
		batch2 = self.receive_batch(item.name, 20, 200)

		self.filters.update({"item_code": [item.name], "show_serial_batch_wise": 1})
		columns, data = execute(self.filters)

		col_fieldnames = [c["fieldname"] for c in columns if isinstance(c, dict)]
		self.assertIn("batch_no", col_fieldnames)
		self.assertIn("serial_no", col_fieldnames)

		rows = {row["batch_no"]: _dict(row) for row in data}
		self.assertEqual(rows[batch1].bal_qty, 10)
		self.assertEqual(rows[batch1].bal_val, 1000)
		self.assertEqual(rows[batch1].val_rate, 100)
		self.assertEqual(rows[batch1].serial_no, "")
		self.assertEqual(rows[batch2].bal_qty, 20)
		self.assertEqual(rows[batch2].bal_val, 4000)
		self.assertEqual(rows[batch2].val_rate, 200)

	def test_batch_and_serial_item_gets_one_row_per_serial_with_its_batch(self):
		"""An item tracked by both dimensions gets full granularity: one row per unit,
		carrying both its batch and its own serial."""
		item = self.make_batch_and_serial_item()
		se = make_stock_entry(item_code=item.name, to_warehouse=self.test_warehouse, qty=2, rate=100)
		batch = get_batch_from_bundle(se.items[0].serial_and_batch_bundle)
		serials = sorted(get_serial_nos_from_bundle(se.items[0].serial_and_batch_bundle))

		self.filters.update({"item_code": [item.name], "show_serial_batch_wise": 1})
		rows = {row["serial_no"]: _dict(row) for row in execute(self.filters)[1]}

		self.assertEqual(sorted(rows), serials)
		for serial in serials:
			self.assertEqual(rows[serial].batch_no, batch)
			self.assertEqual(rows[serial].bal_qty, 1)
			self.assertEqual(rows[serial].bal_val, 100)

	def test_can_filter_by_batch_and_serial_together(self):
		"""Batch No and Serial No filters narrow independently and can be combined."""
		item = self.make_batch_and_serial_item()
		se = make_stock_entry(item_code=item.name, to_warehouse=self.test_warehouse, qty=2, rate=100)
		batch = get_batch_from_bundle(se.items[0].serial_and_batch_bundle)
		serials = sorted(get_serial_nos_from_bundle(se.items[0].serial_and_batch_bundle))
		target_serial = serials[0]

		self.filters.update(
			{
				"item_code": [item.name],
				"show_serial_batch_wise": 1,
				"batch_no": batch,
				"serial_no": target_serial,
			}
		)
		rows = [_dict(row) for row in execute(self.filters)[1]]

		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].batch_no, batch)
		self.assertEqual(rows[0].serial_no, target_serial)

	def test_reconciling_one_batch_does_not_corrupt_another(self):
		"""Reconciling one batch leaves the other batches of that item+warehouse alone."""
		item = self.make_batch_item()
		batch1 = self.receive_batch(item.name, 10, 100)
		batch2 = self.receive_batch(item.name, 20, 100)

		create_stock_reconciliation(
			item_code=item.name,
			warehouse=self.test_warehouse,
			qty=15,
			rate=100,
			reconcile_all_serial_batch=0,
			batch_no=batch1,
		)

		self.filters.update({"item_code": [item.name], "show_serial_batch_wise": 1})
		rows = {row["batch_no"]: _dict(row) for row in execute(self.filters)[1]}

		self.assertEqual(rows[batch1].bal_qty, 15)  # reconciled up from 10
		self.assertEqual(rows[batch2].bal_qty, 20)  # untouched by batch1's reconciliation

	def make_unbundled_reconciliation_sle(self, item_code, warehouse, voucher_no, actual_qty=-5):
		"""A raw pre-bundle-style Stock Reconciliation SLE: no batch_no, no bundle -- an
		item+warehouse snapshot that can't be attributed to any specific batch/serial."""
		frappe.get_doc(
			{
				"doctype": "Stock Ledger Entry",
				"item_code": item_code,
				"warehouse": warehouse,
				"posting_date": "2020-06-01",
				"posting_time": "00:00:00",
				"posting_datetime": "2020-06-01 00:00:00",
				"voucher_type": "Stock Reconciliation",
				"voucher_no": voucher_no,
				"voucher_detail_no": f"{voucher_no}-1",
				"actual_qty": actual_qty,
				"qty_after_transaction": 5,
				"stock_value": 500,
				"stock_value_difference": actual_qty * 100,
				"valuation_rate": 100,
				"company": "_Test Company",
				"is_cancelled": 0,
				"docstatus": 1,
			}
		).db_insert()

	def test_unbundled_reconciliation_is_refused_not_guessed(self):
		"""An unbundled reco does not say which batch/serial it replaced, so refuse rather than guess."""
		item = self.make_batch_item()
		self.receive_batch(item.name, 10, 100)
		self.make_unbundled_reconciliation_sle(item.name, self.test_warehouse, "SR-LEGACY-0001")

		self.filters.update({"item_code": [item.name], "show_serial_batch_wise": 1})
		with self.assertRaises(frappe.ValidationError) as raised:
			execute(self.filters)

		self.assertIn("SR-LEGACY-0001", str(raised.exception))

	def test_unbundled_reconciliation_of_untracked_item_does_not_block_the_report(self):
		"""The ambiguity guard itself must skip an untracked item's unbundled reconciliation,
		not just the row expansion -- an untracked item has only one row to begin with, so
		there is nowhere ambiguous for its snapshot to go."""
		item = make_item(properties={"is_stock_item": 1})
		make_stock_entry(item_code=item.name, to_warehouse=self.test_warehouse, qty=10, rate=100)
		self.make_unbundled_reconciliation_sle(item.name, self.test_warehouse, "SR-PLAIN-0002")

		self.filters.update({"item_code": [item.name], "show_serial_batch_wise": 1})
		execute(self.filters)  # must not raise

	def test_batch_filter_does_not_hide_an_ambiguous_reconciliation(self):
		"""The safety check must see every ambiguous voucher for the item, not just the ones
		that happen to match the batch_no/serial_no filter -- otherwise scoping down to one
		batch silently defeats the refusal an unscoped run would have triggered."""
		item = self.make_batch_item()
		batch1 = self.receive_batch(item.name, 10, 100)
		self.make_unbundled_reconciliation_sle(item.name, self.test_warehouse, "SR-AMBIGUOUS-0001")

		self.filters.update({"item_code": [item.name], "show_serial_batch_wise": 1, "batch_no": batch1})
		self.assertRaises(frappe.ValidationError, execute, self.filters)

	def test_zero_qty_unbundled_reconciliation_is_allowed(self):
		"""A recount that matched the existing balance writes a delta-free SLE with no batch_no
		-- it changes nothing for any batch, so it isn't actually ambiguous and must not block
		the report (this is what a real recount-confirms-existing-balance entry looks like)."""
		item = self.make_batch_item()
		self.receive_batch(item.name, 10, 100)
		self.make_unbundled_reconciliation_sle(
			item.name, self.test_warehouse, "SR-NOOP-0001", actual_qty=0
		)

		self.filters.update({"item_code": [item.name], "show_serial_batch_wise": 1})
		execute(self.filters)  # must not raise

	def test_unbundled_reconciliation_of_untracked_item_is_allowed(self):
		"""An untracked item has only one row, so its snapshot has somewhere unambiguous to go."""
		entry = _dict(
			{
				"serial_no": None,
				"serial_and_batch_bundle": None,
				"has_batch_no": 0,
				"has_serial_no": 0,
				"voucher_type": "Stock Reconciliation",
				"voucher_no": "SR-PLAIN-0001",
				"actual_qty": 5,
				"stock_value_difference": 500,
			}
		)

		rows = self.serial_batch_report().expand_serial_batch_wise_entries([entry])
		self.assertEqual(len(rows), 1)

	def test_unbundled_reconciliation_with_direct_batch_no_is_allowed(self):
		"""Pre-bundle data with batch_no set directly on the row is already attributable --
		prepare_item_warehouse_map already trusts it as a per-batch delta unexpanded."""
		entry = _dict(
			{
				"batch_no": "B-LEGACY-1",
				"serial_no": None,
				"serial_and_batch_bundle": None,
				"has_batch_no": 1,
				"has_serial_no": 0,
				"voucher_type": "Stock Reconciliation",
				"voucher_no": "SR-LEGACY-BATCH-0001",
				"actual_qty": 5,
				"stock_value_difference": 500,
			}
		)

		rows = self.serial_batch_report().expand_serial_batch_wise_entries([entry])
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].batch_no, "B-LEGACY-1")
		self.assertFalse(rows[0].get("is_expanded_row"))

	def test_legacy_multi_serial_entry_goes_to_the_shared_bucket_unsplit(self):
		"""Matches Stock Ledger's own segregate_serial_batch_bundle precedent: a legacy
		multi-serial entry is not split per unit, just passed through with serial_no blank
		rather than guessed at -- its qty/value still land somewhere, just not attributed."""
		rows = self.serial_batch_report().expand_serial_batch_wise_entries(
			[self.legacy_serial_entry("SN-1\nSN-2", 2, 200)]
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].serial_no, "")
		self.assertEqual(rows[0].actual_qty, 2)
		self.assertEqual(rows[0].stock_value_difference, 200)

	def test_legacy_multi_serial_entry_excluded_when_filtering_to_one_serial(self):
		"""Can't confirm it belongs to the filtered serial without splitting, so it's left
		out of a serial-scoped view rather than included as an unqualified guess."""
		report = self.report(show_serial_batch_wise=1, serial_no="SN-1")
		rows = report.expand_serial_batch_wise_entries([self.legacy_serial_entry("SN-1\nSN-2", 2, 200)])
		self.assertEqual(rows, [])

	def test_batch_filter_excludes_other_items(self):
		"""The Batch No filter must reach SQL, not just narrow bundle children."""
		batch_item = self.make_batch_item()
		batch1 = self.receive_batch(batch_item.name, 10, 100)
		self.receive_batch(batch_item.name, 20, 100)

		plain_item = make_item(properties={"is_stock_item": 1})
		make_stock_entry(item_code=plain_item.name, to_warehouse=self.test_warehouse, qty=7, rate=100)

		self.filters.update({"show_serial_batch_wise": 1, "batch_no": batch1})
		rows = [_dict(row) for row in execute(self.filters)[1]]

		self.assertEqual([(row.item_code, row.batch_no) for row in rows], [(batch_item.name, batch1)])

	def test_reserved_stock_column_dropped_in_this_view(self):
		"""Reserved stock is per item+warehouse; repeating it per batch would inflate the total."""
		self.filters.update({"show_serial_batch_wise": 1})
		columns, _data = execute(self.filters)

		self.assertNotIn("reserved_stock", [c["fieldname"] for c in columns if isinstance(c, dict)])

	def test_requires_a_scoping_filter(self):
		self.filters.pop("warehouse")
		self.filters.update({"show_serial_batch_wise": 1})
		self.assertRaises(frappe.ValidationError, execute, self.filters)

	def test_item_group_alone_satisfies_the_scoping_filter(self):
		item = self.make_batch_item()
		batch = self.receive_batch(item.name, 10, 100)

		self.filters.pop("warehouse")
		self.filters.update({"item_group": item.item_group, "show_serial_batch_wise": 1})
		rows = stock_balance(self.filters)

		self.assertEqual([row.batch_no for row in rows if row.item_code == item.name], [batch])

	def test_cannot_combine_with_stock_ageing_data(self):
		self.filters.update({"show_serial_batch_wise": 1, "show_stock_ageing_data": 1})
		self.assertRaises(frappe.ValidationError, execute, self.filters)
