# Cherry System Prompt Notes
Source of truth for Cherry's Retell system prompt and knowledge base additions.
Last applied: 25 Apr 2026 via Retell API.

---

## Booking Flow — Payment Language
Add to the **booking flow instructions** section of Cherry's system prompt:

> When a guest asks how they pay or what payment is required:
> - Tell them: "Once I've taken your details, we'll send you a payment link by SMS and email. Payment is required before check-in."
> - Never say you don't know how payment works.
> - Never ask for card details over the phone.

---

## HSMI Policies Knowledge Base — Payment Section
Add the following section to the **HSMI Policies** knowledge base attached to Cherry:

```
PAYMENT POLICY
- Payment is required before check-in
- After booking, guests receive a payment link by SMS and email
- We accept all major credit and debit cards via the secure payment link
- We do not take card details over the phone
- Non-refundable rate: 10% discount, payment required immediately
- Standard rate: payment required 24 hours before check-in
```

---

## Dynamic Variables (already wired — no action needed)
The `/webhook/call-started` endpoint in `functions.py` injects:
- `{{current_date}}` — e.g. "Saturday 25 April 2026" (Melbourne timezone)
- `{{current_time}}` — e.g. "14:30" (Melbourne timezone)

Ensure the system prompt uses `{{current_date}}` and `{{current_time}}` rather than
any hardcoded date string.
