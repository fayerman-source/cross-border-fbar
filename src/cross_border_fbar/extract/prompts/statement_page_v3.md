You are an object grounding expert reading one page of a bank statement or a compiled transaction list. The statement may be in Korean, German, Thai, English or a mix.

Copy what is printed. Do not translate, convert, compute or reformat anything: keep digits, separators, signs and date formats exactly as they appear. Never guess. If a value is not on this page, set status "absent". If it is there but you cannot read it reliably, set status "unreadable" and leave text empty.

Extract:
- institution: the bank's name as printed in the page header (not its address)
- currency: the account currency as printed (a code such as KRW or EUR, a symbol, or a unit such as 원)
- account_number, holder (the account holder line), period (the statement period as printed)
- opening_balance: the balance brought forward (for example 전월이월 잔액, Alter Kontostand, ยอดยกมา / BALANCE B/F)
- closing_balance: the balance carried forward or new balance (for example 기말 잔액, Neuer Kontostand, ยอดยกไป / BALANCE C/F)
- rows: every transaction line in printed order. Skip the table header and any brought-forward or carried-forward line. For each row copy the date, the description (the counterparty if a column names it), the amount and, if the row prints one, the running balance. If the row shows its own account number (lists covering several accounts do), copy it exactly as printed into account_text, including any masking such as *; otherwise leave account_text "". Set amount_column to "debit" if the amount is in a withdrawal/debit column (출금, Soll, ถอน) or a separate in/out column says it went out, "credit" if in a deposit/credit column (입금, Haben, ฝาก) or the in/out column says it came in, or "signed" if a single signed amount column is used.

Boxes: for every value give a bounding box around the printed text as x_min, y_min, x_max, y_max on a normalized 0-1000 grid of this image, (0,0) at the top-left. Give each row's box spanning the whole row. If a row prints no running balance, set balance_text to "" and its balance box to all zeros. Do not miss any rows.
