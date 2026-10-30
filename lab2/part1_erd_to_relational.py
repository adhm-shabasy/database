#!/usr/bin/env python3
import argparse
import json
import sys
from html import escape


# =====================================================================
# 1. THE RELATIONAL MODEL (what the converter produces)
# =====================================================================
class Column:
    def __init__(self, name, is_pk=False, is_fk=False):
        self.name = name
        self.is_pk = is_pk
        self.is_fk = is_fk


class ForeignKey:
    """columns of THIS table  ->  ref_columns of ref_table"""

    def __init__(self, columns, ref_table, ref_columns):
        self.columns = columns
        self.ref_table = ref_table
        self.ref_columns = ref_columns


class Table:
    def __init__(self, name):
        self.name = name
        self.columns = []
        self.foreign_keys = []

    def has_column(self, name):
        return any(c.name == name for c in self.columns)

    def unique_name(self, wanted, prefix=""):
        """Return a column name that does not clash with an existing one."""
        name = wanted
        if self.has_column(name) and prefix:
            name = f"{prefix}_{wanted}"
        base, n = name, 2
        while self.has_column(name):
            name = f"{base}_{n}"
            n += 1
        return name

    def add_column(self, name, is_pk=False, prefix=""):
        col = Column(self.unique_name(name, prefix), is_pk)
        self.columns.append(col)
        return col

    def pk_names(self):
        return [c.name for c in self.columns if c.is_pk]

    def add_foreign_key(self, ref, role="", rel_name="", in_pk=False):
        """Copy the primary key of `ref` into this table as foreign-key columns."""
        ref_cols = ref.pk_names()  # fresh list -> safe even when ref is self
        my_cols = []
        for pk in ref_cols:
            wanted = f"{role}_{pk}" if role else pk
            col = Column(self.unique_name(wanted, rel_name), in_pk, True)
            self.columns.append(col)
            my_cols.append(col.name)
        self.foreign_keys.append(ForeignKey(my_cols, ref.name, ref_cols))


# =====================================================================
# 2. ER -> RELATIONAL MAPPING RULES
# =====================================================================
def parse_cardinality(text):
    """'N:1' -> ('N','1').  Anything that is not '1' counts as 'many'."""
    parts = str(text).replace(" ", "").upper().split(":")
    if len(parts) != 2 or any(p not in ("1", "N", "M", "*") for p in parts):
        raise ValueError(f"bad cardinality '{text}' (use 1:1, 1:N, N:1 or N:M)")
    return tuple("1" if p == "1" else "N" for p in parts)


