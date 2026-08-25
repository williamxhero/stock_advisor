from __future__ import annotations

"""Rebuildable derived memory index; facts remain in companion.sqlite3."""

from dataclasses import dataclass
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import uuid
from typing import Any, Protocol

from .secret_guard import assert_safe
from .store import now


@dataclass(frozen=True)
class MemoryQuery:
    task_key: str
    known_at: str
    text: str = ""
    limit: int = 12
    cycle_id: str | None = None
    stage: str = "chat"


@dataclass(frozen=True)
class MemoryRequest:
    task_key: str
    known_at: str
    stage: str
    query_text: str = ""
    cycle_id: str | None = None
    available_characters: int = 12000


@dataclass(frozen=True)
class MemoryBundle:
    cards: list[dict[str, Any]]
    selected_characters: int
    audit_id: str
    health: str


class MemoryRetriever(Protocol):
    def retrieve(self, query: MemoryQuery) -> list[dict[str, Any]]: ...


class MemoryIndexer:
    """Only this DB may be discarded and rebuilt; it owns FTS and entities."""
    def __init__(self, store: Any, database: Path | None = None) -> None:
        self.store = store
        self.database = Path(database or store.database.with_name("memory-index.sqlite3"))

    @contextmanager
    def connection(self):
        self.database.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.database); con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def initialize(self) -> None:
        with self.connection() as c:
            c.executescript("""
              CREATE TABLE IF NOT EXISTS memory_document (
                document_id TEXT PRIMARY KEY, origin_type TEXT NOT NULL, origin_id TEXT NOT NULL UNIQUE,
                cycle_id TEXT, task_key TEXT, kind TEXT NOT NULL, actor TEXT, body TEXT NOT NULL,
                summary TEXT NOT NULL, content_sha256 TEXT NOT NULL, occurred_at TEXT, known_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL, corrected_by TEXT, indexed_at TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS ix_memory_document_known ON memory_document(known_at,task_key);
              CREATE TABLE IF NOT EXISTS memory_entity(entity_id TEXT PRIMARY KEY, normalized TEXT NOT NULL UNIQUE,label TEXT NOT NULL,kind TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS memory_document_entity(document_id TEXT NOT NULL,entity_id TEXT NOT NULL,PRIMARY KEY(document_id,entity_id));
              CREATE TABLE IF NOT EXISTS memory_index_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            """)
            try:
                c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(document_id UNINDEXED,content,tokenize='trigram')")
                tokenizer = "trigram"
            except sqlite3.OperationalError:
                c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(document_id UNINDEXED,content,tokenize='unicode61')")
                tokenizer = "unicode61"
            c.execute("INSERT OR REPLACE INTO memory_index_meta(key,value) VALUES('tokenizer',?)", (tokenizer,))

    def sync(self, *, limit: int = 500) -> int:
        self.initialize()
        with self.store.connection() as facts:
            intents = [dict(x) for x in facts.execute("SELECT * FROM memory_index_intent WHERE state='pending' ORDER BY created_at LIMIT ?", (limit,))]
        completed = 0
        for intent in intents:
            try:
                record = self._record(intent)
                if record is None: raise ValueError("memory origin unavailable")
                assert_safe(record["body"], boundary="memory index")
                self._upsert(record)
                with self.store.connection() as facts:
                    facts.execute("UPDATE memory_index_intent SET state='complete',completed_at=?,error=NULL WHERE intent_id=?", (now(),intent["intent_id"]))
                completed += 1
            except Exception as error:
                with self.store.connection() as facts:
                    facts.execute("UPDATE memory_index_intent SET state='blocked',completed_at=?,error=? WHERE intent_id=?", (now(),str(error)[:500],intent["intent_id"]))
        return completed

    def rebuild(self) -> int:
        self.initialize()
        with self.store.connection() as facts:
            for row in facts.execute("SELECT artifact_id,body_sha256,known_at FROM narrative_artifact"):
                facts.execute("INSERT OR IGNORE INTO memory_index_intent(intent_id,origin_type,origin_id,content_sha256,known_at,state,created_at) VALUES(?,?,?,?,?,'pending',?)", (str(uuid.uuid4()),"artifact",row["artifact_id"],row["body_sha256"],row["known_at"],now()))
            for row in facts.execute("SELECT evidence_id,content_sha256,known_at FROM evidence_ledger_entry"):
                facts.execute("INSERT OR IGNORE INTO memory_index_intent(intent_id,origin_type,origin_id,content_sha256,known_at,state,created_at) VALUES(?,?,?,?,?,'pending',?)", (str(uuid.uuid4()),"evidence",row["evidence_id"],row["content_sha256"],row["known_at"],now()))
        return self.sync(limit=100000)

    def health(self) -> dict[str, Any]:
        self.initialize()
        with self.store.connection() as facts: pending=facts.execute("SELECT COUNT(*) FROM memory_index_intent WHERE state!='complete'").fetchone()[0]
        with self.connection() as c:
            count=c.execute("SELECT COUNT(*) FROM memory_document").fetchone()[0]
            tokenizer=c.execute("SELECT value FROM memory_index_meta WHERE key='tokenizer'").fetchone()[0]
        return {"state":"ready" if not pending else "degraded","documents":count,"pending_or_blocked":pending,"tokenizer":tokenizer}

    def _record(self, intent: dict[str, Any]) -> dict[str, Any] | None:
        with self.store.connection() as c:
            if intent["origin_type"] == "artifact":
                row=c.execute("SELECT a.*,c.task_key FROM narrative_artifact a JOIN companion_cycle c ON c.cycle_id=a.cycle_id WHERE a.artifact_id=?",(intent["origin_id"],)).fetchone()
                if not row:return None
                r=dict(row)
                return {"document_id":"artifact:"+r["artifact_id"],"origin_type":"artifact","origin_id":r["artifact_id"],"cycle_id":r["cycle_id"],"task_key":r["task_key"],"kind":r["kind"],"actor":r["actor"],"body":r["body_markdown"],"summary":r["body_markdown"][:1200],"content_sha256":r["body_sha256"],"occurred_at":r["occurred_at"],"known_at":r["known_at"],"metadata_json":r["metadata_json"]}
            row=c.execute("SELECT * FROM evidence_ledger_entry WHERE evidence_id=?",(intent["origin_id"],)).fetchone()
            if not row:return None
            r=dict(row); metadata=json.loads(r["metadata_json"] or "{}")
            return {"document_id":"evidence:"+r["evidence_id"],"origin_type":"evidence","origin_id":r["evidence_id"],"cycle_id":r["cycle_id"],"task_key":metadata.get("task_key","market"),"kind":"evidence","actor":"source","body":r["body_text"],"summary":r["body_text"][:1200],"content_sha256":r["content_sha256"],"occurred_at":r["occurred_at"],"known_at":r["known_at"],"metadata_json":r["metadata_json"]}

    def _upsert(self, record: dict[str, Any]) -> None:
        with self.connection() as c:
            c.execute("DELETE FROM memory_fts WHERE document_id=?",(record["document_id"],))
            c.execute("""INSERT INTO memory_document(document_id,origin_type,origin_id,cycle_id,task_key,kind,actor,body,summary,content_sha256,occurred_at,known_at,metadata_json,corrected_by,indexed_at)
                         VALUES(:document_id,:origin_type,:origin_id,:cycle_id,:task_key,:kind,:actor,:body,:summary,:content_sha256,:occurred_at,:known_at,:metadata_json,NULL,:indexed_at)
                         ON CONFLICT(origin_id) DO UPDATE SET body=excluded.body,summary=excluded.summary,content_sha256=excluded.content_sha256,known_at=excluded.known_at,metadata_json=excluded.metadata_json,indexed_at=excluded.indexed_at""",{**record,"indexed_at":now()})
            c.execute("INSERT INTO memory_fts(document_id,content) VALUES(?,?)",(record["document_id"],record["body"]))
            c.execute("DELETE FROM memory_document_entity WHERE document_id=?",(record["document_id"],))
            for value,label,kind in self._entities(record["body"]):
                eid=str(uuid.uuid5(uuid.NAMESPACE_URL,f"{kind}:{value}")); c.execute("INSERT OR IGNORE INTO memory_entity(entity_id,normalized,label,kind) VALUES(?,?,?,?)",(eid,value,label,kind)); c.execute("INSERT OR IGNORE INTO memory_document_entity(document_id,entity_id) VALUES(?,?)",(record["document_id"],eid))

    @staticmethod
    def _entities(text: str) -> list[tuple[str,str,str]]:
        values=[(x,x,"stock_code") for x in re.findall(r"(?<!\d)\d{6}(?!\d)",text)]
        values.extend((x,x,"theme") for x in re.findall(r"[\u4e00-\u9fff]{3,8}",text)[:32])
        return list(dict.fromkeys(values))


