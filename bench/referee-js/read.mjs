// Reads YAML files with the `yaml` package (YAML 1.2, core schema) and prints each file's value
// as JSON, so the frozen expected values of the real-file workloads can be checked by a parser
// that shares no code or design with the other readers.
//
//   node read.mjs FILE...      ->  {"FILE": {"value": ...} | {"error": "..."}, ...}
//
// The value is the document, or an array of documents when the file holds more than one, as in
// `expected.json`. Integers print without a point and floats with one, so the comparison keeps
// the difference between 1 and 1.0, and negative zero prints as -0.0.
//
// The reader refuses rather than reads loosely: it reports an error for anything that JSON
// cannot hold exactly. That is a float with no JSON form (NaN, infinity), a mapping key that is
// not a scalar, two keys that print to the same text (`1` and `"1"`), a value that is not a
// mapping, sequence or JSON scalar (binary data, a timestamp), and any warning the parser
// raises. The parser reports a tag it cannot resolve (`!!int abc`, `!foo bar`) as a warning
// and reads the text as a string, so a warning is never taken as a reading.
import { readFileSync } from "node:fs";
import { parseAllDocuments } from "yaml";

function scalar(v) {
  switch (typeof v) {
    case "boolean":
      return v ? "true" : "false";
    case "bigint":
      return v.toString();
    case "number": {
      if (!Number.isFinite(v)) throw new Error(`float ${v} has no JSON form`);
      if (Object.is(v, -0)) return "-0.0";
      const s = String(v);
      return /[.eE]/.test(s) ? s : s + ".0";
    }
    case "string":
      return JSON.stringify(v);
  }
  throw new Error(`a ${Object.prototype.toString.call(v)} value has no JSON form`);
}

function keyText(k) {
  if (k === null) return "null";
  if (typeof k === "object") throw new Error("a mapping key that is not a scalar");
  return typeof k === "string" ? k : scalar(k);
}

function json(v) {
  if (v === null) return "null";
  if (Array.isArray(v)) return "[" + v.map(json).join(",") + "]";
  if (v instanceof Map) {
    const seen = new Set();
    const parts = [];
    for (const [k, x] of v) {
      const text = keyText(k);
      if (seen.has(text)) throw new Error(`two keys read as ${JSON.stringify(text)}`);
      seen.add(text);
      parts.push(JSON.stringify(text) + ":" + json(x));
    }
    return "{" + parts.join(",") + "}";
  }
  return scalar(v);
}

function read(path) {
  const text = readFileSync(path, "utf8");
  const docs = parseAllDocuments(text, {
    version: "1.2",
    schema: "core",
    intAsBigInt: true,
    uniqueKeys: true,
  });
  const values = [];
  for (const d of docs) {
    if (d.errors.length) throw new Error(d.errors[0].message);
    if (d.warnings.length) throw new Error(d.warnings[0].message);
    // Maps stay Maps, so keys of different types stay apart until json() has compared their text.
    // Aliases expand in full; the real files are small and have no alias bombs.
    values.push(json(d.toJS({ mapAsMap: true, maxAliasCount: -1 })));
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
