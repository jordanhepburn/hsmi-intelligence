# Cherry QA Test Checklist

Run before any major prompt change goes live. Each test is a real phone call or simulated call via the Retell dashboard.

**Cherry number:** TBC (pending AU regulatory bundle approval)
**Retell agent:** agent_59ba5864fe169392409312b0c2
**Retell dashboard:** https://dashboard.retellai.com/agents/agent_59ba5864fe169392409312b0c2
**Last full review:** 26 Apr 2026

---

## 1. Transfers

### 1.1 Transfer to Veronica — answered
- **Trigger:** Call during business hours (8am–8pm AEST), say "can I speak to someone please"
- **Expected:** Cherry says she'll transfer, Veronica's mobile (+61340631318) rings, call bridges when answered
- **Pass:** Call connects cleanly
- **Fail:** Call drops, Cherry keeps talking, or Veronica never rings

### 1.2 Transfer to Veronica — unanswered
- **Trigger:** Same as 1.1 but Veronica does not answer
- **Expected:** Cherry says "I wasn't able to reach our team — let me take your details and have someone call you back shortly", takes name + number, fires log_message to #phone-calls
- **Pass:** Caller not dropped; details logged
- **Fail:** Call drops silently or Cherry is confused

### 1.3 Transfer to Dwayne (emergency) — answered
- **Trigger:** "There's a fire in my room"
- **Expected:** Cherry immediately transfers to Dwayne (+61448793890), no unnecessary questions first
- **Pass:** Transfer fires without delay
- **Fail:** Cherry asks for details before transferring

### 1.4 Transfer to Dwayne — unanswered
- **Trigger:** Emergency scenario, Dwayne doesn't answer
- **Expected:** Cherry stays on line, logs URGENT maintenance, takes details, reassures caller
- **Pass:** Caller not abandoned
- **Fail:** Call drops

### 1.5 Transfer attempt after hours
- **Trigger:** Call at 9pm, ask to speak to someone
- **Expected:** Cherry does NOT attempt transfer — handles it herself
- **Pass:** No transfer attempted
- **Fail:** Cherry attempts transfer after hours

---

## 2. Booking Flow

### 2.1 Standard booking — non-refundable rate quoted first
- **Say:** "I'd like to book a twin room for this Saturday and Sunday"
- **Expected:** Cherry checks availability, quotes non-refundable rate first (e.g. "$252/night"), does NOT volunteer flexible rate
- **Pass:** Non-refundable quoted first, no mention of flexible until prompted
- **Fail:** Cherry leads with flexible rate or quotes both

### 2.2 Caller asks about cancellations
- **Say:** "What if I need to cancel?"
- **Expected:** Cherry explains flexible rate as an alternative
- **Pass:** Flexible rate offered only after cancellation question
- **Fail:** Cherry confuses policies

### 2.3 Booking completion — mandatory fields
- **Say:** Provide name and phone but refuse to give email
- **Expected:** Cherry insists on email
- **Pass:** Cherry holds firm, doesn't proceed to hold_room without email
- **Fail:** Cherry creates booking without email

### 2.4 Same-day booking before 6pm
- **Say:** "I'd like to check in tonight" (call before 6pm AEST)
- **Expected:** Cherry checks availability and proceeds normally
- **Pass:** Booking taken
- **Fail:** Cherry incorrectly refuses

### 2.5 Same-day booking after 6pm
- **Say:** "I'd like to check in tonight" (call after 6pm AEST)
- **Expected:** Cherry declines tonight, offers future dates or callback
- **Pass:** No hold_room attempted for tonight
- **Fail:** Cherry takes booking for tonight after 6pm

### 2.6 Hesitating caller
- **Say:** "I'll think about it"
- **Expected:** Cherry mentions room is in high demand, offers to hold, offers SMS booking link
- **Pass:** Call doesn't end abruptly; Cherry makes one more attempt to convert
- **Fail:** Cherry says "no problem, bye"

### 2.7 King Spa upsell for couples
- **Say:** "I'm looking for a room for two adults this weekend"
- **Expected:** Cherry proactively mentions the King Spa Room ("very popular with couples, private two-person spa bath in the room")
- **Pass:** King Spa mentioned before caller has to ask
- **Fail:** Cherry only quotes what was asked for

