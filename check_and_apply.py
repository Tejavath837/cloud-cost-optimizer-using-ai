
import hashlib
import json
import sys
from pathlib import Path

OUT=Path("../terraform_out")
try:
    approval=json.loads((OUT / "approval.json").read_text())
    current=hashlib.sha256((OUT / "changes.tf").read_text().encode()).hexdigest()
except FileNotFoundError:
    print("No approval found. Run pipeline.py first."); sys.exit(1)

if not approval.get("approved") or approval["fingerprint"]!=current:
    print("STOP: changes.tf is missing approval or was edited after approval."); sys.exit(1)

print("OK: file matches what you approved. Now, in terraform_out, run by hand:")
print("  terraform init")
print("  terraform plan      <- read it. Does it match what you approved?")
print("  terraform apply     <- type 'yes' only if the plan is what you expect")
