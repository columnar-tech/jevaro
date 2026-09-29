from typesafe_sdk import Noul, TypeSafeClient

with TypeSafeClient() as client:  # Reads TYPESAFE_API_KEY from the environment.
    response = client.system_one(
        model="jev-latest",
        state="The shoes don't fit. I'd like my money back.",
        questions={
            "refund": Noul(instructions="Is the customer requesting a refund?"),
        },
    )

print("Probability of a refund request:", response.nouls["refund"].noul)
