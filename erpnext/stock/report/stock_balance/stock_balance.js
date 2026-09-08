// Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors and contributors
// For license information, please see license.txt

frappe.query_reports["Stock Balance"] = {
	filters: [
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			width: "80",
			options: "Company",
			default: frappe.defaults.get_default("company"),
		},
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			width: "80",
			reqd: 1,
			default: frappe.datetime.add_months(frappe.datetime.get_today(), -1),
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			width: "80",
			reqd: 1,
			default: frappe.datetime.get_today(),
		},
		{
			fieldname: "item_group",
			label: __("Item Group"),
			fieldtype: "Link",
			width: "80",
			options: "Item Group",
		},
		{
			fieldname: "item_code",
			label: __("Items"),
			fieldtype: "MultiSelectList",
			width: "80",
			options: "Item",
			get_data: async function (txt) {
				let item_group = frappe.query_report.get_filter_value("item_group");

				let filters = {
					...(item_group && { item_group }),
					is_stock_item: 1,
				};

				let { message: data } = await frappe.call({
					method: "erpnext.controllers.queries.item_query",
					args: {
						doctype: "Item",
						txt: txt,
						searchfield: "name",
						start: 0,
						page_len: 10,
						filters: filters,
						as_dict: 1,
					},
				});

				data = data.map(({ name, ...rest }) => {
					return {
						value: name,
						description: Object.values(rest),
					};
				});

				return data || [];
			},
		},
		{
			fieldname: "warehouse",
			label: __("Warehouses"),
			fieldtype: "MultiSelectList",
			width: "80",
			options: "Warehouse",
			get_data: (txt) => {
				let warehouse_type = frappe.query_report.get_filter_value("warehouse_type");
				let company = frappe.query_report.get_filter_value("company");

				let filters = {
					...(warehouse_type && { warehouse_type }),
					...(company && { company }),
				};

				return frappe.db.get_link_options("Warehouse", txt, filters);
			},
		},
		{
			fieldname: "warehouse_type",
			label: __("Warehouse Type"),
			fieldtype: "Link",
			width: "80",
			options: "Warehouse Type",
		},
		{
			fieldname: "valuation_field_type",
			label: __("Valuation Field Type"),
			fieldtype: "Select",
			width: "80",
			options: "Currency\nFloat",
			default: "Currency",
		},
		{
			fieldname: "include_uom",
			label: __("Include UOM"),
			fieldtype: "Link",
			options: "UOM",
		},
		{
			fieldname: "show_variant_attributes",
			label: __("Show Variant Attributes"),
			fieldtype: "Check",
		},
		{
			fieldname: "show_stock_ageing_data",
			label: __("Show Stock Ageing Data"),
			fieldtype: "Check",
		},
		{
			fieldname: "ignore_closing_balance",
			label: __("Ignore Closing Balance"),
			fieldtype: "Check",
			default: 0,
		},
		{
			fieldname: "include_zero_stock_items",
			label: __("Include Zero Stock Items"),
			fieldtype: "Check",
			default: 1,
		},
		{
			fieldname: "show_dimension_wise_stock",
			label: __("Show Dimension Wise Stock"),
			fieldtype: "Check",
			default: 0,
		},
		{
			fieldname: "show_serial_batch_wise",
			label: __("Show Serial / Batch Wise Stock"),
			fieldtype: "Check",
			default: 0,
			on_change: function (query_report) {
				let filters = query_report.get_values();
				let is_scoped =
					filters.item_code?.length ||
					filters.item_group ||
					filters.warehouse?.length ||
					filters.batch_no ||
					filters.serial_no;

				if (filters.show_serial_batch_wise && !is_scoped) {
					// defining on_change replaces the report's default refresh-on-change, so an
					// unscoped toggle must be reported here rather than left to error out on run
					frappe.msgprint(
						__(
							"Select an Item, Item Group, Warehouse, Batch No or Serial No to view Serial / Batch wise stock."
						)
					);
					return;
				}

				query_report.refresh(true);
			},
		},
		{
			fieldname: "batch_no",
			label: __("Batch No"),
			fieldtype: "Link",
			options: "Batch",
			depends_on: "eval:doc.show_serial_batch_wise",
		},
		{
			fieldname: "serial_no",
			label: __("Serial No"),
			fieldtype: "Link",
			options: "Serial No",
			depends_on: "eval:doc.show_serial_batch_wise",
		},
	],

	formatter: function (value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);

		if (column.fieldname == "out_qty" && data && data.out_qty > 0) {
			value = "<span style='color:red'>" + value + "</span>";
		} else if (column.fieldname == "in_qty" && data && data.in_qty > 0) {
			value = "<span style='color:green'>" + value + "</span>";
		}

		return value;
	},

	onload: function (report) {
		report.page.add_inner_button(__("View Stock Ledger"), function () {
			var filters = report.get_values();
			frappe.set_route("query-report", "Stock Ledger", filters);
		});
	},
};

erpnext.utils.add_inventory_dimensions("Stock Balance", 8);
