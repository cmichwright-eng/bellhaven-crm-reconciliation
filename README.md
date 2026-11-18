# Bellhaven CRM reconciliation (Clipboard assessment)

Human-in-the-loop web-to-CRM reconciliation. The reviewer approves each write; there are no automatic CRM mutations.

## Run on a Mac

1. Install Python 3.10+.
2. Extract ZIP, then open Terminal in its folder.
3. `python3 -m venv .venv`
4. `source .venv/bin/activate`
5. `pip install -r requirements.txt`
6. `streamlit run app.py`
7. Paste your private CRM API token in the browser sidebar (do not commit it).
8. Tab 1: Fetch CRM and choose Bellhaven's **corporate account ID**.
9. Tab 2: Scrape facilities. Confirm coverage and inspect rows for malformed addresses. Generate proposals.
10. Tab 3: Inspect each proposal's evidence. Approve **only** sound changes; reject wrong ones. Approval immediately calls the API.
11. Verify approved changes in the read-only CRM browser via Tab 4.
12. Keep `decisions.sqlite3` locally; this tracks approvals and rejections across app restarts.

## API configuration

Endpoints follow the instructions' `/api/v1/accounts` convention; verify actual routes, PATCH method, and payload fields in `/api/docs` before approving. Override using environment variables listed in `.env.example`. JSON response shape can be inspected in Tab 1. The assessment website was unavailable for direct schema verification during development; adjust HTML extraction if the scrape count or addresses are wrong. If the API rejects a write, the UI displays the error and **does not** record an approval.

## Matching logic

- Normalized street address, ZIP, city/state, and fuzzy name score.
- Address + ZIP evidence dominates name-only similarity.
- Conflicting top candidates are flagged for manual matching.
- Existing wrong-parent accounts are protected by a fresh CHOW financial check.
- If both lifetime revenue and outstanding AR are positive, create a successor account under the right parent and point `chow_current_account` on old record to new ID; never change the old parent.
- Duplicate candidates only arise for identical address + ZIP with no financial history or debt, and require confirmation.
- Accounts under Bellhaven but missing from website are marked Needs Review with a note upon approval, not automatically deleted or reassigned.
- Stable SHA-256 proposal fingerprints are persisted in SQLite and suppress unchanged decisions.

## Important limitations / reviewer checklist

- Before approvals, **confirm real CRM JSON field names and endpoint methods in the interactive Swagger docs**. The initial implementation has configurable endpoints and common field aliases.
- The website may contain pagination or markup requiring adaptation. Check collected counts, addresses, care offerings, and source URLs; do not approve based on incomplete scrape.
- A write may succeed server-side but lose its response. Before retrying an account creation, inspect CRM to avoid duplicates.
- Matching based on street address can confuse co-located facilities; review proposals manually.
- The GitHub Actions daily schedule performs **read-only** scanning and uploads proposals, not writeback. Approval requires locally running Streamlit. A hosted system would need persistent decision storage and a queue sync.
- GitHub workflow needs repository secrets `BELLHAVEN_API_TOKEN` and `BELLHAVEN_PARENT_ID`. To keep it local, use `BELLHAVEN_PARENT_ID=... BELLHAVEN_API_TOKEN=... python daily_scan.py`.
- Exclude `.env`, `decisions.sqlite3`, and any exported CRM data from public source distribution. Keep the local SQLite file for the demo.

## Demo plan (2 minutes)

1. Show website scrape and account collection; identify Bellhaven parent.
2. Open a high-evidence proposal and explain the source and financial rule.
3. Approve a safe change; reject an incorrect or ambiguous proposal.
4. Open CRM browser and verify approved account fields actually changed.
5. Re-run matching; show decided proposals no longer appear.

## Actual outcome

Fill in after running: website locations captured __, CRM accounts __, approved __, rejected __, CHOW cases __, unresolved __, elapsed time __. Only report changes you actually made and verified.
