#!/usr/bin/env python3
"""Idempotently publish the curated product backlog as GitHub issues."""
from __future__ import annotations

import base64
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

API="https://api.github.com"; VER="2022-11-28"; MARK="workout-product-audit-2026-08-17:"
PRIORITY={"p0":"priority:high","p1":"priority:medium","p2":"priority:low"}

def call(token:str, method:str, url:str, payload:dict[str,Any]|None=None, expected=(200,))->Any:
    data=json.dumps(payload).encode() if payload is not None else None
    headers={"Accept":"application/vnd.github+json","Authorization":f"Bearer {token}","Content-Type":"application/json","User-Agent":"workout-agent-backlog-importer/2026-08-17","X-GitHub-Api-Version":VER}
    for attempt in range(9):
        try:
            with urllib.request.urlopen(urllib.request.Request(url,data=data,headers=headers,method=method),timeout=60) as r:
                response_body=r.read()
                if r.status not in expected: raise RuntimeError(f"unexpected status {r.status}")
                return json.loads(response_body) if response_body else None
        except urllib.error.HTTPError as e:
            raw=e.read().decode(errors="replace")
            if e.code not in {403,429,500,502,503,504} or attempt==8: raise RuntimeError(f"GitHub {e.code}: {raw}") from e
            retry=e.headers.get("Retry-After"); delay=max(1,int(retry)) if retry and retry.isdigit() else min(90,2**(attempt+1))
            print(f"GitHub {e.code}; retrying in {delay}s",file=sys.stderr); time.sleep(delay)
        except urllib.error.URLError as e:
            if attempt==8: raise RuntimeError(f"network failure: {e}") from e
            time.sleep(min(60,2**(attempt+1)))
    raise AssertionError

def all_issues(token:str, repo:str)->list[dict[str,Any]]:
    out=[]; page=1
    while True:
        chunk=call(token,"GET",f"{API}/repos/{repo}/issues?state=all&sort=updated&direction=desc&per_page=100&page={page}")
        if not isinstance(chunk,list): raise RuntimeError("issues endpoint returned non-list")
        out+=chunk
        if len(chunk)<100: return out
        page+=1

def norm(title:str)->str:
    title=re.sub(r"^\s*(?:\[[^\]]+\]\s*)+","",title)
    return re.sub(r"[^a-z0-9]+"," ",title.lower()).strip()

def marker(text:str|None)->str|None:
    match=re.search(rf"<!--\s*{re.escape(MARK)}([a-z0-9-]+)\s*-->",text or "",re.IGNORECASE)
    return match.group(1).lower() if match else None

def label(item:dict[str,Any])->str:
    if item["type"]=="bug": return "bug"
    if item["type"] in {"feature","epic"}: return "enhancement"
    if item["type"]=="chore" and item["area"] in {"docs","documentation"}: return "documentation"
    return "enhancement"

def render_body(item:dict[str,Any], numbers:dict[str,int])->str:
    lines=["## Problem","",item["problem"],"","## Acceptance criteria",""]+[f"- [ ] {x}" for x in item["acceptance_criteria"]]
    deps=item.get("dependencies") or []
    if deps:
        lines += ["","## Dependencies",""]+[f"- #{numbers[d]}" if d in numbers else f"- `{d}`" for d in deps]
    lines += ["","## Delivery requirements","","- Search open and closed issues and pull requests before implementation; continue canonical work instead of opening a duplicate.","- Start from the latest `main`, keep the change tenant-safe, and do not weaken tests or security controls.","- Add or update deterministic tests for the acceptance criteria and relevant failure paths.","- Record exact commands and results in the pull request; do not claim checks that were not run.","- Use the OpenHands Agent Canvas on the VPS for autonomous implementation. Do not revive the retired GitHub-hosted swarm.","","## Audit context","","- Production settings: https://workout.elgansayer.com/settings","- Production Coach: https://workout.elgansayer.com/chat","- Production plan: https://workout.elgansayer.com/plan","- Repository: https://github.com/elgansayer/workout-agent","- Actions: https://github.com/elgansayer/workout-agent/actions","",f"<!-- {MARK}{item['id']} -->"]
    return "\n".join(lines).strip()+"\n"

def main()->int:
    token=os.environ.get("GITHUB_TOKEN","").strip(); repo=os.environ.get("GITHUB_REPOSITORY","").strip()
    if not token or not repo: print("GITHUB_TOKEN and GITHUB_REPOSITORY required",file=sys.stderr); return 2
    root=Path(__file__).resolve().parents[1]; encoded="".join(path.read_text().strip() for path in sorted((root/".github/product-backlog").glob("catalogue.*.b64")))
    catalogue=json.loads(gzip.decompress(base64.b64decode(encoded)))
    existing=all_issues(token,repo); by_marker={}; by_title={}
    for issue in existing:
        if found_marker:=marker(issue.get("body")): by_marker[found_marker]=issue
        normalized=norm(issue.get("title","")); by_title.setdefault(normalized,issue) if normalized else None
    numbers={key:int(value["number"]) for key,value in by_marker.items()}; created=[]; skipped=[]; errors=[]
    for item in catalogue:
        found=by_marker.get(item["id"]) or by_title.get(norm(item["title"]))
        if found:
            numbers[item["id"]]=int(found["number"]); skipped.append(found["number"]); print(f"SKIP #{found['number']}: {item['title']}"); continue
        try:
            issue=call(token,"POST",f"{API}/repos/{repo}/issues",{"title":item["title"],"body":render_body(item,numbers),"labels":[label(item),PRIORITY[item["priority"]]]},(201,))
            numbers[item["id"]]=int(issue["number"]); by_marker[item["id"]]=issue; by_title[norm(item["title"])]=issue
            created.append({"number":issue["number"],"title":item["title"],"url":issue["html_url"]}); print(f"CREATED #{issue['number']}: {item['title']}"); time.sleep(1.1)
        except Exception as error:
            errors.append({"id":item["id"],"title":item["title"],"error":str(error)}); print(f"ERROR {item['title']}: {error}",file=sys.stderr)
    report={"catalogue":len(catalogue),"created":len(created),"skipped_existing":len(skipped),"errors":len(errors),"created_issues":created,"errors_detail":errors}
    Path("product-backlog-import-report.json").write_text(json.dumps(report,indent=2)+"\n")
    if summary:=os.environ.get("GITHUB_STEP_SUMMARY"):
        Path(summary).write_text(f"## Curated product backlog import\n\n- Catalogue: {len(catalogue)}\n- Created: {len(created)}\n- Existing: {len(skipped)}\n- Errors: {len(errors)}\n",encoding="utf-8")
    print(json.dumps(report,indent=2)); return 1 if errors else 0
if __name__=="__main__": raise SystemExit(main())
