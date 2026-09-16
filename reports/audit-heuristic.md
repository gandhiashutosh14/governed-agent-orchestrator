# Orchestrator audit: -

20 objectives, auto-approve=on, 0.2 s.

| Metric | Value |
|---|---|
| Valid plan from the first planner | 20 / 20 |
| Planner fallbacks (heuristic used) | 0 |
| Answered | 20 / 20 |
| Step fallbacks fired | 0 |
| Approval gates raised | 3 |
| Median latency | 9 ms |

| # | Objective | Plan source | Steps | Status | Approvals | ms |
|---|---|---|---|---|---|---|
| 1 | Forecast next year's revenue from the yearly totals. | heuristic | 3 | completed | 0 | 16 |
| 2 | Which countries generate the most revenue? Show the top 5. | heuristic | 2 | completed | 0 | 8 |
| 3 | Show revenue by country for 2012, top 3. | heuristic | 2 | completed | 0 | 9 |
| 4 | What are the top 5 genres by tracks sold? | heuristic | 2 | completed | 0 | 8 |
| 5 | How many customers do we have per country? Top 10. | heuristic | 2 | completed | 0 | 8 |
| 6 | What is our refund policy for digital tracks? | heuristic | 2 | completed | 0 | 7 |
| 7 | Can a support agent issue a refund of 120 on their own? | heuristic | 2 | completed | 0 | 9 |
| 8 | What discount applies to an invoice with 25 line items? | heuristic | 2 | completed | 0 | 8 |
| 9 | Summarise revenue per year. | heuristic | 2 | completed | 0 | 7 |
| 10 | Forecast next year's revenue and send the report to finance@example.com. | heuristic | 4 | completed | 1 | 12 |
| 11 | Send the top 5 countries by revenue to the sales director. | heuristic | 3 | completed | 1 | 15 |
| 12 | What is the revenue trend over time and what does it project for next year? | heuristic | 3 | completed | 0 | 11 |
| 13 | How long do we keep customer contact details after an account closes? | heuristic | 2 | completed | 0 | 8 |
| 14 | Which genre sells the most and how many tracks was that? | heuristic | 2 | completed | 0 | 10 |
| 15 | Top 3 markets by revenue. | heuristic | 2 | completed | 0 | 9 |
| 16 | Are discounts allowed to be combined? | heuristic | 2 | completed | 0 | 9 |
| 17 | Predict revenue for the coming year and explain the method used. | heuristic | 3 | completed | 0 | 10 |
| 18 | Which 5 countries have the most customers? | heuristic | 2 | completed | 0 | 10 |
| 19 | Email the yearly revenue summary to reporting@example.com. | heuristic | 3 | completed | 1 | 13 |
| 20 | What must a forecast state before it can be circulated? | heuristic | 2 | completed | 0 | 7 |