### 2.8 Cot request
- **Say:** "Do you have a cot for our baby?"
- **Expected:** Cherry confirms cot is available free of charge, notes no bassinet available
- **Pass:** Correct answer, no escalation
- **Fail:** Cherry says she doesn't know or promises a bassinet

---

## 3. Discount Policy

### 3.1 Standard discount request
- **Say:** "Can you give me a better rate?"
- **Expected:** Cherry holds firm — non-refundable is already the best rate
- **Pass:** No further discount; Cherry doesn't say "let me check with management"
- **Fail:** Cherry caves or offers an ad-hoc discount

### 3.2 OTA price match request
- **Say:** "Booking.com shows it cheaper — can you match it?"
- **Expected:** Cherry does not match
- **Pass:** No match offered
- **Fail:** Cherry matches or offers to check

### 3.3 Repeat customer discount
- **Say:** "I've stayed with you before — do I get a loyalty discount?"
- **Expected:** Cherry takes their word, offers 20% off non-refundable or 10% off flexible
- **Pass:** Correct tier applied without asking for proof
- **Fail:** Cherry refuses or asks for verification

### 3.4 Group discount — 5 to 12 room nights (handles autonomously)
- **Say:** "We need 3 rooms for 2 nights" (= 6 room nights)
- **Expected:** Cherry applies 20% off non-refundable or 10% off flexible, uses hold_room, logs to #phone-calls tagging Veronica + Jordan + Alessio
- **Pass:** Discount applied, booking held, log fires
- **Fail:** Cherry escalates unnecessarily or gives no discount

### 3.5 Group discount — 13+ room nights (escalates)
- **Say:** "We need 10 rooms for 2 nights" (= 20 room nights)
- **Expected:** Cherry does not price it; takes details; logs to #phone-calls tagging Veronica + Jordan + Alessio
- **Pass:** No rate quoted; log fires with correct tags
- **Fail:** Cherry attempts to price a 20-room-night block

---

## 4. Pet Policy

### 4.1 Pet enquiry
- **Say:** "Do you allow dogs?"
- **Expected:** Immediate firm no: "Sorry, we are a pet-free property. You might want to try the Daylesford Caravan Park — they generally do allow pets."
- **Pass:** Answer in first response, no hedging, redirects helpfully
- **Fail:** Cherry says "let me check" or gives ambiguous answer

### 4.2 Assistance animal
- **Say:** "I have a registered guide dog"
- **Expected:** Cherry confirms assistance animals are permitted
- **Pass:** Correct exception applied
- **Fail:** Cherry refuses or is uncertain

---

## 5. Key / Room Access

### 5.1 Guest can't get into their room
- **Say:** "I can't get into my room, I'm at room 7" (provide name)
- **Expected:** Cherry apologises, logs log_maintenance URGENT with room number and name, says on-site team will be there very shortly
- **Pass:** Maintenance logged URGENT; caller not abandoned; if after hours Cherry offers to transfer to Dwayne
- **Fail:** Cherry says she doesn't know what to do or ends the call

### 5.2 After-hours guest hasn't received check-in SMS
- **Say:** "I'm arriving tonight and haven't got my check-in details"
- **Expected:** Cherry asks name + check-in date, uses get_checkin_instructions, confirms SMS will arrive shortly
- **Pass:** Instructions retrieved after verification; caller reassured
- **Fail:** Cherry can't help or drops the call

---

## 6. Spam / Salespeople

### 6.1 Energy company
- **Say:** "Hi I'm calling from Energy Australia about your electricity bill"
- **Expected:** Cherry identifies within 2 exchanges, takes name/company/number, says she'll pass it on, ends the call. Posts to #phone-calls, no tags.
- **Pass:** Resolved in ≤2 exchanges; no staff names or details given; call ends
- **Fail:** Cherry engages for more than 2 exchanges or gives out Jordan's name/number

### 6.2 Google Ads rep
- **Say:** "I'm calling about your Google Business profile"
- **Expected:** Same — take details, post to #phone-calls, exit
- **Pass:** ≤2 exchanges to exit
- **Fail:** Cherry engages or transfers

---

## 7. Cancellations

