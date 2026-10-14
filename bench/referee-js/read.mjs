// Reads YAML files with the `yaml` package (YAML 1.2, core schema) and prints each file's value
// as JSON, so the frozen expected values of the real-file workloads can be checked by a parser
// that shares no code or design with the other readers.
//
//   node read.mjs FILE...      ->  {"FILE": {"value": ...} | {"error": "..."}, ...}
//
// The value is the document, or an array of documents when the file holds more than one, as in
// `expected.json`. Integers print without a point and floats with one, so the comparison keeps
// the difference between 1 and 1.0. A float that JSON cannot hold (NaN, infinity) and a key
// that is not a plain scalar are reported as errors rather than guessed at.
import { readFileSync } from "node:fs";
import { parseAllDocuments } from "yaml";

function key(k) {
  if (k === null || typeof k === "object") throw new Error("a mapping key that is not a scalar");
  return typeof k === "string" ? k : String(k);
}

function json(v) {
  if (v === null) return "null";
  switch (typeof v) {
    case "boolean":
      return v ? "true" : "false";
    case "bigint":
      return v.toString();
    case "number": {
      if (!Number.isFinite(v)) throw new Error(`float ${v} has no JSON form`);
      const s = String(v);
      return /[.eE]/.test(s) ? s : s + ".0";
    }
    case "string":
      return JSON.stringify(v);
  }
  if (Array.isArray(v)) return "[" + v.map(json).join(",") + "]";
  if (v instanceof Map) {
    return "{" + [...v].map(([k, x]) => JSON.stringify(key(k)) + ":" + json(x)).join(",") + "}";
  }
  if (v instanceof Date) throw new Error("a timestamp: the core schema reads it as a string");
  return "{" + Object.entries(v).map(([k, x]) => JSON.stringify(k) + ":" + json(x)).join(",") + "}";
}

function read(path) {
  const text = readFileSync(path, "utf8");
  const docs = parseAllDocuments(text, {
    version: "1.2",
    schema: "core",
    intAsBigInt: true,
    mapAsMap: true,
    uniqueKeys: true,
    // Aliases expand to their full value; the real files are small and have no alias bombs.
    maxAliasCount: -1,
  });
  const values = [];
  for (const d of docs) {
    if (d.errors.length) throw new Error(d.errors[0].message);
    values.push(json(d.toJS()));
  }
  return values.length === 1 ? values[0] : "[" + values.join(",") + "]";
}

const out = [];
for (const path of process.argv.slice(2)) {
  let entry;
  try {
    entry = `{"value":${read(path)}}`;
  } catch (e) {
    entry = `{"error":${JSON.stringify(String(e.message))}}`;
  }
  out.push(JSON.stringify(path) + ":" + entry);
}
console.log("{" + out.join(",") + "}");
