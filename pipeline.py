import hashlib
import json
import os
import sys
from pathlib import Path

import pandas as pd

HOURS_PER_MONTH=730
IDLE_PEAK=5
VOLUME_PRICE={"gp3": 0.08, "gp2": 0.10}
SNAPSHOT_PRICE=0.05
OLD_SNAPSHOT_DAYS=90
OUT=Path("../terraform_out")

RESOURCE_IDS = {"old-test": "i-0aaaaaaaaaaaaaaa1", "batch-job": "i-0bbbbbbbbbbbbbbb2","web-server": "i-0eeeeeeeeeeeeeee5", "half-busy": "i-0fffffffffffffff6",
"vol-03": "vol-0ccccccccccccccc3", "vol-04": "vol-0ccccccccccccccc7","snap-01": "snap-0ddddddddddddddd4", "snap-02": "snap-0ddddddddddddddd8",
}

def scan():
    inst=pd.read_csv(r"C:\Users\Rohith\PycharmProjects\JupyterProject\step9\instances.csv")
    cpu=pd.read_csv(r"C:\Users\Rohith\PycharmProjects\JupyterProject\step9\cpu.csv")
    summ=cpu.groupby("name")["cpu"].agg(["mean", "max"]).reset_index()
    data=inst.merge(summ, on="name")
    findings=[]
    for _,r in data.iterrows():
        findings.append({"resource": r["name"], "type": "instance","peak_cpu": int(r["max"]),"monthly_cost": round(r["rate_per_hour"] * HOURS_PER_MONTH, 2)})
    vol=pd.read_csv("volumes.csv")
    for _,r in vol[vol["attached_to"].isna()].iterrows():
        findings.append({"resource": r["volume_id"], "type": "volume", "attached": False,
                         "monthly_cost": round(r["size_gb"] * VOLUME_PRICE[r["type"]], 2)})
    snap=pd.read_csv("snapshots.csv")
    old=snap[(snap["volume_exists"] == "no") & (snap["age_days"] > OLD_SNAPSHOT_DAYS)]
    for _,r in old.iterrows():
        findings.append({"resource": r["snapshot_id"], "type": "snapshot",
                         "age_days": int(r["age_days"]), "source_volume_exists": False,
                         "monthly_cost": round(r["size_gb"] * SNAPSHOT_PRICE, 2)})
    return findings


def recommend_rules(findings):
    recs=[]
    for f in findings:
        if f["type"]=="instance":
            idle=f["peak_cpu"] < IDLE_PEAK
            recs.append({"resource": f["resource"], "action": "stop_instance" if idle else "no_action",
                         "risk": "low", "reason": "peak CPU below 5%" if idle else "healthy load"})
        elif f["type"]=="volume":
            recs.append({"resource": f["resource"], "action": "delete_volume",
                         "risk": "medium", "reason": "not attached to any server"})
        else:
            recs.append({"resource": f["resource"], "action": "delete_snapshot",
                         "risk": "medium", "reason": "source volume gone and older than 90 days"})
    return recs


def recommend_ai(findings):
    from typing import List,Literal
    from pydantic import BaseModel
    from google import genai

    class Rec(BaseModel):
        resource:str
        action:Literal["stop_instance","delete_volume","delete_snapshot","no_action"]
        risk:Literal["low","medium","high"]
        reason:str

    class Report(BaseModel):
        recommendations: List[Rec]

    prompt=("You are a cloud cost analyst. Recommend one action per resource. "
              "Instances with peak CPU below 5% are idle; above 30% are healthy. "
              "Unattached volumes and orphan snapshots are waste. Deleting data is never low risk. "
              "Include every resource exactly once. Use only facts from the data.\n\n"
              + json.dumps(findings, indent=2))
    client=genai.Client()
    out=client.interactions.create(
        model="gemini-3.8-flash", input=prompt,
        response_format={"type":"text","mime_type":"application/json",
                         "schema":Report.model_json_schema()})
    return [r.model_dump() for r in Report.model_validate_json(out.output_text).recommendations]


