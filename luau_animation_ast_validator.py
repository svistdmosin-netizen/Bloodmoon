#!/usr/bin/env python3
"""AST validator for Uhhhhhh animation flushes (direct Update reachability)."""
from __future__ import annotations
import argparse, json, subprocess
from pathlib import Path
from typing import Any, Iterable


def walk(x: Any) -> Iterable[dict[str, Any]]:
    if isinstance(x,dict):
        if "type" in x: yield x
        for v in x.values(): yield from walk(v)
    elif isinstance(x,list):
        for v in x: yield from walk(v)


def walk_executable(x: Any, root=True) -> Iterable[dict[str,Any]]:
    """Walk executable nodes but never descend into nested function bodies."""
    if isinstance(x,dict):
        typ=x.get("type")
        if typ in {"AstExprFunction","AstStatLocalFunction","AstStatFunction"} and not root:
            return
        if typ: yield x
        for v in x.values(): yield from walk_executable(v,False)
    elif isinstance(x,list):
        for v in x: yield from walk_executable(v,False)


def local_id_from_decl(d:dict[str,Any])->str:
    return f"{d.get('name')}@{d.get('location')}"
def local_id(e:dict[str,Any])->str|None:
    return local_id_from_decl(e.get("local",{})) if e.get("type")=="AstExprLocal" else None
def expr_name(e:dict[str,Any])->str|None:
    if e.get("type")=="AstExprLocal": return e.get("local",{}).get("name")
    if e.get("type")=="AstExprGlobal": return e.get("global")
    if e.get("type")=="AstExprIndexName": return e.get("index")
    return None

def is_get_scale(e:dict[str,Any],receivers:set[str])->bool:
    if e.get("type")!="AstExprCall": return False
    f=e.get("func",{})
    return f.get("type")=="AstExprIndexName" and f.get("index")=="GetScale" and local_id(f.get("expr",{})) in receivers

def proxy_base_id(e:dict[str,Any], field:str)->str|None:
    """Return declaration identity for `proxy.<field>` expressions."""
    if e.get("type")!="AstExprIndexName" or e.get("index")!=field: return None
    base=e.get("expr",{})
    ident=local_id(base)
    if ident: return ident
    # Loop proxies may themselves be an indexed table expression. Use a stable
    # structural signature after deleting source locations.
    if base.get("type") in {"AstExprIndexName","AstExprIndexExpr"}:
        def clean(x):
            if isinstance(x,dict): return {k:clean(v) for k,v in x.items() if k not in {"location","indexLocation"}}
            if isinstance(x,list): return [clean(v) for v in x]
            return x
        return json.dumps(clean(base),sort_keys=True)
    return None

def update_functions(ast:dict[str,Any])->list[dict[str,Any]]:
    out=[]
    for n in walk(ast.get("root",{})):
        if n.get("type")!="AstStatAssign": continue
        vals=n.get("values",[])
        if vals and vals[0].get("type")=="AstExprFunction" and any(v.get("type")=="AstExprIndexName" and v.get("index")=="Update" for v in n.get("vars",[])): out.append(vals[0])
    return out

