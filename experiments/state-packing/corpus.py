"""The synthetic customer-message corpus used by Jevaro's live benchmarks (no API calls).

Copied from the generator behind the 10,000-state benchmark runs so these
experiments are self-contained. `generate()` returns the same 10,000 messages in
the same order.
"""

import random
import re

SEED = 20260928
PER_SCENARIO = 200

PRODUCTS = {
    "clothes": ["linen shirt", "rain jacket", "wool sweater", "running top", "winter coat", "cotton hoodie", "work shirt", "fleece vest"],
    "shoes": ["running shoes", "walking boots", "sandals", "trainers", "work boots", "slippers", "hiking shoes", "loafers"],
    "fragile": ["ceramic mug", "glass jug", "serving bowl", "table lamp", "picture frame", "tea pot", "vase", "wall mirror"],
    "electronics": ["wireless keyboard", "desk fan", "coffee grinder", "Bluetooth speaker", "phone charger", "electric toothbrush", "webcam", "reading light"],
    "assembly": ["bookshelf", "desk chair", "shoe rack", "side table", "standing desk", "storage cabinet", "bedside table", "plant stand"],
    "general": ["backpack", "water bottle", "lunch box", "yoga mat", "notebook", "travel bag", "umbrella", "picnic blanket", "pencil case", "phone case", "cushion cover", "bath towel"],
    "subscription": ["coffee subscription", "pet-food delivery plan", "razor refill plan", "tea subscription", "monthly craft box", "snack subscription"],
}