class ERDConverter:
    def __init__(self, erd):
        self.entities = {e["name"]: e for e in erd.get("entities", [])}
        self.relationships = erd.get("relationships", [])
        self.tables = {} 

    def convert(self):
        self._strong_entities()
        self._weak_entities()
        self._multivalued_attributes()
        self._relationships()
        return list(self.tables.values())

    def _register(self, table):
        if table.name in self.tables:
            raise ValueError(f"two tables would both be called '{table.name}'")
        self.tables[table.name] = table

    @staticmethod
    def _add_attributes(table, attrs, prefix=""):
        """Rule: simple -> column, composite -> its simple components,
        multivalued -> skipped here (handled by its own table)."""
        for attr in attrs:
            if attr.get("isMultiValued"):
                continue
            is_pk = bool(attr.get("isPrimaryKey") or attr.get("isPartialKey"))
            if attr.get("composite"):
                for part in attr["composite"]:
                    table.add_column(part, is_pk, prefix=attr["name"])
            else:
                table.add_column(attr["name"], is_pk, prefix=prefix)

    def _strong_entities(self):
        for entity in self.entities.values():
            if entity.get("isWeak"):
                continue
            table = Table(entity["name"])
            self._add_attributes(table, entity.get("attributes", []))
            if not table.pk_names():
                raise ValueError(f"strong entity '{entity['name']}' has no primary key")
            self._register(table)

    def _owners_of(self, entity):
        owner = entity.get("owner")
        if owner:
            return [owner] if isinstance(owner, str) else list(owner)
        owners = []
        for rel in self.relationships:
            if rel.get("identifying") and entity["name"] in (rel["entity1"], rel["entity2"]):
                other = rel["entity2"] if rel["entity1"] == entity["name"] else rel["entity1"]
                owners.append(other)
        if not owners:
            raise ValueError(f"weak entity '{entity['name']}' needs an \"owner\" "
                             f"(or an identifying relationship)")
        return owners

    def _weak_entities(self):
        pending = [e for e in self.entities.values() if e.get("isWeak")]
        while pending:
            progressed = False
            for entity in list(pending):
                owners = self._owners_of(entity)
                if not all(o in self.tables for o in owners):
                    continue  
                table = Table(entity["name"])
                for o in owners:
                    table.add_foreign_key(self.tables[o], in_pk=True) 
                self._add_attributes(table, entity.get("attributes", []))
                self._register(table)
                pending.remove(entity)
                progressed = True
            if not progressed:
                raise ValueError("weak entities have a circular or unknown owner: "
                                 + ", ".join(e["name"] for e in pending))

    def _multivalued_attributes(self):
        for entity in self.entities.values():
            for attr in entity.get("attributes", []):
                if not attr.get("isMultiValued"):
                    continue
                table = Table(f"{entity['name']}_{attr['name']}")
                table.add_foreign_key(self.tables[entity["name"]], in_pk=True)
                for part in attr.get("composite") or [attr["name"]]:
                    table.add_column(part, True, prefix=attr["name"])
                self._register(table)

    def _is_identifying(self, rel):
        if rel.get("identifying"):
            return True
        for weak, other in ((rel["entity1"], rel["entity2"]), (rel["entity2"], rel["entity1"])):
            ent = self.entities.get(weak)
            if ent and ent.get("isWeak") and other in self._owners_of(ent):
                return True
        return False

    def _relationships(self):
        for rel in self.relationships:
            for key in ("entity1", "entity2", "cardinality"):
                if key not in rel:
                    raise ValueError(f"relationship {rel} is missing '{key}'")
            for key in ("entity1", "entity2"):
                if rel[key] not in self.tables:
                    raise ValueError(f"relationship refers to unknown entity '{rel[key]}'")
            if self._is_identifying(rel):
                continue  

            t1, t2 = self.tables[rel["entity1"]], self.tables[rel["entity2"]]
            role1, role2 = rel.get("role1", ""), rel.get("role2", "")
            card = parse_cardinality(rel["cardinality"])

            if card == ("N", "N"):
                self._many_to_many(rel, t1, t2, role1, role2)
            elif card == ("1", "N"):          # FK goes on the N side (entity2)
                self._put_fk(rel, fk_table=t2, ref_table=t1, role=role1)
            elif card == ("N", "1"):          # FK goes on the N side (entity1)
                self._put_fk(rel, fk_table=t1, ref_table=t2, role=role2)
            else:                             # 1:1 -> FK on the side with total participation
                e2_total = rel.get("participation2") == "total"
                e1_total = rel.get("participation1") == "total"
                if e2_total and not e1_total:
                    self._put_fk(rel, fk_table=t2, ref_table=t1, role=role1)
                else:
                    self._put_fk(rel, fk_table=t1, ref_table=t2, role=role2)

    def _put_fk(self, rel, fk_table, ref_table, role):
        fk_table.add_foreign_key(ref_table, role=role, rel_name=rel.get("name", ""))
        # relationship attributes migrate to the side that holds the FK
        self._add_attributes(fk_table, rel.get("attributes", []), prefix=rel.get("name", ""))

    def _many_to_many(self, rel, t1, t2, role1, role2):
        name = rel.get("name") or f"{t1.name}_{t2.name}"
        table = Table(name)
        table.add_foreign_key(t1, role=role1, rel_name=name, in_pk=True)
        table.add_foreign_key(t2, role=role2, rel_name=name, in_pk=True)
        self._add_attributes(table, rel.get("attributes", []), prefix=name)
        self._register(table)


def schema_to_text(tables):
    """Plain-text version of the schema (printed to the console)."""
    lines = []
    for t in tables:
        lines.append(t.name)
        for c in t.columns:
            tags = ",".join(x for x, on in (("PK", c.is_pk), ("FK", c.is_fk)) if on)
            lines.append(f"    {c.name}" + (f"  [{tags}]" if tags else ""))
        for fk in t.foreign_keys:
            lines.append(f"    ({', '.join(fk.columns)}) -> "
                         f"{fk.ref_table}({', '.join(fk.ref_columns)})")
        lines.append("")
    return "\n".join(lines)


