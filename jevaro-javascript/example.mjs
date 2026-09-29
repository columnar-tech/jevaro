import { TypeSafeClient, choice, score, noul } from "jevaro";

const client = new TypeSafeClient();
const reader = await client.systemOne({
  states: [
    "The shoes don't fit. Please refund my money today.",
    "My parcel is marked delivered but isn't here. Please help me find it.",
    "I was charged twice for my coffee maker. Please fix the duplicate charge this week.",
  ],
  questions: {
    department: choice("Which team should handle this customer message?", {
      returns: "Returns, exchanges, refunds", shipping: "Delivery problems",
      billing: "Charges and payments", other: "None of these teams fits",
    }),
    urgency: score("How soon does the customer want a resolution?", [
      "No deadline or can wait", "Within the next few days", "Today or immediately",
    ]),
    refund: noul("Is the customer requesting a refund?"),
  },
});

try {
  const metadata = reader.schema.fields.find(f => f.name === "department").metadata;
  const { labels } = JSON.parse(metadata.get("ARROW:extension:metadata"));
  for await (const batch of reader) {
    for (const row of batch) {
      console.log(JSON.stringify({
        department: labels[row.department.choice], urgency: row.urgency.score, refund: row.refund,
      }));
    }
  }
} finally {
  await reader.cancel();
}