# Each scenario couples a product family, a specific problem, and compatible
# requests. This avoids nonsense such as a broken zipper on a coffee grinder.
# These groups describe how data was authored; they are NOT ground-truth labels.
SCENARIOS = [
    ("returns", "clothing_fit", "clothes",
     ["The {item} fits much tighter than the measurements on your site.", "I tried on the {item} at home and the fit isn't right; the tags are still attached."],
     ["Can I exchange it for a larger size?", "I'd like to send it back for a refund.", "What are my options if the next size is unavailable?"]),
    ("returns", "shoe_fit", "shoes",
     ["The {item} pinch at the toes even though I ordered my usual size.", "I only tried the {item} on indoors, but the heels keep slipping."],
     ["Please help me arrange a refund.", "Could I swap them for another size instead of getting my money back?", "Do I need to pay postage to send these back?"]),
    ("returns", "wrong_color", "general",
     ["I ordered the {item} in {color}, but the one in the parcel is {other_color}.", "The receipt says {color}; the {item} you sent is definitely {other_color}."],
     ["Could you send the color I ordered?", "I'd rather return it and get a refund.", "Please tell me how an exchange would work."]),
    ("returns", "broken_on_arrival", "fragile",
     ["The {item} arrived cracked, although the outer box looks fine.", "I opened the parcel and found the {item} broken, with loose pieces in the wrapping."],
     ["Please replace it; I don't want a refund.", "I want my money back, including the delivery charge.", "Can you tell me what photos you need to sort this out?"]),
    ("returns", "missing_parts", "assembly",
     ["The {item} came without the bag of screws listed in the instructions.", "I'm assembling the {item}, but a bracket is missing from the sealed parts pack."],
     ["Can you send just the missing parts?", "I'd prefer to return the whole thing for a refund.", "I don't need a refund if you can supply the missing pieces."]),
    ("returns", "stopped_working", "electronics",
     ["The {item} stopped turning on after {days} days of normal use.", "My {item} switches off on its own. I've followed the troubleshooting guide already."],
     ["I'd like a replacement under the warranty.", "Please arrange a return and refund.", "Is a repair possible, or should I send it back?"]),
    ("returns", "changed_mind", "general",
     ["I bought the {item}, but I've decided I don't need it. It's still unopened.", "The {item} is fine; it just isn't what I wanted once I saw it in person."],
     ["Am I able to return it for store credit?", "Could I get a refund to the original payment method?", "Can you explain the return process?"]),
    ("returns", "wrong_item", "general",
     ["I ordered the {item}, but the parcel contains somebody else's product.", "The packing slip lists the {item} I bought, but the item inside doesn't match it at all."],
     ["Please arrange to send the correct item.", "I don't want another delivery; please refund the order.", "Do you need the wrong item back before you can replace it?"]),
    ("returns", "return_label", "clothes",
     ["I've started a return for the {item}, but the label link opens a blank page.", "The prepaid label for returning my {item} won't scan at the drop-off shop."],
     ["Please email me a working label.", "Could you arrange a printer-free return?", "I still want the refund, but I need a usable label first."]),
    ("returns", "late_return", "general",
     ["The return window for my unopened {item} closed {days} days ago while I was away.", "I meant to return the {item}, but I missed the deadline because I was ill."],
     ["Would you consider an exception for a refund?", "Could you offer store credit instead?", "Is an exchange still possible?"]),
    ("returns", "gift_exchange", "clothes",
     ["I received the {item} as a gift, but it doesn't fit and I don't have the payment card.", "A relative bought me the {item}; I have the gift receipt but need a different size."],
     ["I'd like an exchange without notifying the person who bought it.", "Can you issue store credit to me?", "Can a refund go back to the buyer if an exchange isn't possible?"]),
    ("returns", "replacement_fault", "electronics",
     ["The replacement {item} has the same fault as the first one.", "I've now received two faulty versions of the {item}. I'm really disappointed."],
     ["I don't want a third one. Please refund me.", "Could someone check a replacement before sending it?", "Please tell me whether I can choose a different model."]),
    ("returns", "wear_fault", "clothes",
     ["A seam on the {item} split after {days} days, without catching on anything.", "The fastening on my {item} broke during normal use. I've barely worn it."],
     ["Could you replace it?", "I'd like to return it for a refund.", "Does the warranty cover this sort of fault?"]),
    ("returns", "partial_return", "general",
     ["I bought {quantity} of the {item} but only need {received}; the spare is unopened.", "I want to keep most of my order and return one unused {item}."],
     ["Can you refund just that one item?", "How do I create a return for part of the order?", "Could I exchange the spare for a different product?"]),
    ("shipping", "delivered_missing", "general",
     ["Tracking says the {item} was delivered, but I've checked the porch and neighbours and can't find it.", "The delivery photo for my {item} shows a doorway that isn't mine."],
     ["Can you investigate where the parcel went?", "Please send a replacement if it can't be located.", "If it's lost, I'd prefer a refund."]),
    ("shipping", "tracking_stalled", "electronics",
     ["The tracking for my {item} hasn't changed in {days} days.", "My {item} is still showing as being at the sorting depot, with no delivery estimate."],
     ["Could you check this with the carrier?", "Please give me an updated delivery date.", "Can I cancel for a refund if it hasn't actually shipped?"]),
    ("shipping", "label_only", "general",
     ["The {item} was marked shipped, but the carrier only shows 'label created'.", "I got a dispatch email for the {item}, but the courier says it hasn't received the parcel."],
     ["Has it physically left your warehouse?", "Please check when the carrier will collect it.", "If it hasn't left, can you refund me instead?"]),
    ("shipping", "address_correction", "clothes",
     ["I placed an order for the {item} and noticed that I left the apartment number off the address.", "The delivery address on my {item} order is my old address."],
     ["Can you correct it before dispatch?", "Could the carrier hold it at a collection point?", "Please pause dispatch while I send you the correct address."]),
    ("shipping", "partial_delivery", "general",
     ["I ordered {quantity} units of the {item}, but only {received} arrived.", "The order page shows the whole order delivered, but one {item} is missing from the box."],
     ["Is the rest coming in a separate parcel?", "Could you send the missing item?", "Please refund the missing item if it isn't available."]),
    ("shipping", "missed_delivery", "assembly",
     ["The carrier says nobody was home for my {item} delivery, but I was here the whole time.", "The driver didn't ring the bell when attempting to deliver my {item}."],
     ["Could you arrange another delivery attempt?", "Can I choose a collection location instead?", "Please ask the driver to call when they arrive."]),
    ("shipping", "delivery_instructions", "general",
     ["The building entrance is locked, and I'm expecting the {item} to be delivered.", "The courier needs to use the side entrance when bringing my {item}."],
     ["Can you add a note to the delivery instructions?", "Please ask them not to leave it on the pavement.", "Can the parcel go to a pickup locker instead?"]),
    ("shipping", "expedite", "clothes",
     ["I chose standard delivery for the {item}, but my plans have changed.", "My {item} hasn't shipped yet and I need to change the delivery service."],
     ["Is it possible to pay the difference for express shipping?", "Can you tell me the earliest realistic arrival date?", "Please check whether collection from your warehouse is possible."]),
    ("shipping", "customs", "general",
     ["My {item} is being held by customs, and the carrier says an invoice is missing.", "The tracking page says customs needs more information before releasing my {item}."],
     ["Could you provide the paperwork they need?", "Who should I contact to get this moving?", "Can you check whether the shipment can still be delivered?"]),
    ("shipping", "returned_to_sender", "electronics",
     ["The parcel containing my {item} is marked 'return to sender'; I never refused it.", "The carrier sent my {item} back after it sat at a collection point I wasn't told about."],
     ["Can you send it out again?", "I'd rather have a refund than another delivery attempt.", "Please explain what happens next."]),
    ("shipping", "lost_confirmation", "general",
     ["I ordered the {item} but never received a confirmation or tracking email.", "I checked my spam folder and still can't find any dispatch information for the {item}."],
     ["Can you confirm whether the order is on its way?", "Please resend the tracking details.", "Could you tell me how to look up the order without the email?"]),
    ("shipping", "delivery_slot", "assembly",
     ["The delivery slot for my {item} clashes with a work shift.", "The carrier has given an all-day window for the {item}, and I need to plan around it."],
     ["Can we move the delivery to another day?", "Is a narrower time window available?", "Could the driver call before setting off?"]),
    ("billing", "duplicate_charge", "general",
     ["My bank shows two completed charges of ${price} for one {item}.", "The {item} arrived once, but I've been billed twice for the same purchase."],
     ["Please refund the duplicate charge.", "Can someone check why there are two charges?", "I want to keep the item; only the extra payment needs reversing."]),
    ("billing", "pending_hold", "electronics",
     ["There is a pending card payment as well as a settled payment for the {item}.", "I see an authorisation hold for my {item} even though the actual payment has gone through."],
     ["When will the hold disappear?", "Can you confirm I haven't paid twice?", "Please release the unused authorisation if you can."]),
    ("billing", "promo_not_applied", "clothes",
     ["The discount code was accepted for the {item}, but the receipt shows full price.", "The checkout total for my {item} changed after I applied the promotion."],
     ["Could you refund the discount difference?", "Can you explain why the offer didn't apply?", "I want to keep the order but have the price corrected."]),
    ("billing", "invoice_details", "electronics",
     ["I need an invoice for the {item} with my company name on it.", "The invoice for the {item} has the wrong billing details."],
     ["Please send a corrected invoice.", "Can this be updated without cancelling the order?", "Could you send an itemised receipt for expenses?"]),
    ("billing", "failed_payment", "general",
     ["Payment for the {item} failed at checkout, but the money appears to have left my account.", "Your page said the {item} order wasn't completed, while my bank shows a transaction."],
     ["Please confirm whether I should place the order again.", "If there is no order, can you return the payment?", "Can you help me avoid paying twice?"]),
    ("billing", "renewal_after_cancel", "subscription",
     ["I cancelled my {item}, but another renewal payment was taken.", "My {item} account shows cancelled, yet I have a new charge on my statement."],
     ["Please refund that renewal and stop future charges.", "Could you check whether the cancellation was recorded?", "I need confirmation that this won't keep happening."]),
    ("billing", "refund_not_received", "clothes",
     ["You approved the refund for my {item} {days} days ago, but it hasn't reached my bank.", "The return of my {item} is marked complete, yet the refund is still missing."],
     ["Can you check the payment status?", "Please send the refund transaction reference.", "Could you explain which payment method it was returned to?"]),
    ("billing", "shipping_fee", "general",
     ["My {item} order qualified for free delivery, but the receipt includes a delivery fee.", "I was charged for express delivery on the {item} even though I selected standard shipping."],
     ["Please refund the extra delivery charge.", "Could you explain the amount on the receipt?", "Can you correct this without changing the order?"]),
    ("billing", "gift_card_balance", "general",
     ["I used a gift card for the {item}, but checkout failed and the gift card balance is now lower.", "The {item} order was cancelled, but the amount hasn't been put back on my gift card."],
     ["Please restore the balance.", "Can you trace where the gift card funds went?", "Could you help me use the balance on another order?"]),
    ("billing", "unrecognised_charge", "subscription",
     ["There's a charge labelled as your {item}, but I don't recognise signing up.", "I found a payment for the {item} and can't find a matching order in my account."],
     ["Please explain what this payment was for.", "I want the charge investigated and refunded if it wasn't authorised.", "Can you help me find which account is being billed?"]),
    ("other", "care_instructions", "clothes",
     ["The care label on my {item} is difficult to read.", "I'd like to know the recommended way to clean the {item}."],
     ["Is machine washing safe?", "Could you send the care instructions in text?", "Which cleaning products should I avoid?"]),
    ("other", "product_dimensions", "general",
     ["The listing for the {item} gives two different sets of dimensions.", "I'm considering the {item}, but I need the exact size before ordering."],
     ["Could you confirm the measurements?", "Does the listed size include the packaging?", "Can someone measure one rather than repeating the listing?"]),
    ("other", "restock", "clothes",
     ["The {item} in {color} is sold out in the size I need.", "I've been checking for the {color} {item}, but it still isn't available."],
     ["Do you expect another restock?", "Can I sign up for a stock notification?", "Is the same version available in any of your shops?"]),
    ("other", "account_login", "general",
     ["I'm trying to look at the {item} in my saved list, but my account won't let me log in.", "The password reset email never arrives, so I can't access the account where I saved the {item}."],
     ["Can you help me regain access?", "Is there another way to reset my password?", "Could you check whether the reset service is working?"]),
    ("other", "accessibility", "general",
     ["My screen reader can't read the options on the {item} page.", "The size selector on the {item} listing can't be operated with a keyboard."],
     ["Could you provide an accessible way to choose the options?", "Please pass this on to whoever maintains the website.", "Can somebody help me place an order without using that control?"]),
    ("other", "materials", "general",
     ["The {item} description doesn't give a full list of materials.", "I'd like more information about the materials used in the {item}."],
     ["Can you share a full materials list?", "Does this contain any animal-derived materials?", "Is there a product specification sheet I can read?"]),
    ("other", "wholesale", "general",
     ["I'm looking at buying {bulk_quantity} units of the {item} for our community group.", "Our small shop would like to stock the {item}."],
     ["Do you have trade pricing?", "Who should I speak to about a bulk order?", "Could you explain minimum order quantities?"]),
    ("other", "thank_you", "general",
     ["The {item} arrived and I'm really pleased with it. Your support team was helpful too.", "Just a quick note to say the {item} is exactly what I hoped for."],
     ["Please pass my thanks to the team.", "Where is the best place to leave a review?", "No action needed; I wanted to send some positive feedback."]),
    ("other", "unsubscribe", "general",
     ["Since I bought the {item}, I've been getting more marketing emails than I want.", "I keep seeing promotional messages for the {item} after unsubscribing."],
     ["Please remove me from marketing emails.", "Can I keep order updates but stop promotions?", "Could you fix the unsubscribe link?"]),
    ("other", "assembly_help", "assembly",
     ["All the parts for my {item} are here, but I don't understand step four.", "The diagram for the {item} is too small to see which way the bracket faces."],
     ["Could you send a clearer diagram?", "Is there an assembly video?", "Please explain that step; I don't need a replacement or refund."]),
    ("mixed", "delivery_and_charge", "general",
     ["My {item} still hasn't arrived, and my card also shows two settled charges.", "I have no delivery update for the {item}, plus a duplicate payment on my statement."],
     ["Please track down the parcel and refund the extra charge.", "Could one person help with both the delivery and the payment?", "I'd like to cancel for a full refund if you can't locate it."]),
    ("mixed", "changed_resolution", "clothes",
     ["In my previous message about the {item}, I asked for a refund. I've changed my mind.", "The return form for the {item} says 'refund', but that isn't what I want now."],
     ["Please exchange it for another size instead.", "Can you stop the return? I've decided to keep it.", "I'd prefer store credit if that's possible."]),
    ("mixed", "conditional_refund", "electronics",
     ["I'm still waiting for a replacement {item}, and nobody has confirmed whether one is in stock.", "Support promised to replace the faulty {item}, but I haven't had an update."],
     ["If you can replace it, great; otherwise I'd like a refund.", "Please keep the replacement request open. I don't want my money back yet.", "Can you tell me my options before I choose between a replacement and a refund?"]),
    ("mixed", "policy_question", "general",
     ["I'm thinking about buying the {item}, and the refund policy is unclear to me.", "Before I order the {item}, I want to understand what would happen if it wasn't suitable."],
     ["Would it qualify for a refund after opening the packaging?", "Is return shipping free, or would that come out of a refund?", "Could you explain the difference between a refund and store credit?"]),
]

