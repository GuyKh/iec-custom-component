1. The `iec_last_cost` sensor's `value_fn` uses `data[INVOICE_DICT_NAME].amount_origin`. If the user has a credit/refund (e.g., from solar panels or overpayment), this value from the IEC API is correctly negative. There is no parsing issue.
2. However, there are architectural bugs in `sensor.py`:
   - `get_previous_bill_kwh_price(invoice)` divides `consumption` by `amount_origin` (kWh / ILS). It should divide `amount_origin` by `consumption` (ILS / kWh).
   - If `amount_origin` is negative, `get_previous_bill_kwh_price` will return a negative price. We should use `abs(invoice.amount_origin)` to calculate the tariff correctly.
   - The `iec_last_cost` and `iec_last_elec_usage` sensors have `state_class=SensorStateClass.TOTAL`. Since they just represent the snapshot of the last bill (and can go up, down, or negative), they are NOT accumulating totals. Using `TOTAL` causes HA to interpret negative drops as meter resets. They should use `SensorStateClass.MEASUREMENT` (or None).
