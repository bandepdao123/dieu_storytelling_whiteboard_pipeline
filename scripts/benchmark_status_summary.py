#!/usr/bin/env python3
"""Deterministic status payload benchmark; no wall-clock claims.

Builds fresh in-memory 50/360-scene databases via public workflow calls, then
traces SELECT statements only around compact UTF-8 JSON serialization calls.
"""
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from du_pipeline.db import Database
from du_pipeline.service import Pipeline


def measure(count):
    db=Database(":memory:"); p=Pipeline(db)
    pid=p.init_project("benchmark",scene_range=(1,360)); duration=count*4000
    p.import_audio(pid,"audio.wav",duration,"a"*64)
    p.import_srt(pid,[(i*4000,(i+1)*4000,f"cue {i}") for i in range(count)])
    p.plan_scenes(pid)
    traces=[]; db.conn.set_trace_callback(lambda sql: traces.append(sql) if sql.lstrip().upper().startswith("SELECT") else None)
    full=p.status(pid); full_queries=len(traces); traces.clear()
    summary=p.status_summary(pid); summary_queries=len(traces)
    compact=lambda value: len(json.dumps(value,ensure_ascii=False,separators=(",",":"),sort_keys=True).encode())
    db.close()
    return {"scenes":count,"full":{"bytes":compact(full),"select_queries":full_queries},"summary":{"bytes":compact(summary),"select_queries":summary_queries}}


if __name__=="__main__":
    print(json.dumps({"method":"fresh in-memory SQLite; public init/import/plan fixture; SELECT trace enabled only for status call; sorted compact UTF-8 JSON; deterministic bytes/query count; no timing","results":[measure(50),measure(360)]},separators=(",",":"),sort_keys=True))