def analyze_update(fn:dict[str,Any],index:int)->list[dict[str,Any]]:
    args=fn.get("args",[])
    figure={local_id_from_decl(a) for a in args if a.get("name") in {"figure","model","character"}}
    # Hub references conventionally use the second Update parameter as figure.
    if len(args)>=2: figure.add(local_id_from_decl(args[1]))
    aliases=set(figure); scales=set()
    nodes=list(walk_executable(fn.get("body",{})))
    calls=[]
    # Process in lexical AST order. A binding becomes trusted only after its
    # initializer/assignment, preventing use-before-initialization acceptance.
    # Branch-sensitive merge remains a documented future refinement.
    for n in nodes:
        pairs=[]
        if n.get("type")=="AstStatLocal": pairs=zip(n.get("vars",[]),n.get("values",[]))
        elif n.get("type")=="AstStatAssign": pairs=zip(n.get("vars",[]),n.get("values",[]))
        for var,val in pairs:
            vid=local_id(var) or (local_id_from_decl(var) if var.get("type")=="AstLocal" else None)
            src=local_id(val)
            # Evaluate the RHS against the state before assignment, then
            # invalidate all trust previously attached to the target binding.
            alias_rhs_ok=src in aliases
            scale_rhs_ok=is_get_scale(val,aliases)
            if vid:
                aliases.discard(vid)
                scales.discard(vid)
                if alias_rhs_ok: aliases.add(vid)
                if scale_rhs_ok: scales.add(vid)
        if n.get("type")!="AstExprCall" or expr_name(n.get("func",{}))!="SetC0C1Joint": continue
        a=n.get("args",[]); fourth=a[3] if len(a)>=4 else {}
        ref_base=proxy_base_id(a[0],"Reference") if len(a)>=1 else None
        c0_base=proxy_base_id(a[1],"C0") if len(a)>=2 else None
        c1_base=proxy_base_id(a[2],"C1") if len(a)>=3 else None
        proxy_triplet_ok=bool(ref_base and ref_base==c0_base==c1_base)
        calls.append({"location":n.get("location"),"update_index":index,"argument_count":len(a),
          "scale_origin_ok":is_get_scale(fourth,aliases) or local_id(fourth) in scales,
          "joint_argument_kind":a[0].get("type") if a else None,"pose_arguments_present":len(a)>=3,
          "proxy_triplet_ok":proxy_triplet_ok,"proxy_base_identity":ref_base})
    return calls

def validate(ast:dict[str,Any])->dict[str,Any]:
    definitions=0
    for n in walk(ast.get("root",{})):
        if n.get("type")=="AstStatLocalFunction" and n.get("name",{}).get("name")=="SetC0C1Joint": definitions+=1
        if n.get("type")=="AstStatLocal":
            for var,val in zip(n.get("vars",[]),n.get("values",[])):
                if var.get("name")=="SetC0C1Joint" and val.get("type")=="AstExprFunction": definitions+=1
    updates=update_functions(ast); calls=[]
    for i,fn in enumerate(updates): calls.extend(analyze_update(fn,i))
    valid=[c for c in calls if c["argument_count"]==4 and c["scale_origin_ok"] and c["pose_arguments_present"]]
    shared=[c for c in valid if c["proxy_triplet_ok"]]
    # A shared base is structural evidence only. Strict origin remains false
    # until Motor6D/CFrame types and six-joint R6 coverage are independently proven.
    return {"update_functions":len(updates),"helper_definitions":definitions,"update_flush_call_sites":len(calls),
      "valid_scale_flush_call_sites":len(valid),"shared_proxy_base_call_sites":len(shared),
      "proven_proxy_flush_call_sites":0,
      "calls":calls,"ok":bool(updates and valid),"shared_proxy_base_ok":bool(updates and shared),
      "strict_origin_ok":False,
      "direct_update_containment":bool(calls),"transitive_update_reachability":False,
      "receiver_bound_to_update_figure":bool(valid),"joint_and_cframe_type_inference":False,
      "r6_joint_coverage_ok":False,
      "limitations":["helper calls transitively reached from Update are not included","proxy Reference/C0/C1 types are not inferred","direct-joint profiles such as v_moveset2 need Motor6D/CFrame inference","R6 six-joint coverage is not yet proven"]}
def main()->int:
    ap=argparse.ArgumentParser(); ap.add_argument("source",type=Path); ap.add_argument("--luau-ast",type=Path,default=Path(__file__).parent/"tools/luau/luau-ast"); ap.add_argument("--output",type=Path)
    a=ap.parse_args(); p=subprocess.run([str(a.luau_ast),str(a.source)],capture_output=True,text=True)
    if p.returncode: print(p.stderr); return 2
    r=validate(json.loads(p.stdout)); text=json.dumps(r,indent=2)
    if a.output:a.output.write_text(text)
    print(text); return 0 if r["ok"] else 1
if __name__=="__main__":raise SystemExit(main())