class MemoryLibrary:
    def __init__(self, store: Any, indexer: MemoryIndexer | None = None) -> None:
        self.store=store; self.indexer=indexer or MemoryIndexer(store)

    def retrieve_bundle(self, request: MemoryRequest) -> MemoryBundle:
        self.indexer.sync(); terms=re.findall(r"[\u4e00-\u9fff]{2,}|\d{6}|[A-Za-z]{3,}",request.query_text)[:10]
        with self.indexer.connection() as c:
            rows=[dict(x) for x in c.execute("SELECT * FROM memory_document WHERE known_at<=? ORDER BY known_at DESC LIMIT 600",(request.known_at,))]
            entity_ids=set(); fts_ids=set()
            if terms:
                marks=','.join('?' for _ in terms); entity_ids={x[0] for x in c.execute(f"SELECT DISTINCT de.document_id FROM memory_document_entity de JOIN memory_entity e ON e.entity_id=de.entity_id WHERE e.normalized IN ({marks})",terms)}
                # Trigram FTS is a candidate source, never a bypass around the
                # authority-time filter applied above.  Short codes/themes are
                # still covered by exact entities and lexical matching.
                fts_terms=[term for term in terms if len(term)>=3]
                if fts_terms:
                    try:
                        expression=" OR ".join('"'+term.replace('"','')+'"' for term in fts_terms)
                        fts_ids={row[0] for row in c.execute("SELECT document_id FROM memory_fts WHERE memory_fts MATCH ? LIMIT 300",(expression,))}
                    except sqlite3.OperationalError:
                        fts_ids=set()
        candidates=[]
        for rank,row in enumerate(rows,1):
            if request.stage in {"m1_research","m1_judgment"} and row["cycle_id"]==request.cycle_id and row["actor"]=="human": continue
            text=(row["body"]+row["metadata_json"]).lower(); hits=sum(term.lower() in text for term in terms); counter=any(x in text for x in ("错误","反证","失败","incorrect","timing_wrong","缺口")); score=1/(60+rank)+hits*.25+(.45 if row["document_id"] in entity_ids else 0)+(.35 if row["document_id"] in fts_ids else 0)+(.35 if counter else 0)
            candidates.append({"artifact_id":row["origin_id"] if row["origin_type"]=="artifact" else None,"origin_id":row["origin_id"],"origin_type":row["origin_type"],"kind":row["kind"],"actor":row["actor"],"task_key":row["task_key"],"cycle_id":row["cycle_id"],"summary":row["summary"],"known_at":row["known_at"],"occurred_at":row["occurred_at"],"metadata_json":row["metadata_json"],"score":score,"counterevidence":counter})
        selected=[]; used=0; seen=set()
        for item in sorted(candidates,key=lambda x:x["score"],reverse=True):
            length=len(item["summary"]); key=(item["task_key"],item["summary"][:80])
            if key in seen or (used and used+length>request.available_characters):continue
            if not selected or item["counterevidence"] or item["score"]/max(length,1)>=.0008: selected.append(item); used+=length; seen.add(key)
        audit=str(uuid.uuid4())
        with self.store.connection() as c:c.execute("INSERT INTO memory_retrieval_audit(retrieval_id,task_key,query_text,known_at,selected_artifact_ids_json,created_at) VALUES(?,?,?,?,?,?)",(audit,request.task_key,request.query_text,request.known_at,json.dumps([x["origin_id"] for x in selected],ensure_ascii=False),now()))
        return MemoryBundle(selected,used,audit,self.indexer.health()["state"])


class SqliteMemoryRetriever:
    """Compatibility list adapter while callers move to MemoryBundle."""
    def __init__(self,store:Any)->None:self.library=MemoryLibrary(store)
    def retrieve(self,query:MemoryQuery)->list[dict[str,Any]]:
        return self.library.retrieve_bundle(MemoryRequest(query.task_key,query.known_at,query.stage,query.text,query.cycle_id,max(1600,query.limit*1200))).cards[:query.limit]
