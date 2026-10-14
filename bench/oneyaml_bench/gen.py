"""Deterministic synthetic workloads.

Every generator returns the YAML text together with the value that text means, built
from the same Python data. The expected value therefore never comes from a YAML
library, so it can referee all of them. Text is written by hand-rolled templates, one
per shape, and the random choices come from a fixed generator so the same size gives
the same bytes on every machine and Python version.

A generator is called with a target size in bytes and repeats its template until the
text reaches it, so one shape gives the 1x, 10x and 100x inputs of a scaling check.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

WORDS = ("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi "
         "omicron pi rho sigma tau upsilon phi chi psi omega").split()


class Rng:
    """A 64-bit linear congruential generator: same sequence everywhere."""

    def __init__(self, seed: int = 20260101):
        self.x = seed & 0xFFFFFFFFFFFFFFFF

    def below(self, n: int) -> int:
        self.x = (self.x * 6364136223846793005 + 1442695040888963407) & 0xFFFFFFFFFFFFFFFF
        return (self.x >> 33) % n

    def word(self) -> str:
        return WORDS[self.below(len(WORDS))]


@dataclass
class Generated:
    text: str
    value: object  # the value of the (single) document, or the list of documents
    documents: int = 1
    # Value when merge keys are applied (YAML 1.1 extension), when different.
    merged: object | None = None
    # Edits and patches as {"ops": [...], "subst": [...]}; see workloads.expected_bytes.
    edits: dict | None = None
    patch: dict | None = None
    notes: list[str] = field(default_factory=list)


def _grow(target: int, piece, *, head: str = "") -> tuple[str, list]:
    """Calls piece(i) -> (text, value) until the text reaches target bytes."""
    parts, values, size, i = [head], [], len(head.encode()), 0
    while size < target or i == 0:
        t, v = piece(i)
        parts.append(t)
        values.append(v)
        size += len(t.encode())
        i += 1
    return "".join(parts), values


def _q(s: str) -> str:
    """A double-quoted YAML scalar for text with no control characters."""
    return json.dumps(s, ensure_ascii=False)


# ---- records: typed data, the shape a typed load is for -------------------------------

def records(size: int) -> Generated:
    rng = Rng(1)

    def piece(i: int):
        w1, w2, w3 = rng.word(), rng.word(), rng.word()
        name = f"{w1.capitalize()} {w2.capitalize()} {i}"
        email = f"{w1}.{w2}{i}@example.com"
        active = rng.below(2) == 0
        score = rng.below(8000) / 8 + 0.125
        tags = [rng.word() for _ in range(1 + rng.below(3))]
        zipc = f"{rng.below(100000):05d}"
        lat, lng = rng.below(1440) / 8 - 90, rng.below(2880) / 8 - 180
        note = None if rng.below(3) else f"seen {w3} twice"
        t = (f"- id: {i}\n  name: {name}\n  email: {email}\n"
             f"  active: {'true' if active else 'false'}\n  score: {score!r}\n  tags:\n")
        t += "".join(f"    - {x}\n" for x in tags)
        t += (f"  address:\n    city: {w3.capitalize()}ville\n    zip: '{zipc}'\n"
              f"    geo:\n      lat: {lat!r}\n      lng: {lng!r}\n")
        t += f"  note: {'~' if note is None else _q(note)}\n"
        v = {"id": i, "name": name, "email": email, "active": active, "score": score,
             "tags": tags, "address": {"city": f"{w3.capitalize()}ville", "zip": zipc,
                                       "geo": {"lat": lat, "lng": lng}}, "note": note}
        return t, v

    text, vals = _grow(size, piece)
    n = len(vals)
    assert n >= 5
    # Edits touch records 1..4 only, so any size from 5 records up has the same script.
    rec1, rec2, rec3 = vals[1], vals[2], vals[3]
    edits = {
        "ops": [
            {"op": "replace", "path": "/1/name", "value": "Renamed Person"},
            {"op": "replace", "path": "/2/score", "value": 99.5},
            {"op": "replace", "path": "/2/active", "value": not rec2["active"]},
            {"op": "remove", "path": "/3/address/geo"},
        ],
        "subst": [
            {"find": f"  name: {rec1['name']}\n", "replace": "  name: Renamed Person\n"},
            {"regex": rf"(- id: 2\n(?:.*\n){{3}}  score: ){rec2['score']!r}\n", "replace": r"\g<1>99.5\n"},
            {"regex": rf"(- id: 2\n(?:.*\n){{2}}  active: ){'true' if rec2['active'] else 'false'}\n",
             "replace": rf"\g<1>{'false' if rec2['active'] else 'true'}\n"},
            {"regex": r"(- id: 3\n(?:.*\n)*?    zip: '[0-9]{5}'\n)    geo:\n      lat: [^\n]*\n      lng: [^\n]*\n",
             "replace": r"\g<1>"},
        ],
    }
    tag_last = rec2["tags"][-1]
    patch = {
        "ops": [
            {"op": "test", "path": "/0/id", "value": 0},
            {"op": "replace", "path": "/1/name", "value": "Renamed Person"},
            {"op": "add", "path": "/2/tags/-", "value": "appended"},
            {"op": "remove", "path": "/3/address/geo"},
        ],
        "subst": [
            edits["subst"][0],
            {"regex": rf"(- id: 2\n(?:.*\n)*?    - {tag_last}\n)(  address:)",
             "replace": r"\g<1>    - appended\n\g<2>"},
            edits["subst"][3],
        ],
        "failing": {"op": "test", "path": "/0/id", "value": 1},
    }
    return Generated(text, vals, edits=edits, patch=patch)


# ---- compose with anchors and merge keys ----------------------------------------------

def compose_merge(size: int) -> Generated:
    head = ("x-common: &common\n  restart: unless-stopped\n  logging:\n    driver: json-file\n"
            "    options: {max-size: 10m, max-file: '3'}\n"
            "x-env: &env\n  LOG_LEVEL: info\n  TZ: UTC\nservices:\n")
    common = {"restart": "unless-stopped",
              "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}}
    env = {"LOG_LEVEL": "info", "TZ": "UTC"}

    def piece(i: int):
        port = 8000 + i
        t = (f"  svc{i}:\n    <<: *common\n    image: registry.example.com/app/svc{i}:1.{i % 10}.0\n"
             f"    environment:\n      <<: *env\n      PORT: {port}\n      NAME: svc{i}\n"
             f"    ports: ['{port}:{port}']\n    depends_on: [db]\n")
        base = {"image": f"registry.example.com/app/svc{i}:1.{i % 10}.0", "ports": [f"{port}:{port}"],
                "depends_on": ["db"]}
        lit = {"<<": common, **base, "environment": {"<<": env, "PORT": port, "NAME": f"svc{i}"}}
        mer = {**common, **base, "environment": {**env, "PORT": port, "NAME": f"svc{i}"}}
        return t, (lit, mer)

    text, pairs = _grow(size, piece, head=head)
    svc_l = {f"svc{i}": p[0] for i, p in enumerate(pairs)}
    svc_m = {f"svc{i}": p[1] for i, p in enumerate(pairs)}
    lit = {"x-common": common, "x-env": env, "services": svc_l}
    mer = {"x-common": common, "x-env": env, "services": svc_m}
    edits = {
        "ops": [
            {"op": "replace", "path": "/services/svc0/image", "value": "registry.example.com/app/svc0:2.0.0"},
            {"op": "replace", "path": "/services/svc0/environment/PORT", "value": 9000},
            {"op": "remove", "path": "/services/svc1/depends_on"},
        ],
        "subst": [
            {"find": "image: registry.example.com/app/svc0:1.0.0\n",
             "replace": "image: registry.example.com/app/svc0:2.0.0\n"},
            {"find": "      PORT: 8000\n", "replace": "      PORT: 9000\n"},
            {"regex": r"(  svc1:\n(?:.*\n)*?)    depends_on: \[db\]\n", "replace": r"\g<1>", "count": 1},
        ],
    }
    return Generated(text, lit, merged=mer, edits=edits,
                     notes=["`<<` is an ordinary key in YAML 1.2; the merged tree is the YAML 1.1 extension."])


# ---- multi-document Kubernetes ----------------------------------------------------------

def k8s_multi(size: int) -> Generated:
    def piece(i: int):
        t = (f"---\napiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: web-{i}\n  namespace: prod\n"
             f"  labels:\n    app: web-{i}\n    tier: frontend\nspec:\n  replicas: {1 + i % 5}\n"
             f"  selector:\n    matchLabels:\n      app: web-{i}\n  template:\n    metadata:\n      labels:\n"
             f"        app: web-{i}\n    spec:\n      containers:\n        - name: web\n"
             f"          image: registry.example.com/web:1.{i}.0\n          args: [\"--port\", \"{8000 + i}\"]\n"
             f"          ports:\n            - containerPort: {8000 + i}\n"
             f"          resources:\n            limits: {{cpu: 500m, memory: 128Mi}}\n")
        d = {"apiVersion": "apps/v1", "kind": "Deployment",
             "metadata": {"name": f"web-{i}", "namespace": "prod",
                          "labels": {"app": f"web-{i}", "tier": "frontend"}},
             "spec": {"replicas": 1 + i % 5, "selector": {"matchLabels": {"app": f"web-{i}"}},
                      "template": {"metadata": {"labels": {"app": f"web-{i}"}},
                                   "spec": {"containers": [{
                                       "name": "web", "image": f"registry.example.com/web:1.{i}.0",
                                       "args": ["--port", str(8000 + i)],
                                       "ports": [{"containerPort": 8000 + i}],
                                       "resources": {"limits": {"cpu": "500m", "memory": "128Mi"}}}]}}}}
        t2 = (f"---\napiVersion: v1\nkind: Service\nmetadata:\n  name: web-{i}\nspec:\n  type: ClusterIP\n"
              f"  selector:\n    app: web-{i}\n  ports:\n    - port: 80\n      targetPort: {8000 + i}\n")
        d2 = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": f"web-{i}"},
              "spec": {"type": "ClusterIP", "selector": {"app": f"web-{i}"},
                       "ports": [{"port": 80, "targetPort": 8000 + i}]}}
        cfg = f"level={i}\nname=web-{i}\n"
        t3 = (f"---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: web-{i}-config\ndata:\n"
              f"  app.properties: |\n    level={i}\n    name=web-{i}\n  enabled: \"true\"\n")
        d3 = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": f"web-{i}-config"},
              "data": {"app.properties": cfg, "enabled": "true"}}
        return t + t2 + t3, [d, d2, d3]

    text, groups = _grow(size, piece)
    docs = [d for g in groups for d in g]
    return Generated(text, docs, documents=len(docs))


# ---- OpenAPI ------------------------------------------------------------------------------

def openapi(size: int) -> Generated:
    head = "openapi: 3.0.3\ninfo:\n  title: Generated API\n  version: 1.0.0\npaths:\n"
    mid = "components:\n  schemas:\n"
    paths_t, schemas_t, paths, schemas, total, i = [], [], {}, {}, len(head) + len(mid), 0
    while total < size or i == 0:
        t = (f"  /items{i}:\n    get:\n      summary: List items{i}\n      operationId: listItems{i}\n"
             f"      parameters:\n        - name: limit\n          in: query\n          required: false\n"
             f"          schema:\n            type: integer\n            format: int32\n"
             f"      responses:\n        '200':\n          description: OK\n          content:\n"
             f"            application/json:\n              schema:\n                type: array\n"
             f"                items:\n                  $ref: '#/components/schemas/Item{i}'\n"
             f"        default:\n          description: unexpected error\n")
        paths[f"/items{i}"] = {"get": {
            "summary": f"List items{i}", "operationId": f"listItems{i}",
            "parameters": [{"name": "limit", "in": "query", "required": False,
                            "schema": {"type": "integer", "format": "int32"}}],
            "responses": {"200": {"description": "OK", "content": {"application/json": {"schema": {
                "type": "array", "items": {"$ref": f"#/components/schemas/Item{i}"}}}}},
                          "default": {"description": "unexpected error"}}}}
        s = (f"    Item{i}:\n      type: object\n      required: [id, name]\n      properties:\n"
             f"        id:\n          type: integer\n          format: int64\n"
             f"        name:\n          type: string\n          description: \"Name of item {i}\"\n"
             f"        tags:\n          type: array\n          items: {{type: string}}\n")
        schemas[f"Item{i}"] = {"type": "object", "required": ["id", "name"], "properties": {
            "id": {"type": "integer", "format": "int64"},
            "name": {"type": "string", "description": f"Name of item {i}"},
            "tags": {"type": "array", "items": {"type": "string"}}}}
        paths_t.append(t)
        schemas_t.append(s)
        total += len(t) + len(s)
        i += 1
    text = head + "".join(paths_t) + mid + "".join(schemas_t)
    value = {"openapi": "3.0.3", "info": {"title": "Generated API", "version": "1.0.0"},
             "paths": paths, "components": {"schemas": schemas}}
    edits = {
        "ops": [
            {"op": "replace", "path": "/info/version", "value": "1.1.0"},
            {"op": "replace", "path": "/paths/~1items0/get/summary", "value": "All items0"},
            {"op": "remove", "path": "/paths/~1items0/get/responses/default"},
        ],
        "subst": [
            {"find": "  version: 1.0.0\n", "replace": "  version: 1.1.0\n"},
            {"find": "      summary: List items0\n", "replace": "      summary: All items0\n"},
            {"regex": r"(  /items0:\n(?:.*\n)*?)        default:\n          description: unexpected error\n",
             "replace": r"\g<1>"},
        ],
    }
    return Generated(text, value, edits=edits)


# ---- Helm values: comment-heavy configuration ------------------------------------------

def helm_values(size: int) -> Generated:
    head = ("## Global settings\nglobal:\n  # -- Registry host\n  registry: registry.example.com\n"
            "  # -- Image pull secrets\n  imagePullSecrets: []\n\n")

    def piece(i: int):
        t = (f"svc{i}:\n  # -- Whether svc{i} is enabled\n  enabled: true\n  # -- Number of replicas\n"
             f"  replicaCount: 2\n  image:\n    repository: registry.example.com/svc{i}\n    tag: \"\"\n"
             f"    pullPolicy: IfNotPresent\n  # -- Resource limits\n  resources: {{}}\n"
             f"  #  limits:\n  #    cpu: 100m\n  service:\n    type: ClusterIP\n    port: 80\n"
             f"  # nodeSelector: {{}}\n  annotations: {{}}\n\n")
        v = {"enabled": True, "replicaCount": 2,
             "image": {"repository": f"registry.example.com/svc{i}", "tag": "", "pullPolicy": "IfNotPresent"},
             "resources": {}, "service": {"type": "ClusterIP", "port": 80}, "annotations": {}}
        return t, (f"svc{i}", v)

    text, items = _grow(size, piece, head=head)
    value = {"global": {"registry": "registry.example.com", "imagePullSecrets": []}}
    value.update(dict(items))
    edits = {
        "ops": [
            {"op": "replace", "path": "/svc0/replicaCount", "value": 5},
            {"op": "replace", "path": "/svc0/enabled", "value": False},
            {"op": "replace", "path": "/global/registry", "value": "mirror.example.com"},
        ],
        "subst": [
            {"regex": r"(svc0:\n(?:.*\n)*?  replicaCount: )2\n", "replace": r"\g<1>5\n"},
            {"find": "svc0:\n  # -- Whether svc0 is enabled\n  enabled: true\n",
             "replace": "svc0:\n  # -- Whether svc0 is enabled\n  enabled: false\n"},
            {"find": "  registry: registry.example.com\n", "replace": "  registry: mirror.example.com\n"},
        ],
    }
    return Generated(text, value, edits=edits)


# ---- stress shapes -----------------------------------------------------------------------

def deep_block(size: int) -> Generated:
    depth = max(10, min(100, size // 100))
    text = "".join(f"{' ' * i}k:\n" for i in range(depth)) + f"{' ' * depth}k: 1\n"
    value: object = 1
    for _ in range(depth + 1):
        value = {"k": value}
    return Generated(text, value)


def deep_flow(size: int) -> Generated:
    depth = max(10, min(100, size // 20))
    value: object = 1
    for _ in range(depth):
        value = [value]
    return Generated("[" * depth + "1" + "]" * depth + "\n", value)


def wide_map(size: int) -> Generated:
    def piece(i: int):
        kind = i % 6
        if kind == 0:
            return f"key{i}: {i}\n", (f"key{i}", i)
        if kind == 1:
            return f"key{i}: {i / 8!r}\n", (f"key{i}", i / 8)
        if kind == 2:
            return f"key{i}: {'true' if i % 12 == 2 else 'false'}\n", (f"key{i}", i % 12 == 2)
        if kind == 3:
            return f"key{i}: plain text {i}\n", (f"key{i}", f"plain text {i}")
        if kind == 4:
            return f"key{i}: \"quoted: {i}\"\n", (f"key{i}", f"quoted: {i}")
        return f"key{i}: null\n", (f"key{i}", None)

    text, items = _grow(size, piece)
    edits = {
        "ops": [{"op": "replace", "path": "/key0", "value": 42}, {"op": "remove", "path": "/key3"}],
        "subst": [{"find": "key0: 0\n", "replace": "key0: 42\n"},
                  {"find": "key3: plain text 3\n", "replace": ""}],
    }
    return Generated(text, dict(items), edits=edits)


def long_scalars(size: int) -> Generated:
    # Five values share the size; the divisor allows for the quoting and line structure.
    each = max(200, int(size / 4.2))
    rng = Rng(7)

    def words(n: int) -> list[str]:
        out, total = [], 0
        while total < n:
            w = rng.word()
            out.append(w)
            total += len(w) + 1
        return out

    plain = " ".join(words(each))
    sq_words = words(each)
    sq_words[1] = "it's"
    sq = " ".join(sq_words)
    dq_lines = [" ".join(words(60)) for _ in range(max(1, each // 400))]
    dq = "\n".join(dq_lines)
    lit_lines = [" ".join(words(70)) for _ in range(max(1, each // 70))]
    fold_lines = [" ".join(words(70)) for _ in range(max(1, each // 70))]
    text = (f"plain: {plain}\nsingle: '{sq.replace(chr(39), chr(39) * 2)}'\n"
            f"double: \"{'\\n'.join(dq_lines)}\"\nliteral: |\n" + "".join(f"  {l}\n" for l in lit_lines)
            + "folded: >\n" + "".join(f"  {l}\n" for l in fold_lines))
    value = {"plain": plain, "single": sq, "double": dq, "literal": "\n".join(lit_lines) + "\n",
             "folded": " ".join(fold_lines) + "\n"}
    return Generated(text, value)


def flow_heavy(size: int) -> Generated:
    def piece(i: int):
        a, b = rng.word(), rng.word()
        return (f"- {{id: {i}, tags: [{a}, {b}], ok: {'true' if i % 2 else 'false'}, pos: [{i}, {i * 2}]}}\n",
                {"id": i, "tags": [a, b], "ok": bool(i % 2), "pos": [i, i * 2]})

    rng = Rng(3)
    text, vals = _grow(size, piece)
    return Generated(text, vals)


def anchors(size: int) -> Generated:
    head = "base: &base {a: 1, b: [x, y], c: {d: e}}\nitems:\n"
    base = {"a": 1, "b": ["x", "y"], "c": {"d": "e"}}

    def piece(i: int):
        t = f"  - &n{i} {{id: {i}, ref: *base}}\n  - *n{i}\n  - *base\n"
        d = {"id": i, "ref": base}
        return t, [d, d, base]

    text, groups = _grow(size, piece, head=head)
    return Generated(text, {"base": base, "items": [x for g in groups for x in g]})


def unicode_text(size: int) -> Generated:
    def piece(i: int):
        t = (f"- na\u00efve caf\u00e9 {i}\n- \"\\u00e9\\u4e2d\\u6587 {i}\"\n- \"\\U0001F600 grin {i}\"\n"
             f"- \U0001F642 smile {i}\n- 'Za\u017c\u00f3\u0142\u0107 g\u0119\u015bl\u0105 {i}'\n")
        return t, [f"na\u00efve caf\u00e9 {i}", f"\u00e9\u4e2d\u6587 {i}", f"\U0001F600 grin {i}",
                   f"\U0001F642 smile {i}", f"Za\u017c\u00f3\u0142\u0107 g\u0119\u015bl\u0105 {i}"]

    text, groups = _grow(size, piece)
    return Generated(text, [x for g in groups for x in g])


def scalar_styles(size: int) -> Generated:
    def piece(i: int):
        t = (f"- plain: text {i}\n  single: 'it''s {i}'\n  double: \"tab\\there {i}\"\n  literal: |\n"
             f"    line one {i}\n    line two\n  folded: >\n    folded one {i}\n    folded two\n"
             f"  keep: |+\n    kept\n\n  strip: |-\n    stripped\n")
        v = {"plain": f"text {i}", "single": f"it's {i}", "double": f"tab\there {i}",
             "literal": f"line one {i}\nline two\n", "folded": f"folded one {i} folded two\n",
             "keep": "kept\n\n", "strip": "stripped"}
        return t, v

    text, vals = _grow(size, piece)
    return Generated(text, vals)


SHAPES = {
    "records": records, "compose-merge": compose_merge, "k8s-multi": k8s_multi,
    "openapi": openapi, "helm-values": helm_values, "deep-block": deep_block,
    "deep-flow": deep_flow, "wide-map": wide_map, "long-scalars": long_scalars,
    "flow-heavy": flow_heavy, "anchors": anchors, "unicode": unicode_text,
    "scalar-styles": scalar_styles,
}
