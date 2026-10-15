import json
import sys
from html import escape
from graphviz import Graph

FILL = "lightgray"


def warn(msg):
    print("WARNING:", msg, file=sys.stderr)

def nid(*parts):
    return ".".join(str(p) for p in parts).replace(":", "_")

def entity_id(name):
    return nid("ent", name)

def attribute_label(attr):
    text = escape(str(attr["name"]))
    if attr.get("isForeignKey", False):
        text = f"<I>{text} (FK)</I>"
    if attr.get("isPrimaryKey", False):
        return f"<<U>{text}</U>>"
    if attr.get("isPartialKey", False):
        return ('<<TABLE BORDER="0" CELLBORDER="1" CELLSPACING="0" CELLPADDING="0">'
                f'<TR><TD SIDES="B" STYLE="dashed">{text}</TD></TR></TABLE>>')
    return f"<{text}>"

def add_attribute(g, owner_id, attr):
    if isinstance(attr, str):                      
        attr = {"name": attr}
    if not str(attr.get("name", "")).strip():
        warn("attribute without a name skipped")
        return
    node_id = nid(owner_id, attr["name"])

    style = {}
    if attr.get("isMultiValued", False):
        style["peripheries"] = "2"                 
    if attr.get("isDerived", False):
        style["style"] = "dashed"                  

    g.node(node_id, label=attribute_label(attr), shape="ellipse", **style)
    g.edge(owner_id, node_id)

    for child in attr.get("composite") or []:
        cname = child.get("name") if isinstance(child, dict) else child
        child_id = nid(node_id, cname)
        g.node(child_id, label=str(cname), shape="ellipse")
        g.edge(node_id, child_id)

def add_entity(g, entity):
    ename = str(entity["name"])
    eid = entity_id(ename)
    g.node(eid, label=ename, shape="box", style="filled", fillcolor=FILL,
           peripheries="2" if entity.get("isWeak", False) else "1")
    for attr in entity.get("attributes") or []:
        add_attribute(g, eid, attr)

def get_participants(rel):
    if rel.get("participants"):
        return list(rel["participants"])

    card = str(rel.get("cardinality", "")).strip()
    left, right = "", ""
    if ":" in card:
        left, right = [p.strip() for p in card.split(":", 1)]

    return [
        {"entity": rel.get("entity1"), "cardinality": left,
         "participation": rel.get("participation1"), "role": rel.get("role1")},
        {"entity": rel.get("entity2"), "cardinality": right,
         "participation": rel.get("participation2"), "role": rel.get("role2")},
    ]


def add_relationship(g, rel, index, known_entities):
    name = str(rel.get("name", "")).strip()
    participants = get_participants(rel)

    missing = [str(p.get("entity")) for p in participants
               if str(p.get("entity")) not in known_entities]
    if missing or len(participants) < 2 or not name:
        warn(f"relationship '{name}' skipped (unknown entity {missing} or too few participants)")
        return

    rid = nid("rel", index, name)                   # index keeps equal names apart
    g.node(rid, label=name, shape="diamond", style="filled", fillcolor=FILL,
           peripheries="2" if rel.get("isIdentifying", False) else "1")

    for p in participants:
        edge = {}
        role = str(p.get("role") or "").strip()
        card = str(p.get("cardinality") or "").strip()
        if role:
            edge["label"] = role                   
        if card:
            edge["taillabel"] = card               
        if str(p.get("participation") or "").lower() == "total":
            edge["color"] = "black:white:black"    
        g.edge(entity_id(p["entity"]), rid, **edge)

    for attr in rel.get("attributes") or []:       
        add_attribute(g, rid, attr)

def render(json_path, out_name, engine="neato"):
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    entities = data.get("entities") or []
    names = [str(e.get("name")) for e in entities]
    for n in set(names):
        if names.count(n) > 1:
            warn(f"duplicate entity name '{n}'")

    graph_attr = {"overlap": "false", "sep": "+8", "splines": "true"}
    if engine == "dot":
        graph_attr = {"rankdir": "LR", "nodesep": "0.4", "ranksep": "0.8"}

    g = Graph(engine=engine, format="png", graph_attr=graph_attr,
              edge_attr={"fontsize": "11", "labeldistance": "2"})

    for entity in entities:
        add_entity(g, entity)
    for i, rel in enumerate(data.get("relationships") or []):
        add_relationship(g, rel, i, set(names))

    return g.render(out_name, cleanup=True)


if __name__ == "__main__":
    render(sys.argv[1], sys.argv[2])