def recommend(findings):
    if os.environ.get("GEMINI_API_KEY"):
        try:
            print("[recommend] using the AI")
            return recommend_ai(findings)
        except Exception as e:
            print(f"[recommend] AI failed ({type(e).__name__}); falling back to rules")
    else:
        print("[recommend] no API key, using plain rules")
    return recommend_rules(findings)


def validate(recs, findings):
    known={f["resource"]: f for f in findings}
    seen=set()
    for r in recs:
        if r["resource"] not in known:
            raise ValueError(f"AI invented a resource: {r['resource']}")
        if r["resource"] in seen:
            raise ValueError(f"Duplicate recommendation: {r['resource']}")
        seen.add(r["resource"])
        t,a = known[r["resource"]]["type"], r["action"]
        allowed={"instance": {"stop_instance", "no_action"},
                   "volume": {"delete_volume", "no_action"},
                   "snapshot": {"delete_snapshot", "no_action"}}[t]
        if a not in allowed:
            raise ValueError(f"Action {a} not allowed for a {t}: {r['resource']}")
    missing=set(known) - seen
    if missing:
        raise ValueError(f"AI skipped: {sorted(missing)}")

def stop_instance(name,rid):
    label = name.replace("-", "_")
    return (f'resource "aws_ec2_instance_state" "{label}" {{\n'
            f'  instance_id = "{rid}"\n  state       = "stopped"\n}}\n')


def remove_block(kind):
    def make(name, rid):
        return (f"# ACTION: remove the {kind} block for {name} ({rid}) from your Terraform code.\n"
                f"# Back it up BEFORE deleting. Plan must show exactly: 1 to destroy.\n")
    return make


ACTIONS = {"stop_instance": stop_instance,"delete_volume": remove_block("aws_ebs_volume"),"delete_snapshot": remove_block("aws_ebs_snapshot")}


def build_changes(recs):
    parts=["# GENERATED - a human must review this before anything is applied.\n"]
    for r in recs:
        if r["action"] == "no_action":
            continue
        parts.append(f"# risk: {r['risk']} | {r['reason']}\n"
                     + ACTIONS[r["action"]](r["resource"], RESOURCE_IDS[r["resource"]]))
    return "\n".join(parts)

def fingerprint(text):
    return hashlib.sha256(text.encode()).hexdigest()


def approval_gate(recs, findings, changes_text):
    cost = {f["resource"]: f["monthly_cost"] for f in findings}   # money comes from OUR data
    print("\n" + "=" * 70)
    print("PROPOSED CHANGES - please review")
    print("=" * 70)
    total = 0
    for r in recs:
        if r["action"] == "no_action":
            continue
        total += cost[r["resource"]]
        print(f"{r['resource']:10} {r['action']:16} risk={r['risk']:6} "
              f"saves ${cost[r['resource']]:7.2f}  ({r['reason']})")
    print(f"\nTotal monthly saving: ${total:.2f}   |   per year: ${total * 12:.2f}")
    print("\nCaveats: CPU only (memory unchecked); deletes cannot be undone; back up first.")

    fp = fingerprint(changes_text)
    code = fp[:8]
    print(f"\nFile fingerprint: {code}")
    answer = input(f'To approve, type exactly:  APPROVE {code}   (anything else rejects): ').strip()
    if answer != f"APPROVE {code}":
        print("REJECTED. Nothing was approved.")
        return False
    (OUT / "approval.json").write_text(json.dumps({"fingerprint": fp, "approved": True}, indent=2))
    print("APPROVED and recorded in approval.json.")
    return True


def main():
    OUT.mkdir(exist_ok=True)
    findings=scan()
    print(f"[scan] {len(findings)} resources checked")
    recs=recommend(findings)
    validate(recs, findings)
    print("[validate] recommendations passed all checks")
    changes=build_changes(recs)
    (OUT / "changes.tf").write_text(changes)
    if not approval_gate(recs, findings, changes):
        sys.exit(1)
    print("\nNext: run  python check_and_apply.py  (it re-checks the fingerprint and tells you what to do)")


if __name__ =="__main__":
    main()