# =====================================================================
# 3. DRAWING 
# =====================================================================
class SvgDiagram:

    CHAR_W, CELL_PAD, CELL_H, MIN_CELL = 8.4, 14, 32, 60
    LABEL_H = 28                      # band above each table that holds its name
    LANE_START, LANE_GAP = 18, 10     # wiring lanes under a table: first lane, spacing
    MARGIN, CHANNEL_GAP, CHANNEL_STEP = 40, 40, 9
    FILL, STROKE, INK = "#e3e3e3", "#4d4d4d", "#111111"

    def __init__(self, tables):
        self.tables = tables
        self.by_name = {t.name: t for t in tables}
        self.order = self._order_tables()
        self._layout()

    # ---- which table goes on which line (referenced tables first, like the lecture)
    def _forward_edges(self):
        """(table, referenced_table) pairs minus self references and cycle-closing edges."""
        state, edges = {}, []                # state: 1 = on the DFS stack, 2 = finished

        def visit(t):
            state[t.name] = 1
            for fk in t.foreign_keys:
                ref = fk.ref_table
                if ref == t.name or state.get(ref) == 1:
                    continue
                edges.append((t.name, ref))
                if ref not in state:
                    visit(self.by_name[ref])
            state[t.name] = 2

        for t in self.tables:
            if t.name not in state:
                visit(t)
        return edges

    def _order_tables(self):
        edges = self._forward_edges()
        depth = {t.name: 0 for t in self.tables}
        for _ in self.tables:                # longest-path layering
            for a, b in edges:
                depth[a] = max(depth[a], depth[b] + 1)
        for t in self.tables:                # a table nobody needs to precede sits just before its users
            users = [a for a, b in edges if b == t.name]
            if users and not any(a == t.name for a, _ in edges):
                depth[t.name] = min(depth[u] for u in users) - 1
        index = {t.name: i for i, t in enumerate(self.tables)}
        return sorted(self.tables, key=lambda t: (depth[t.name], index[t.name]))

    # ---- geometry
    def _layout(self):
        # 1. one arrow per (FK column -> referenced PK column)
        self.arrows = []
        for t in self.order:
            for fk in t.foreign_keys:
                for col, ref_col in zip(fk.columns, fk.ref_columns):
                    self.arrows.append({
                        "src": t.name, "src_i": self._col_index(t, col),
                        "dst": fk.ref_table,
                        "dst_i": self._col_index(self.by_name[fk.ref_table], ref_col)})

        # 2. cell sizes (a cell that receives many arrows is widened so the heads do not touch)
        incoming = {}
        for a in self.arrows:
            key = (a["dst"], a["dst_i"])
            incoming[key] = incoming.get(key, 0) + 1
        self.cells = {}                      # table -> [(x, width), ...] one per column
        for t in self.order:
            x, row = self.MARGIN, []
            for i, c in enumerate(t.columns):
                w = max(self.MIN_CELL, len(c.name) * self.CHAR_W + 2 * self.CELL_PAD,
                        24 + 16 * (incoming.get((t.name, i), 1) - 1))
                row.append((x, w))
                x += w
            self.cells[t.name] = row
        table_w = {n: sum(w for _, w in row) for n, row in self.cells.items()}

        # 3. x of each arrow end (several arrows into one cell are placed side by side)
        into = {}
        for a in self.arrows:
            into.setdefault((a["dst"], a["dst_i"]), []).append(a)
        for a in self.arrows:
            x, w = self.cells[a["src"]][a["src_i"]]
            a["sx"] = x + w / 2
        for (name, i), group in into.items():
            x, w = self.cells[name][i]
            step = min(16, (w - 24) / max(len(group) - 1, 1))
            for k, a in enumerate(group):
                a["tx"] = x + w / 2 + (k - (len(group) - 1) / 2) * step

        # 4. wiring lanes under each table.  Sorting the ends left-to-right and giving the
        #    left-most end the DEEPEST lane keeps the horizontal runs from crossing the drops.
        ends = {t.name: [] for t in self.order}
        for a in self.arrows:
            ends[a["src"]].append((a["sx"], a, "lane_s"))
            ends[a["dst"]].append((a["tx"], a, "lane_t"))
        lane_off, zone_h = {}, {}
        for name, lst in ends.items():
            lst.sort(key=lambda e: e[0])
            m = len(lst)
            for rank, (_, a, key) in enumerate(lst):
                lane_off[(id(a), key)] = self.LANE_START + (m - 1 - rank) * self.LANE_GAP
            zone_h[name] = (self.LANE_START + (m - 1) * self.LANE_GAP + 16) if m else 22

        # 5. vertical positions
        self.row_top, self.row_bottom, y = {}, {}, self.MARGIN
        for t in self.order:
            self.row_top[t.name] = y + self.LABEL_H
            self.row_bottom[t.name] = y + self.LABEL_H + self.CELL_H
            y = self.row_bottom[t.name] + zone_h[t.name]
        for a in self.arrows:
            a["lane_s"] = self.row_bottom[a["src"]] + lane_off[(id(a), "lane_s")]
            a["lane_t"] = self.row_bottom[a["dst"]] + lane_off[(id(a), "lane_t")]

        # 6. vertical "channels" on the right; short hops get the inner channels
        base = self.MARGIN + max(table_w.values()) + self.CHANNEL_GAP
        for rank, a in enumerate(sorted(self.arrows, key=lambda a: abs(a["lane_s"] - a["lane_t"]))):
            a["cx"] = base + rank * self.CHANNEL_STEP
        self.width = base + len(self.arrows) * self.CHANNEL_STEP + self.MARGIN
        self.height = y + self.MARGIN - 10

    @staticmethod
    def _col_index(table, name):
        return [c.name for c in table.columns].index(name)

    # ---- drawing
    def render(self):
        out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width:.0f}" '
               f'height="{self.height:.0f}" viewBox="0 0 {self.width:.0f} {self.height:.0f}" '
               f'font-size="14">',
               '<defs><marker id="head" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="9" '
               f'markerHeight="9" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="{self.INK}"/>'
               '</marker></defs>',
               f'<rect width="{self.width:.0f}" height="{self.height:.0f}" fill="#ffffff"/>']
        for t in self.order:
            out.append(self._table_svg(t))
        for a in self.arrows:
            pts = [(a["sx"], self.row_bottom[a["src"]]), (a["sx"], a["lane_s"]),
                   (a["cx"], a["lane_s"]), (a["cx"], a["lane_t"]),
                   (a["tx"], a["lane_t"]), (a["tx"], self.row_bottom[a["dst"]])]
            out.append('<polyline points="' + " ".join(f"{x:.1f},{y:.1f}" for x, y in pts) +
                       f'" fill="none" stroke="{self.INK}" stroke-width="1.3" '
                       f'stroke-linejoin="round" marker-end="url(#head)"/>')
        for a in self.arrows:                # dot = this attribute is a foreign key
            out.append(f'<circle cx="{a["sx"]:.1f}" cy="{self.row_bottom[a["src"]]}" r="3.2" '
                       f'fill="{self.INK}"/>')
        out.append("</svg>")
        return "\n".join(out)

    def _table_svg(self, t):
        top, bottom = self.row_top[t.name], self.row_bottom[t.name]
        s = [f'<text x="{self.MARGIN}" y="{top - 8}" font-family="Helvetica, Arial, sans-serif" '
             f'font-weight="bold" fill="{self.INK}">{escape(t.name.upper())}</text>']
        for c, (x, w) in zip(t.columns, self.cells[t.name]):
            s.append(f'<rect x="{x}" y="{top}" width="{w}" height="{self.CELL_H}" '
                     f'fill="{self.FILL}" stroke="{self.STROKE}" stroke-width="1.2"/>')
            cx, base = x + w / 2, top + self.CELL_H / 2 + 5
            s.append(f'<text x="{cx}" y="{base}" text-anchor="middle" fill="{self.INK}" '
                     f'font-family="Consolas, Menlo, \'Courier New\', monospace">{escape(c.name)}</text>')
            if c.is_pk:                      # drawn by hand: every renderer shows it the same
                half = len(c.name) * self.CHAR_W / 2
                s.append(f'<line x1="{cx - half:.1f}" y1="{base + 3.5}" x2="{cx + half:.1f}" '
                         f'y2="{base + 3.5}" stroke="{self.INK}" stroke-width="1.2"/>')
        return "\n".join(s)


