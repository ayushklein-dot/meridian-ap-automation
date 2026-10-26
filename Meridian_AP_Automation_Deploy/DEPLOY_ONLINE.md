# Meridian AP Automation Control Center — Online Deployment Package

## Deploy online (shareable URL)

This package is prepared for Streamlit Community Cloud. You must sign in with your own GitHub and Streamlit accounts; I cannot publish into your accounts or create a live URL without your account access.

### A. Upload the project to GitHub
1. Sign in at https://github.com/ and create a **new public repository** (for example `meridian-ap-automation`).
2. On the repository page choose **Add file → Upload files**.
3. Upload the contents of this folder (not the ZIP itself). Make sure `app.py`, `workflow_engine.py`, `langgraph_runtime.py`, and `requirements.txt` are at the repository root.
4. Click **Commit changes**.

### B. Deploy with Streamlit Community Cloud
1. Open https://share.streamlit.io/ and sign in using GitHub.
2. Choose **Create app** / **Deploy an app**.
3. Select your repository, branch (usually `main`) and main file path `app.py`.
4. Choose **Deploy** and wait for installation to finish.
5. Copy the generated `https://....streamlit.app` URL to share with your professor.

### C. Before sharing
- Open the deployed URL in a private/incognito browser to test it.
- Try the Overview, workflow lab, exception workbench, approvals, payment planner, reconciliation lab and audit export.
- Do not enter real supplier, tax, bank, personal or confidential data.
- This is a synthetic coursework prototype. GSTIN checks are format-only; no live GST portal, OCR, ERP, email, bank or payment integration exists.
- Human approval remains required; the app does not release real payments.
- The hosted app's local SQLite file may reset when the app is rebuilt/restarted and is not a durable shared database. Treat the deployed app as a demonstration only.

## Run locally
```bash
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

## Tests
```bash
python -m unittest test_meridian_rules.py test_workflow_engine.py test_langgraph_runtime.py -v
```

The current test suite was run before packaging: 64 tests passed.