OPENINGS = ["Hi.", "Hello!", "Hi there,", "Hello support,", "Good morning.", "Hi team,", "Hey,", "Hello again,", "I'd appreciate some help.", "Could someone help with this?", "Sorry to bother you,", "Quick question:"]
CLOSINGS = ["Thanks.", "Thank you for your help.", "I'd appreciate an update.", "Please let me know.", "Thanks in advance!", "I'm not sure who else to contact.", "I hope we can sort this out.", "Any guidance would be appreciated."]
CONTEXT = ["I tried the help page before writing.", "Email is easier for me than a phone call.", "Please keep the reply in this thread.", "I can provide more details if needed.", "I'm writing from my phone, so apologies if this is unclear.", "The automated help didn't cover my question.", "This is my first time contacting your team.", "I've had good experiences with you before."]
TIMING = {
    "unspecified": [""],
    "unhurried": ["No rush; next week is fine.", "This isn't urgent.", "Whenever you get a chance is fine.", "I'm happy to wait until next week for a reply.", "There's no deadline on my side."],
    "soon": ["Could you get back to me within the next couple of days?", "An answer in two or three days would help.", "Please try to sort this out in the next few days.", "I can wait a few days, but I'd like an update then.", "Could someone reply by the end of this week?"],
    "today": ["Please respond today.", "I need this sorted out before the end of today.", "Could someone look into this straight away?", "I need an answer within the next hour.", "Please treat this as urgent; I can't wait until tomorrow."],
}