# =====================================================================
# 4.PNG EXPORT  
# =====================================================================
def write_png(svg_path, scale=2):
    """Convert the SVG file to a PNG. Skipped with a hint if cairosvg is missing."""
    try:
        import cairosvg
    except ImportError:
        print("PNG skipped: install the converter with  pip install cairosvg  "
              "(or open the .svg in a browser and screenshot / print it)")
        return
    png_path = svg_path.rsplit(".", 1)[0] + ".png"
    cairosvg.svg2png(url=svg_path, write_to=png_path, scale=scale)
    print(f"picture written to {png_path}")


# =====================================================================
# 5. COMMAND LINE
# =====================================================================
def main(argv=None):
    ap = argparse.ArgumentParser(description="Convert an ERD (JSON) into a relational-model diagram (SVG).")
    ap.add_argument("erd", help="path of the ERD JSON file")
    ap.add_argument("-o", "--output", help="output .svg path (default: <input>.svg)")
    ap.add_argument("--png", action="store_true",
                    help="also write a .png next to the .svg (needs: pip install cairosvg)")
    args = ap.parse_args(argv)

    try:
        with open(args.erd, encoding="utf-8") as f:
            erd = json.load(f)
        tables = ERDConverter(erd).convert()
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as err:
        print(f"error: {err!r}" if isinstance(err, KeyError) else f"error: {err}", file=sys.stderr)
        return 1

    print(schema_to_text(tables))
    out_path = args.output or args.erd.rsplit(".", 1)[0] + ".svg"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(SvgDiagram(tables).render())
    print(f"diagram written to {out_path}")
    if args.png:
        write_png(out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
