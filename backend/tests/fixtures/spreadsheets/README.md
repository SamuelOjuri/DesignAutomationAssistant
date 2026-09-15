# Spreadsheet fixture

`pricing_schedule.xls` is a synthetic BIFF8 workbook generated with xlwt 1.3.0.
It contains no customer data. Its `Pricing` sheet has item, quantity, unit price,
zero, boolean and date cells; `Notes!A3` contains `Delivery included`.
The committed binary exercises the real xlrd reader without requiring an XLS
writer in production or in the test dependencies. XLSX workbooks are generated
in tests using the production openpyxl dependency.