def generate(seed=SEED):
    rng = random.Random(seed)
    rows = []
    seen = set()
    for group, name, family, problems, requests in SCENARIOS:
        added = 0
        while added < PER_SCENARIO:
            color, other_color = rng.sample(["navy", "green", "black", "grey", "red", "cream"], 2)
            quantity = rng.randint(2, 6)
            slots = dict(item=rng.choice(PRODUCTS[family]), color=color,
                         other_color=other_color, quantity=quantity, received=quantity - 1,
                         days=rng.choice([2, 3, 4, 5, 7, 8, 10, 12, 14]),
                         price=f"{rng.randint(12, 190)}.{rng.choice([0, 25, 49, 50, 95, 99]):02d}",
                         bulk_quantity=rng.choice([20, 25, 40, 50, 75, 100, 150]))
            timing = rng.choices(list(TIMING), weights=[4, 2, 2, 2])[0]
            problem = rng.choice(problems).format(**slots)
            request = rng.choice(requests)
            parts = []
            if rng.random() < 0.55:
                parts.append(rng.choice(OPENINGS))
            parts.append(problem)
            if rng.random() < 0.25:
                parts.append(rng.choice(CONTEXT))
            # Occasionally the request leads the message, as in real support mail.
            if rng.random() < 0.18:
                parts.insert(0, request)
            else:
                parts.append(request)
            # A thank-you saying no action is needed must not demand an urgent reply.
            if "No action needed" in request:
                timing = "unspecified"
            deadline = rng.choice(TIMING[timing])
            if deadline:
                parts.append(deadline)
            if group in {"returns", "shipping", "billing"} and rng.random() < 0.15:
                parts.append(f"Order reference: {rng.choice(['TS', 'WEB', 'ORD'])}-{rng.randint(10000, 99999)}.")
            if rng.random() < 0.35:
                closings = ["Thanks.", "Best wishes to the team."] if "No action needed" in request else CLOSINGS
                parts.append(rng.choice(closings))
            text = " ".join(parts)
            style = rng.random()
            if style < 0.10:
                text = text.lower()
            elif style < 0.16:
                text = text.replace("Please", "Pls").replace("please", "pls").replace("I'm", "Im")
            # Enforce uniqueness even after removing reference numbers, casing,
            # and punctuation: IDs alone must never manufacture a new message.
            identity = re.sub(r"Order reference:.*?(?:\.|$)", "", text, flags=re.I)
            identity = re.sub(r"[^a-z0-9 ]", "", identity.lower())
            identity = " ".join(identity.split())
            if identity in seen:
                continue
            seen.add(identity)
            rows.append((text, group, name, timing))
            added += 1
    rng.shuffle(rows)
    return rows
