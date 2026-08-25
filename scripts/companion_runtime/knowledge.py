from __future__ import annotations
import csv,hashlib,json,os,uuid
from pathlib import Path
from typing import Any
from .models import TASK_POLICIES
from .store import now

POLICY={
 "daily_open_close":{"data/logs/05_DECISION_LOG.csv","data/logs/12_OPPORTUNITY_LOG.csv","data/state/10_THEME_STATE.csv","data/state/11_STOCK_STATE.csv"},
 "daily_intraday":{"data/logs/05_DECISION_LOG.csv","data/state/10_THEME_STATE.csv","data/state/11_STOCK_STATE.csv"},
 "periodic_review":{"data/logs/05_DECISION_LOG.csv","data/logs/12_OPPORTUNITY_LOG.csv"},
}
class KnowledgeCommitter:
 def __init__(self,root:Path,store:Any)->None:self.root=Path(root);self.store=store
 def propose(self,cycle:dict[str,Any],changeset:dict[str,Any])->str:
  pid=str(uuid.uuid4());raw=json.dumps(changeset,ensure_ascii=False,sort_keys=True)
  with self.store.connection() as c:c.execute(
   """INSERT INTO knowledge_change_proposal(
        proposal_id,cycle_id,policy,changeset_json,state,created_at,applied_at,error,
        category,evidence_json,validation_json,requires_approval,approved_at,decision_note)
      VALUES(?,?,?,?, 'pending',?,NULL,NULL,'deterministic_projection','[]','{}',1,NULL,NULL)""",
   (pid,cycle["cycle_id"],cycle["task_key"],raw,now()))
  return pid
 def apply(self,proposal_id:str)->dict[str,Any]:
  with self.store.connection() as c: row=c.execute("SELECT * FROM knowledge_change_proposal WHERE proposal_id=?",(proposal_id,)).fetchone()
  if not row:raise ValueError("unknown proposal")
  changes=json.loads(row["changeset_json"]);policy=TASK_POLICIES[row["policy"]].knowledge_family
  applied=[]
  for change in changes.get("changes",[]):
   target=change.get("target");op=change.get("operation")
   if target not in POLICY[policy]:raise ValueError(f"target not allowed by policy: {target}")
   path=(self.root/target).resolve()
   if not path.is_relative_to(self.root.resolve()):raise ValueError("target escapes root")
   expected=change.get("expected_sha256");current=hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
   if expected!=current:raise ValueError(f"revision conflict: {target}")
   if op=="append_csv_row":
    row_data=change.get("row");
    if not isinstance(row_data,dict):raise ValueError("row must be object")
    with path.open("r",encoding="utf-8-sig",newline="") as h: fields=next(csv.reader(h))
    if set(row_data)-set(fields):raise ValueError("row has unknown columns")
    line=",".join('"'+str(row_data.get(f,"")).replace('"','""')+'"' for f in fields)+"\n"
    with path.open("a",encoding="utf-8",newline="") as h:h.write(line);h.flush();os.fsync(h.fileno())
   else:raise ValueError(f"unsupported operation: {op}")
   applied.append(target)
  with self.store.connection() as c:c.execute("UPDATE knowledge_change_proposal SET state='applied',applied_at=? WHERE proposal_id=?",(now(),proposal_id))
  return {"proposal_id":proposal_id,"applied":applied}