### 7.1 OTA booking cancellation
- **Say:** "I need to cancel my Booking.com reservation"
- **Expected:** Cherry redirects to Booking.com; offers to pass a note to team
- **Pass:** No cancellation processed; Booking.com contact info given
- **Fail:** Cherry attempts to cancel an OTA booking

### 7.2 Direct booking — flexible rate, >48hrs out
- **Say:** "I need to cancel, I booked directly and check-in is next week"
- **Expected:** Cherry confirms eligible for refund, logs for team to process
- **Pass:** log_message fires; Cherry does not process refund herself
- **Fail:** Cherry processes or denies refund herself

### 7.3 Direct booking — non-refundable
- **Say:** "I booked the non-refundable rate, can I get a refund?"
- **Expected:** Cherry explains no cash refund; notes voucher option if 48+ hrs notice; logs for team
- **Pass:** Policy stated correctly
- **Fail:** Cherry promises a refund or gives wrong policy

### 7.4 Exceptional circumstances
- **Say:** "I need to cancel — I've been hospitalised"
- **Expected:** Cherry empathises, does not promise refund, logs as "exceptional circumstances — management review needed"
- **Pass:** Correct log reason; no promises made
- **Fail:** Cherry promises a refund or dismisses the situation

---

## 8. Group & Wedding Enquiries

### 8.1 Wedding group
- **Say:** "We're getting married in May and need rooms for about 15 guests"
- **Expected:** Cherry congratulates, sells property warmly ("very popular with wedding groups"), captures date + guest count, explains payment options (one-go or per-guest), logs to #phone-calls tagging Veronica + Jordan + Alessio
- **Pass:** Enthusiastic; no pricing attempt for 15 guests (13+ room nights likely); correct tags
- **Fail:** Cherry tries to price the block or logs to wrong channel

### 8.2 Small group Cherry can handle (≤12 room nights)
- **Say:** "We have 4 people needing 2 rooms for 3 nights" (= 6 room nights)
- **Expected:** Cherry sells property, applies 20% group discount, uses hold_room, logs to #phone-calls
- **Pass:** Booking held with discount; log fires
- **Fail:** Cherry escalates unnecessarily

### 8.3 Large group enquiry (13+ room nights)
- **Say:** "We have a group of 20 people, do you have room for all of us?"
- **Expected:** Cherry sells property warmly, takes details, no rate quoted, logs URGENT to #phone-calls tagging Veronica + Jordan + Alessio
- **Pass:** No pricing; log fires with tags; callback promised "very soon"
- **Fail:** Cherry quotes a rate for 20 people

---

## 9. Stay Extension

### 9.1 In-house guest wants to extend
- **Say:** "We'd love to stay another night, can we extend?" (call as if currently checked in)
- **Expected:** Cherry checks availability, quotes 20% off base rate, runs same process as new booking (hold_room), logs to #phone-calls notifying Veronica
- **Pass:** Rate quoted correctly; hold_room fires; Veronica notified
- **Fail:** Cherry logs generic modification request without quoting rate or holding room

### 9.2 Extension not available
- **Say:** Same as 9.1 but motel is full that night
- **Expected:** Cherry apologises, offers waitlist, logs for team
- **Pass:** No false confirmation; waitlist offered
- **Fail:** Cherry confirms extension on a sold-out night

---

## 10. After-Hours / Late Arrival

### 10.1 Late arrival notice
- **Say:** "Just letting you know we'll be arriving around 10pm"
- **Expected:** Cherry acknowledges, logs the note for the team
- **Pass:** log_message fires; Cherry reassures late arrival is fine
- **Fail:** Cherry says the property closes or they can't check in late

---

## 11. In-House Requests

### 11.1 Late checkout — room available next night
- **Say:** "Can we check out at midday instead of 10?"
- **Expected:** Cherry uses check_late_checkout, confirms 12pm checkout if room is free
- **Pass:** Function fires; outcome confirmed to guest
- **Fail:** Cherry guesses or gives a blanket yes/no

### 11.2 Late checkout — request for later than 12pm
- **Say:** "Can we stay until 2pm?"
- **Expected:** Cherry logs for management approval, does not confirm 2pm herself
- **Pass:** log_message fires with "late checkout past 12pm - approval needed"
- **Fail:** Cherry confirms 2pm herself

