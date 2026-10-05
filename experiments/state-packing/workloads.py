"""The two workloads: plain-text messages, and structured tickets that questions address by path."""

import random

import corpus
from packing import Request

MESSAGE_QUESTIONS = {
    "department": {
        "type": "choice",
        "instructions": "Which team should handle this customer message?",
        "criteria": {
            "returns": "Return, exchange, or refund requests",
            "shipping": "Delivery status or missing packages",
            "billing": "Incorrect charges, invoices, or payment errors",
            "other": "None of these teams fits the request",
        },
    },
    "urgency": {
        "type": "score",
        "instructions": "How soon does the customer want a resolution?",
        "criteria": [
            "Customer says the issue can wait or gives no deadline.",
            "Customer asks for resolution within the next few days.",
            "Customer asks for resolution today or immediately.",
        ],
    },
    "refund": {"type": "noul", "instructions": "Is the customer requesting a refund?"},
}

# Every way a question can refer to its state: a nested path, two paths, a path
# in structured instructions, structured criteria, no path at all, and a
# backticked name that belongs to the instructions rather than the state.
TICKET_QUESTIONS = {
    "department": {
        "type": "choice",
        "instructions": "Which team should handle the request in `ticket.messages[0].text`?",
        "criteria": {
            "returns": {"what": "Returns, exchanges, or refunds", "not_for": "Delivery status or charges"},
            "shipping": {"what": "Delivery status, delays, or missing packages", "not_for": "Returns of delivered items"},
            "billing": {"what": "Incorrect charges, invoices, or payment errors", "not_for": "Refunds for returned items"},
            "other": {"what": "Product questions, feedback, or anything else"},
        },
    },
    "urgency": {
        "type": "score",
        "instructions": {
            "question": "How soon does the customer want a resolution?",
            "inspect": "`ticket.messages[0].text`",
            "note": "Judge only the customer's words, not the agent's reply.",
        },
        "criteria": [
            {"summary": "No deadline", "signals": ["Says it can wait", "Gives no timing at all"]},
            {"summary": "Within a few days", "signals": ["Asks for a reply this week", "Mentions the next couple of days"]},
            {"summary": "Today or immediately", "signals": ["Says it's urgent", "Asks for an answer today or within hours"]},
        ],
    },
    "refund": {
        "type": "noul",
        "instructions": "Does `ticket.messages[0].text` ask for a refund?",
        "criteria": {
            "true": {"what": "Asks for money back, in full or in part", "examples": ["Please refund my order"]},
            "false": {
                "what": "No refund requested, or prefers a replacement, repair, or store credit",
                "examples": ["Please send a replacement", "Can I get store credit instead?"],
            },
        },
    },
    "item_matches": {
        "type": "noul",
        "instructions": "Is the product the customer writes about in `ticket.messages[0].text` the same product as `order.items[0].name`?",
    },
    "tone": {
        "type": "choice",
        "instructions": "How does the customer come across?",
        "criteria": {"calm": None, "frustrated": None, "angry": None},
    },
    "policy_covers": {
        "type": "noul",
        "instructions": {
            "policy": "Unused items can be returned within 30 days of delivery for a full refund.",
            "question": "Is what the customer asks for in `ticket.messages[0].text` covered by `policy`?",
        },
    },
}

AGENT_REPLIES = [
    "Thanks for getting in touch. I'm looking into this now.",
    "Sorry about this. Could you confirm the email address on the order?",
    "I've passed this to the right team, and they'll reply by email.",
    "Refunds normally reach your account 3 to 5 working days after approval.",
    "Our returns policy allows 30 days from delivery for unused items.",
    "Thanks for your patience while we check with the warehouse.",
]
STATUSES = ["processing", "shipped", "in transit", "delivered", "delivered", "delivered"]
ALL_PRODUCTS = sorted({name for names in corpus.PRODUCTS.values() for name in names})


def messages():
    """The 10,000 messages from Jevaro's live benchmarks, as plain-text states."""
    return [Request(text, MESSAGE_QUESTIONS) for text, *_ in corpus.generate()]


def messages_wrapped():
    """Diagnostic: the messages with each question's instructions wrapped as {"question": ...}."""
    questions = {key: {**q, "instructions": {"question": q["instructions"]}} for key, q in MESSAGE_QUESTIONS.items()}
    return [Request(text, questions) for text, *_ in corpus.generate()]


def _mentioned_product(text):
    lowered = text.lower()
    found = [name for name in ALL_PRODUCTS if name.lower() in lowered]
    return max(found, key=len) if found else None


def tickets(seed=20261005):
    """The same 10,000 messages as structured tickets, with an order and a customer record."""
    rng = random.Random(seed)
    out = []
    for number, (text, *_) in enumerate(corpus.generate()):
        product = _mentioned_product(text)
        if product is None or rng.random() < 0.3:
            ordered = rng.choice([name for name in ALL_PRODUCTS if name != product])
        else:
            ordered = product
        items = [{"name": ordered, "quantity": rng.randint(1, 3)}]
        if rng.random() < 0.25:
            items.append({"name": rng.choice(ALL_PRODUCTS), "quantity": 1})
        state = {
            "ticket": {
                "id": f"T-{100000 + number}",
                "channel": rng.choice(["email", "chat", "web form"]),
                "messages": [
                    {"from": "customer", "text": text},
                    {"from": "agent", "text": rng.choice(AGENT_REPLIES)},
                ],
            },
            "customer": {"tier": rng.choice(["standard", "plus", "business"]), "orders_last_year": rng.randint(0, 40)},
            "order": {"id": f"TS-{rng.randint(10000, 99999)}", "status": rng.choice(STATUSES), "items": items},
        }
        out.append(Request(state, TICKET_QUESTIONS))
    return out


WORKLOADS = {"messages": messages, "messages_wrapped": messages_wrapped, "tickets": tickets}
