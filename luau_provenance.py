#!/usr/bin/env python3
"""Scoped conservative provenance analysis for Luau JSON AST.

Origins form a may-set lattice. Branch/loop merges use union, so a trusted
origin is usable only when the resulting set is exactly the required origin.
This prevents conditional reassignment from silently preserving trust.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

PROVENANCE_API_VERSION = "2.0"
OriginSet = frozenset[str]


def decl_id(decl: dict[str, Any]) -> str:
    return f"{decl.get('name')}@{decl.get('location')}"


def local_id(expr: dict[str, Any]) -> str | None:
    return decl_id(expr.get("local", {})) if expr.get("type") == "AstExprLocal" else None


def member_name(expr: dict[str, Any]) -> str | None:
    if expr.get("type") == "AstExprIndexName": return expr.get("index")
    if expr.get("type") == "AstExprIndexExpr":
        index=expr.get("index", {})
        if index.get("type") == "AstExprConstantString": return index.get("value")
    return None


@dataclass
class FlowState:
    values: dict[str, OriginSet] = field(default_factory=dict)

    def copy(self) -> "FlowState": return FlowState(dict(self.values))
    def get(self, identity: str | None) -> OriginSet:
        return self.values.get(identity or "", frozenset({"unknown"}))
    def set(self, identity: str | None, origins: OriginSet) -> None:
        if identity: self.values[identity]=origins
    @staticmethod
    def merge(states: list["FlowState"]) -> "FlowState":
        keys=set().union(*(s.values for s in states)); out={}
        for key in keys:
            origins=set()
            for state in states: origins.update(state.values.get(key, frozenset({"unassigned"})))
            out[key]=frozenset(origins)
        return FlowState(out)


@dataclass
class ProvenanceResult:
    final_state: FlowState
    before: dict[str, FlowState]
    after: dict[str, FlowState]
    expression_before: dict[str, FlowState]


class Analyzer:
    def __init__(self) -> None:
        self.before: dict[str,FlowState]={}; self.after: dict[str,FlowState]={}
        self.expression_before: dict[str,FlowState]={}

    def _visit_expression_children(self, node: dict[str,Any], state: FlowState) -> None:
        def visit(value: Any) -> None:
            if isinstance(value,dict):
                typ=value.get("type","")
                if typ.startswith("AstExpr"):
                    self.expr(value,state)
                    return
                for child in value.values(): visit(child)
            elif isinstance(value,list):
                for child in value: visit(child)
        for key,value in node.items():
            if key in {"type","location","local"}: continue
            if node.get("type")=="AstExprFunction" and key=="body": continue
            visit(value)

    def expr(self, node: dict[str,Any], state: FlowState) -> OriginSet:
        location=node.get("location")
        if location: self.expression_before[location]=state.copy()
        typ=node.get("type")
        self._visit_expression_children(node,state)
        if typ=="AstExprLocal": return state.get(local_id(node))
        if typ=="AstExprGlobal":
            origin = {"loadstring":"dangerous_callable", "game":"game", "workspace":"workspace", "_G":"global_env"}.get(node.get("global"), "unknown_global")
            return frozenset({origin})
        if typ in {"AstExprIndexName","AstExprIndexExpr"}:
            base=self.expr(node.get("expr",{}),state); member=member_name(node)
            if member=="loadstring" and ("global_env" in base): return frozenset({"dangerous_callable"})
            if member=="Players" and base==frozenset({"game"}): return frozenset({"players_service"})
            if member=="Character" and base==frozenset({"local_player"}): return frozenset({"character"})
            if member=="LocalPlayer" and base==frozenset({"players_service"}): return frozenset({"local_player"})
            if member in {"Torso","HumanoidRootPart","Head","Left Arm","Right Arm","Left Leg","Right Leg"} and base==frozenset({"character"}): return frozenset({"r6_part:"+member})
            if member in {"C0","C1"} and any(x.startswith("joint:") for x in base): return frozenset({"cframe"})
            return frozenset({"member:"+str(member)})
        if typ=="AstExprCall":
            func=node.get("func",{}); name=member_name(func)
            if func.get("type")=="AstExprGlobal": name=func.get("global")
            receiver=self.expr(func.get("expr",{}),state) if func.get("expr") else frozenset()
            args=node.get("args",[])
            if name=="GetService" and receiver==frozenset({"game"}) and args and args[0].get("type")=="AstExprConstantString":
                service_name=args[0].get("value")
                if service_name=="Players": return frozenset({"players_service"})
                if service_name=="UserInputService": return frozenset({"user_input_service"})
            if name=="FindFirstChild" and receiver==frozenset({"workspace"}): return frozenset({"workspace_candidate"})
            if name=="GetMouse" and receiver==frozenset({"local_player"}): return frozenset({"player_mouse"})
            if name=="getgenv": return frozenset({"global_env"})
            return frozenset({"call_result"})
        if typ=="AstExprConstantString": return frozenset({"string"})
        if typ=="AstExprConstantNumber": return frozenset({"number"})
        if typ=="AstExprConstantBool": return frozenset({"boolean"})
        if typ=="AstExprTable": return frozenset({"table"})
        if typ=="AstExprFunction":
            function_state=state.copy()
            for parameter in node.get("args",[]):
                function_state.set(decl_id(parameter),frozenset({"parameter"}))
            # Analyze the body in an isolated state to expose provenance at
            # executable call sites without applying closure mutations globally.
            self.block(node.get("body",{}),function_state)
            return frozenset({"function"})
        if typ=="AstExprBinary": return self.expr(node.get("left",{}),state)|self.expr(node.get("right",{}),state)
        return frozenset({"unknown"})

    def block(self, block: dict[str,Any], incoming: FlowState) -> FlowState:
        state=incoming.copy()
        for statement in block.get("body",[]): state=self.statement(statement,state)
        return state

    def statement(self, node: dict[str,Any], incoming: FlowState) -> FlowState:
        loc=node.get("location","unknown"); self.before[loc]=incoming.copy(); state=incoming.copy(); typ=node.get("type")
        if typ=="AstStatLocal":
            for i,var in enumerate(node.get("vars",[])):
                value=node.get("values",[])[i] if i<len(node.get("values",[])) else {}
                state.set(decl_id(var),self.expr(value,incoming) if value else frozenset({"unassigned"}))
        elif typ=="AstStatAssign":
            for i,var in enumerate(node.get("vars",[])):
                value=node.get("values",[])[i] if i<len(node.get("values",[])) else {}
                state.set(local_id(var),self.expr(value,incoming))
        elif typ=="AstStatCompoundAssign":
            variable=node.get("var",{})
            old=state.get(local_id(variable)); new=self.expr(node.get("value",{}),incoming)
            state.set(local_id(variable),old|new)
        elif typ=="AstStatExpr":
            self.expr(node.get("expr",{}),state)
        elif typ=="AstStatReturn":
            for value in node.get("list",[]): self.expr(value,state)
        elif typ=="AstStatLocalFunction":
            function=node.get("func",{})
            state.set(decl_id(node.get("name",{})),frozenset({"function"}))
            self.expr(function,state)
        elif typ=="AstStatFunction":
            self.expr(node.get("func",{}),state)
        elif typ=="AstStatIf":
            branches=[self.block(node.get("thenbody",{}),incoming.copy())]
            elsebody=node.get("elsebody")
            branches.append(self.block(elsebody,incoming.copy()) if isinstance(elsebody,dict) else incoming.copy())
            state=FlowState.merge(branches)
        elif typ in {"AstStatWhile","AstStatRepeat","AstStatFor","AstStatForIn"}:
            body=node.get("body",{}); state=FlowState.merge([incoming.copy(),self.block(body,incoming.copy())])
        elif typ=="AstStatBlock": state=self.block(node,incoming)
        self.after[loc]=state.copy(); return state

    def analyze(self, ast: dict[str,Any]) -> ProvenanceResult:
        final=self.block(ast.get("root",{}),FlowState())
        return ProvenanceResult(final,self.before,self.after,self.expression_before)


def analyze(ast: dict[str,Any]) -> ProvenanceResult:
    return Analyzer().analyze(ast)