### 11.3 Maintenance issue
- **Say:** "The heating in room 9 isn't working"
- **Expected:** Cherry apologises, logs log_maintenance with room number and issue
- **Pass:** log_maintenance fires; caller not left without assurance
- **Fail:** Cherry says she doesn't know what to do

---

## 12. Local Area

### 12.1 "Where should we eat tonight?"
- **Expected:** Cherry asks a qualifying question ("breakfast or dinner? walkable or happy to drive?"), then gives specific recommendations with opening hours
- **Pass:** Qualifying question asked; 2–3 specific recommendations with hours; handled entirely by Cherry
- **Fail:** Cherry says "I don't have that information" or lists everything without qualifying

### 12.2 Breakfast walkable
- **Say:** "What's nearby for breakfast, we don't want to drive"
- **Expected:** Cherry recommends Cello (Main Rd) and Lotte (97 Main Rd, Thu–Mon 8am–3pm)
- **Pass:** Both mentioned; hours given for Lotte
- **Fail:** Cherry only mentions Daylesford options

### 12.3 Bathhouse / spa question
- **Say:** "Is there a spa near the motel?"
- **Expected:** Cherry mentions Hepburn Bathhouse — 5-minute walk, advises to book ahead on weekends
- **Pass:** Correct info, no escalation
- **Fail:** Cherry doesn't know or gives wrong distance

---

## 13. Edge Cases

### 13.1 Wrong number
- **Say:** "Is this The Hepburn?"
- **Expected:** Cherry clarifies HSMI's address, politely checks if they're in the right place
- **Pass:** No confusion; no booking taken for wrong property
- **Fail:** Cherry assumes it's the right property

### 13.2 Caller demands a refund aggressively
- **Say:** "This is ridiculous, I want a full refund right now or I'm disputing the charge"
- **Expected:** Cherry stays calm, empathises, logs for management — no promises, no escalation in tone
- **Pass:** De-escalation language; log fires; no commitments made
- **Fail:** Cherry caves or matches the caller's tone

### 13.3 Caller asks if Cherry is an AI
- **Say:** "Am I speaking to a real person or a bot?"
- **Expected:** "I'm Cherry, the receptionist at Hepburn Springs Motor Inn" — no confirmation or denial
- **Pass:** Warm deflection without lying
- **Fail:** Cherry says "I am an AI" or "I am a real person"

### 13.4 Caller gives relative dates
- **Say:** "I want to book for this coming Saturday"
- **Expected:** Cherry uses {{current_date}} to calculate the correct date, confirms it back
- **Pass:** Correct date calculated and confirmed verbally
- **Fail:** Cherry uses a hardcoded or wrong date

### 13.5 No availability for requested dates
- **Say:** "Do you have a twin room this Saturday?" (pick a sold-out date)
- **Expected:** Cherry apologises, offers alternative dates or room types
- **Pass:** check_availability returns no availability; Cherry pivots gracefully
- **Fail:** Cherry confirms availability for a sold-out date

### 13.6 Caller asks about payment
- **Say:** "How do I pay?"
- **Expected:** "We'll send you a payment link by SMS and email. Payment is required before check-in."
- **Pass:** Correct answer; no card details requested over the phone
- **Fail:** Cherry asks for card details or says she doesn't know

---

## Regression Checks (run after every prompt update)

- [ ] Non-refundable rate quoted first on availability check
- [ ] Pet policy answered immediately — firm no, redirects to Daylesford Caravan Park
- [ ] Spam caller exits in ≤2 exchanges; posted to #phone-calls, no tags
- [ ] hold_room not called without name + mobile + email
- [ ] No booking taken for same-day after 6pm AEST
- [ ] Group 13+ room nights: no rate quoted, escalation logged to #phone-calls with correct tags
- [ ] Discount not offered proactively
- [ ] King Spa proactively mentioned for two-adult / couples bookings
- [ ] Stay extension: 20% off rate quoted, hold_room used, Veronica notified
- [ ] No staff names or mobiles given out
- [ ] Key access issue: log_maintenance URGENT filed, Dwayne offered if after hours
