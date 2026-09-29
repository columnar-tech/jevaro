from typesafe_sdk import Choice, Noul, Score, TypeSafeClient

QUESTIONS = {
    "department": Choice(
        instructions="Which team should handle this customer message?",
        criteria={
            "returns": "Return, exchange, or refund requests",
            "shipping": "Delivery status or missing packages",
            "billing": "Incorrect charges, invoices, or payment errors",
            "other": "None of these teams fits the request",
        },
    ),
    "urgency": Score(
        instructions="How soon does the customer want a resolution?",
        criteria=[
            "Customer says the issue can wait or gives no deadline.",
            "Customer asks for resolution within the next few days.",
            "Customer asks for resolution today or immediately.",
        ],
    ),
    "refund": Noul(instructions="Is the customer requesting a refund?"),
}


def main():
    with TypeSafeClient() as client:  # Reads TYPESAFE_API_KEY from the environment.
        response = client.system_one(
            model="jev-latest",
            state="The shoes don't fit. Please refund my money today.",
            questions=QUESTIONS,
        )

    # Choice: one label, plus confidence and probabilities for every option.
    department = response.choices["department"]
    print("Choice (department):", department.choice)
    print("  Confidence:", department.confidence)
    print("  Probabilities:", department.probabilities)

    # Score: a weighted position on the levels above (0 to 2, possibly fractional).
    urgency = response.scores["urgency"]
    print("\nScore (urgency, 0 to 2):", urgency.score)
    print("  Confidence:", urgency.confidence)
    print("  Probabilities:", urgency.probabilities)
    print("  Levels:", urgency.legend)

    # Noul: probability of yes, from 0 to 1; no separate confidence field.
    print("\nNoul (probability of a refund request):", response.nouls["refund"].noul)


if __name__ == "__main__":
    main()
