from __future__ import annotations
from typing import Any
class OutcomeResolver:
 """Deterministic placeholder: refuses to invent returns without a versioned price input."""
 def resolve(self,cycle:dict[str,Any],price_revision:dict[str,Any]|None)->dict[str,Any]:
  if not price_revision:return {"state":"unresolved","reason":"no versioned price input","cycle_id":cycle["cycle_id"]}
  required={"entry_price","exit_price","symbol","as_of"}
  if required-price_revision.keys():return {"state":"unresolved","reason":"incomplete price revision","cycle_id":cycle["cycle_id"]}
  entry=float(price_revision["entry_price"]);exit=float(price_revision["exit_price"])
  if entry<=0:return {"state":"unresolved","reason":"invalid entry price","cycle_id":cycle["cycle_id"]}
  return {"state":"resolved","cycle_id":cycle["cycle_id"],"symbol":price_revision["symbol"],"return":(exit-entry)/entry,"price_revision":price_revision}
