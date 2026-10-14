"""The workload set: real-world files, generated shapes, the reference edit cases.

A workload is a YAML input plus what is needed to judge and time each task on it. They
are materialised into a directory (`prepare`) that every language's harness reads, so
the Rust, Go and .NET harnesses all measure identical bytes against identical
expectations.

Materialised layout, one directory per workload under `<out>/workloads/`:

    input.yaml            the exact input bytes
    workload.json         id, kind, description, source, documents, tasks, sizes
    expected.json         the value of the input (a list of documents when there are several)
    expected.merged.json  the value with `<<` merge keys applied (when it differs)
    edit.json             the ops of the edit-script task
    edit.expected.yaml    the exact bytes after those edits
    patch.json            an RFC 6902 patch (ops), and the same patch with a failing step
    patch.expected.yaml   the exact bytes after the patch

`<out>/workloads/index.json` lists the workloads in order.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import gen

ROOT = Path(__file__).resolve().parents[1]
REAL_DIR = ROOT / "workloads" / "real"

TASKS = ("typed-load", "generic-load", "dump", "document-parse", "unchanged-emit",
         "edit-script", "patch")

KINDS = ("real-world", "generated", "reference")


class WorkloadError(Exception):
    pass


def apply_subst(text: str, substs: list[dict], what: str = "workload") -> str:
    """Applies the textual substitutions that spell out the exact expected bytes.

    The expected bytes of an edit are written as plain text edits of the input, never
    by running a YAML library, so a library cannot define its own expectation. Each
    substitution must match exactly once (or `count` times), so a stale one fails loudly.

    """
    for s in substs:
        want = s.get("count", 1)
        if "find" in s:
            n = text.count(s["find"])
            if n != want:
                raise WorkloadError(f"{what}: {s['find']!r} matches {n} times, expected {want}")
            text = text.replace(s["find"], s["replace"])
        else:
            text, n = re.subn(s["regex"], s["replace"], text)
            if n != want:
                raise WorkloadError(f"{what}: {s['regex']!r} matches {n} times, expected {want}")
    return text


def _norm_ops(ops: list[dict]) -> list[dict]:
    return [dict(o) for o in ops]


# ---- real-world files -------------------------------------------------------------------

REAL_EDITS = {
    "gha-workflow": {
        "edits": {
            "ops": [
                {"op": "replace", "path": "/name", "value": "Build and Test (renamed)"},
                {"op": "replace", "path": "/jobs/build/runs-on", "value": "ubuntu-24.04"},
                {"op": "replace", "path": "/jobs/build/steps/0/with/node-version", "value": "26.x"},
                {"op": "remove", "path": "/jobs/build/steps/1"},
            ],
            "subst": [
                {"find": "name: Build and Test\n", "replace": "name: Build and Test (renamed)\n"},
                {"find": "  build:\n    runs-on: ubuntu-latest\n", "replace": "  build:\n    runs-on: ubuntu-24.04\n"},
                {"find": "          node-version: 24.x\n", "replace": "          node-version: 26.x\n"},
                {"find": "      - uses: actions/checkout@v7\n      - run: npm ci\n", "replace": "      - run: npm ci\n"},
            ],
        },
        "patch": {
            "ops": [
                {"op": "test", "path": "/name", "value": "Build and Test"},
                {"op": "replace", "path": "/jobs/build/runs-on", "value": "ubuntu-24.04"},
                {"op": "add", "path": "/on/push/branches/-", "value": "develop"},
                {"op": "remove", "path": "/jobs/build/steps/1"},
            ],
            "subst": [
                {"find": "  build:\n    runs-on: ubuntu-latest\n", "replace": "  build:\n    runs-on: ubuntu-24.04\n"},
                {"find": "      - releases/*\n", "replace": "      - releases/*\n      - develop\n"},
                {"find": "      - uses: actions/checkout@v7\n      - run: npm ci\n", "replace": "      - run: npm ci\n"},
            ],
            "failing": {"op": "test", "path": "/name", "value": "Not the name"},
        },
    },
    "compose-wordpress": {
        "edits": {
            "ops": [
                {"op": "replace", "path": "/services/db/image", "value": "mariadb:11.4"},
                {"op": "replace", "path": "/services/db/command", "value": "--skip-networking"},
                {"op": "remove", "path": "/services/db/restart"},
                {"op": "replace", "path": "/services/wordpress/ports/0", "value": "8080:80"},
            ],
            "subst": [
                {"find": "image: mariadb:10.6.4-focal\n", "replace": "image: mariadb:11.4\n"},
                {"find": "    command: '--default-authentication-plugin=mysql_native_password'\n",
                 "replace": "    command: '--skip-networking'\n"},
                {"find": "    volumes:\n      - db_data:/var/lib/mysql\n    restart: always\n",
                 "replace": "    volumes:\n      - db_data:/var/lib/mysql\n"},
                {"find": "      - 80:80\n", "replace": "      - 8080:80\n"},
            ],
        },
        "patch": {
            "ops": [
                {"op": "test", "path": "/services/db/image", "value": "mariadb:10.6.4-focal"},
                {"op": "replace", "path": "/services/db/image", "value": "mariadb:11.4"},
                {"op": "add", "path": "/services/db/environment/-", "value": "MYSQL_CHARSET=utf8mb4"},
                {"op": "add", "path": "/services/wordpress/container_name", "value": "wp"},
            ],
            "subst": [
                {"find": "image: mariadb:10.6.4-focal\n", "replace": "image: mariadb:11.4\n"},
                {"find": "      - MYSQL_PASSWORD=wordpress\n",
                 "replace": "      - MYSQL_PASSWORD=wordpress\n      - MYSQL_CHARSET=utf8mb4\n"},
                {"find": "      - WORDPRESS_DB_NAME=wordpress\n",
                 "replace": "      - WORDPRESS_DB_NAME=wordpress\n    container_name: wp\n"},
            ],
            "failing": {"op": "test", "path": "/services/db/image", "value": "mysql:8"},
        },
    },
    "helm-ingress-nginx": {
        "edits": {
            "ops": [
                {"op": "replace", "path": "/controller/replicaCount", "value": 3},
                {"op": "replace", "path": "/controller/dnsPolicy", "value": "ClusterFirstWithHostNet"},
                {"op": "replace", "path": "/controller/hostNetwork", "value": True},
                {"op": "replace", "path": "/controller/allowSnippetAnnotations", "value": True},
            ],
            "subst": [
                {"regex": r"(?m)(^controller:\n(?:.*\n)*?  podAnnotations: \{\}\n  replicaCount: )1\n", "replace": r"\g<1>3\n"},
                {"find": "  dnsPolicy: ClusterFirst\n", "replace": "  dnsPolicy: ClusterFirstWithHostNet\n"},
                {"find": "  hostNetwork: false\n", "replace": "  hostNetwork: true\n"},
                {"find": "  allowSnippetAnnotations: false\n", "replace": "  allowSnippetAnnotations: true\n"},
            ],
        },
    },
    "openapi-petstore": {
        "edits": {
            "ops": [
                {"op": "replace", "path": "/info/version", "value": "1.1.0"},
                {"op": "replace", "path": "/servers/0/url", "value": "https://petstore.example.com/v3"},
                {"op": "remove", "path": "/info/contact"},
                {"op": "replace", "path": "/paths/~1pets/get/description",
                 "value": "Returns all pets\nfrom the system\n"},
            ],
            "subst": [
                {"find": "  version: 1.0.0\n", "replace": "  version: 1.1.0\n"},
                {"find": "  - url: https://petstore.swagger.io/v2\n", "replace": "  - url: https://petstore.example.com/v3\n"},
                {"find": "  contact:\n    name: Swagger API Team\n    email: apiteam@swagger.io\n    url: http://swagger.io\n",
                 "replace": ""},
                {"regex": r"(      description: \|\n)(?:(?:        .*)?\n)+?(      operationId: findPets\n)",
                 "replace": r"\g<1>        Returns all pets\n        from the system\n\g<2>"},
            ],
        },
    },
    "openapi-api-with-examples": {
        "edits": {
            "ops": [{"op": "replace", "path": "/info/version", "value": "2.1.0"},
                    {"op": "replace", "path": "/info/title", "value": "Simple API"}],
            "subst": [{"find": "  version: 2.0.0\n", "replace": "  version: 2.1.0\n"},
                      {"find": "  title: Simple API overview\n", "replace": "  title: Simple API\n"}],
        },
    },
    "prometheus-config": {
        "edits": {
            "ops": [
                {"op": "replace", "path": "/global/scrape_interval", "value": "30s"},
                {"op": "replace", "path": "/scrape_configs/0/job_name", "value": "self"},
            ],
            "subst": [
                {"find": "  scrape_interval: 15s #", "replace": "  scrape_interval: 30s #"},
                {"find": "  - job_name: \"prometheus\"\n", "replace": "  - job_name: \"self\"\n"},
            ],
        },
    },
}

# Real files are single documents unless stated.
REAL_EXTRA = {
    "gha-workflow": {"typed": "gha"},
}


def source_url(src: dict) -> str:
    """Where the file can be read, built from the pinned revision rather than stored."""
    return f"{src['repository']}/blob/{src['revision']}/{src['path']}"


def load_real() -> list[dict]:
    out = []
    for d in sorted(REAL_DIR.iterdir()):
        if not (d / "workload.json").is_file():
            continue  # LICENSES/ and the sources page
        meta = json.loads((d / "workload.json").read_text(encoding="utf-8"))
        meta["_dir"] = d
        out.append(meta)
    return out


# ---- reference edit cases -----------------------------------------------------------------

REF_INPUT = "#hello\na: 10\n\n#abc 123\nb: |\n  this is a test\nc: 13\n"


def reference_a() -> gen.Generated:
    ops = [{"op": "replace", "path": "/b", "value": "this is a test2\n"}]
    sub = [{"find": "  this is a test\n", "replace": "  this is a test2\n"}]
    return gen.Generated(REF_INPUT, {"a": 10, "b": "this is a test\n", "c": 13},
                         edits={"ops": ops, "subst": sub}, patch={"ops": ops, "subst": sub,
                         "failing": {"op": "test", "path": "/a", "value": 11}})


def reference_b() -> gen.Generated:
    ops = [{"op": "replace", "path": "/b", "value": "this is a test2"}]
    sub = [{"find": "b: |\n  this is a test\n", "replace": "b: |-\n  this is a test2\n"}]
    return gen.Generated(REF_INPUT, {"a": 10, "b": "this is a test\n", "c": 13},
                         edits={"ops": ops, "subst": sub}, patch={"ops": ops, "subst": sub,
                         "failing": {"op": "test", "path": "/a", "value": 11}})


# ---- the generated set -------------------------------------------------------------------

KB, MB = 1024, 1024 * 1024
# (shape, size in bytes, tier). The id is "<shape>-<size label>".
GENERATED = [
    ("records", 4 * KB, "quick"), ("records", 64 * KB, "quick"),
    ("records", 1 * MB, "full"), ("records", 8 * MB, "full"),
    ("compose-merge", 2 * KB, "quick"), ("compose-merge", 64 * KB, "quick"),
    ("compose-merge", 1 * MB, "full"),
    ("k8s-multi", 16 * KB, "quick"), ("k8s-multi", 1 * MB, "full"),
    ("openapi", 100 * KB, "quick"), ("openapi", 8 * MB, "full"),
    ("helm-values", 64 * KB, "quick"), ("helm-values", 1 * MB, "full"),
    ("deep-block", 10 * KB, "quick"), ("deep-flow", 10 * KB, "quick"),
    ("wide-map", 64 * KB, "quick"), ("wide-map", 1 * MB, "full"),
    ("long-scalars", 256 * KB, "quick"), ("flow-heavy", 64 * KB, "quick"),
    ("anchors", 32 * KB, "quick"), ("unicode", 32 * KB, "quick"),
    ("scalar-styles", 32 * KB, "quick"),
]
SHAPE_ABOUT = {
    "records": "A list of typed records (numbers, booleans, strings, nested maps, nulls). Typed load and scaling.",
    "compose-merge": "Compose-style services sharing settings through anchors and `<<` merge keys.",
    "k8s-multi": "Many `---` separated Kubernetes documents (Deployment, Service, ConfigMap with a literal block).",
    "openapi": "An OpenAPI description: paths, schemas, `$ref` strings.",
    "helm-values": "Helm-style values: comment runs, commented-out blocks, empty `{}` and `[]`.",
    "deep-block": "One chain of nested block mappings, 100 levels deep at most.",
    "deep-flow": "Nested flow sequences, 100 levels deep at most.",
    "wide-map": "One flat mapping with many keys and mixed scalar types.",
    "long-scalars": "Very long plain, quoted, literal and folded scalars.",
    "flow-heavy": "A sequence of flow mappings and flow sequences.",
    "anchors": "Many anchors and aliases of small collections (no nesting that multiplies).",
    "unicode": "Non-ASCII text, escapes in double quotes, characters outside the BMP.",
    "scalar-styles": "Every scalar style: plain, single, double, literal, folded, keep and strip chomping.",
}
def size_label(n: int) -> str:
    return f"{n // MB}m" if n >= MB and n % MB == 0 else f"{n // KB}k"


def generated_workloads(tiers: tuple[str, ...]) -> list[dict]:
    out = []
    for shape, size, tier in GENERATED:
        if tier not in tiers:
            continue
        wid = shape if shape.startswith("deep-") else f"{shape}-{size_label(size)}"
        out.append({"id": wid, "shape": shape, "size": size, "tier": tier})
    return out


def _tasks_for(g: gen.Generated, *, typed: str | None, dump: bool) -> list[str]:
    t = []
    if typed:
        t.append(f"typed-load")
    t += ["generic-load"]
    if dump:
        t.append("dump")
    t += ["document-parse", "unchanged-emit"]
    if g.edits:
        t.append("edit-script")
    if g.patch:
        t.append("patch")
    return t


def _write(path: Path, data: bytes) -> None:
    path.write_bytes(data)


def _dump_json(v) -> bytes:
    return (json.dumps(v, ensure_ascii=False, sort_keys=False, separators=(",", ":")) + "\n").encode("utf-8")


def _emit(dest: Path, wid: str, kind: str, description: str, source: dict, text: bytes,
          g: gen.Generated, typed: str | None, tier: str, dump: bool) -> dict:
    dest.mkdir(parents=True, exist_ok=True)
    _write(dest / "input.yaml", text)
    _write(dest / "expected.json", _dump_json(g.value))
    if g.merged is not None:
        _write(dest / "expected.merged.json", _dump_json(g.merged))
    textstr = text.decode("utf-8")
    if g.edits:
        _write(dest / "edit.json", _dump_json({"ops": _norm_ops(g.edits["ops"])}))
        _write(dest / "edit.expected.yaml",
               apply_subst(textstr, g.edits["subst"], f"{wid} edits").encode("utf-8"))
    if g.patch:
        failing = _norm_ops(g.patch["ops"]) + [g.patch["failing"]]
        _write(dest / "patch.json", _dump_json({"ops": _norm_ops(g.patch["ops"]), "failing_ops": failing}))
        _write(dest / "patch.expected.yaml",
               apply_subst(textstr, g.patch["subst"], f"{wid} patch").encode("utf-8"))
    meta = {
        "id": wid, "kind": kind, "tier": tier, "description": description, "source": source,
        "documents": g.documents, "input_bytes": len(text),
        "input_sha256": hashlib.sha256(text).hexdigest(),
        "tasks": _tasks_for(g, typed=typed, dump=dump),
        "typed_model": typed, "merge_variants": g.merged is not None,
        "notes": g.notes,
    }
    _write(dest / "workload.json", (json.dumps(meta, indent=1) + "\n").encode("utf-8"))
    return meta


def universe(tiers: tuple[str, ...] = ("quick", "full")) -> dict[str, list[str]]:
    """Every workload id of the tiers with the tasks it defines, without writing anything.

    The known-deviation lists are checked against this, so an entry that names a workload
    or task that no longer exists is found even when the run was filtered or in quick mode.
    Small instances stand in for the large ones: which tasks exist does not depend on size.
    """
    out: dict[str, list[str]] = {}
    for m in load_real():
        if m.get("tier", "quick") not in tiers:
            continue
        extra = REAL_EDITS.get(m["id"], {})
        g = gen.Generated("", None, documents=m["documents"], edits=extra.get("edits"), patch=extra.get("patch"))
        out[m["id"]] = _tasks_for(g, typed=REAL_EXTRA.get(m["id"], {}).get("typed"), dump=m["documents"] == 1)
    for ref_id, fn in (("reference-a", reference_a), ("reference-b", reference_b)):
        out[ref_id] = _tasks_for(fn(), typed=None, dump=True)
    for w in generated_workloads(tiers):
        g = gen.SHAPES[w["shape"]](4096)
        out[w["id"]] = _tasks_for(g, typed="records" if w["shape"] == "records" else None, dump=g.documents == 1)
    return out


def prepare(out_dir: Path, tiers: tuple[str, ...] = ("quick",)) -> list[dict]:
    """Writes every workload of the given tiers under out_dir/workloads and returns the metas."""
    root = out_dir / "workloads"
    root.mkdir(parents=True, exist_ok=True)
    metas = []
    for m in load_real():
        d = m["_dir"]
        if m.get("tier", "quick") not in tiers:
            continue
        text = (d / "input.yaml").read_bytes()
        m["source"] = dict(m["source"], url=source_url(m["source"]))
        value = json.loads((d / "expected.json").read_text(encoding="utf-8"))
        extra = REAL_EDITS.get(m["id"], {})
        g = gen.Generated(text.decode("utf-8"), value, documents=m["documents"],
                          edits=extra.get("edits"), patch=extra.get("patch"))
        metas.append(_emit(root / m["id"], m["id"], "real-world", m["description"], m["source"],
                           text, g, REAL_EXTRA.get(m["id"], {}).get("typed"), m.get("tier", "quick"),
                           dump=m["documents"] == 1))
    for ref_id, fn, about in (
            ("reference-a", reference_a, "Setting a block literal to text that ends in a newline keeps `|`."),
            ("reference-b", reference_b, "Setting a block literal to text without a final newline changes the header to `|-` and nothing else.")):
        g = fn()
        metas.append(_emit(root / ref_id, ref_id, "reference", about,
                           {"origin": "requirement", "licence": "CC0-1.0"}, g.text.encode("utf-8"),
                           g, None, "quick", dump=True))
    for w in generated_workloads(tiers):
        g = gen.SHAPES[w["shape"]](w["size"])
        about = SHAPE_ABOUT[w["shape"]]
        typed = "records" if w["shape"] == "records" else None
        metas.append(_emit(root / w["id"], w["id"], "generated", about,
                           {"origin": "generated", "generator": f"bench/oneyaml_bench/gen.py:{w['shape']}",
                            "target_bytes": w["size"], "licence": "CC0-1.0"},
                           g.text.encode("utf-8"), g, typed, w["tier"],
                           dump=g.documents == 1))
    index = {"format": "oneyaml-kit-workloads/1",
             "workloads": [{"id": m["id"], "kind": m["kind"], "tier": m["tier"],
                            "input_bytes": m["input_bytes"], "tasks": m["tasks"]} for m in metas]}
    (root / "index.json").write_text(json.dumps(index, indent=1) + "\n", encoding="utf-8")
    return metas
