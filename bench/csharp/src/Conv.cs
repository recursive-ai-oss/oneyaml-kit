using System.Collections;
using System.Numerics;
using OneYamlKit.Bench.Models;

namespace OneYamlKit.Bench;

/// <summary>Conversions to the common form. They run for the guard, never under the clock.</summary>
public static class Conv
{
    /// <summary>A library's generic tree (dictionaries, lists and CLR scalars) as a common value. A node
    /// reached twice (an alias kept as a shared reference) is written out each time it is reached.</summary>
    public static J FromObject(object? o)
    {
        switch (o)
        {
            case null: return J.Null.I;
            case bool b: return new J.Bool(b);
            case string s: return new J.Str(s);
            case sbyte or byte or short or ushort or int or uint or long or ulong:
                return new J.Int(new BigInteger(Convert.ToDecimal(o)));
            case BigInteger bi: return new J.Int(bi);
            case double d: return new J.Float(d);
            case float f: return new J.Float(f);
            case decimal m: return new J.Float((double)m);
            case System.Text.Json.JsonElement je: return Libs.Support.FromJsonElement(je);
            case IDictionary dict:
                {
                    var obj = new J.Obj();
                    foreach (DictionaryEntry e in dict) obj.Put(J.KeyText(FromObject(e.Key)), FromObject(e.Value));
                    return obj;
                }
            case IEnumerable seq:
                {
                    var l = new List<J>();
                    foreach (var x in seq) l.Add(FromObject(x));
                    return new J.Arr(l);
                }
            default: throw new InvalidOperationException($"the harness cannot read a {o.GetType().FullName} as a value");
        }
    }

    public static J Docs(List<J> docs) => docs.Count == 1 ? docs[0] : new J.Arr(docs);

    // ---- the typed models, written out the way serde writes them -----------------------------------

    static J Str(string? s) => s == null ? J.Null.I : new J.Str(s);

    static J Strings(IEnumerable<string>? l) => new J.Arr(l == null ? [] : l.Select(x => (J)new J.Str(x)).ToList());

    public static J Records(List<Record> rs) => new J.Arr(rs.Select(RecordJ).ToList());

    static J RecordJ(Record r)
    {
        var o = new J.Obj();
        o.Put("id", new J.Int(r.Id));
        o.Put("name", Str(r.Name));
        o.Put("email", Str(r.Email));
        o.Put("active", new J.Bool(r.Active));
        o.Put("score", new J.Float(r.Score));
        o.Put("tags", Strings(r.Tags));
        var a = new J.Obj();
        a.Put("city", Str(r.Address.City));
        a.Put("zip", Str(r.Address.Zip));
        // An absent geo is left out, as a field that is not there; an absent note is a null.
        if (r.Address.Geo is { } g)
        {
            var go = new J.Obj();
            go.Put("lat", new J.Float(g.Lat));
            go.Put("lng", new J.Float(g.Lng));
            a.Put("geo", go);
        }
        o.Put("address", a);
        o.Put("note", Str(r.Note));
        return o;
    }

    public static J WorkflowJ(Workflow w)
    {
        var o = new J.Obj();
        o.Put("name", Str(w.Name));
        var on = new J.Obj();
        on.Put("pull_request", Str(w.On.PullRequest));
        var push = new J.Obj();
        push.Put("branches", Strings(w.On.Push.Branches));
        on.Put("push", push);
        o.Put("on", on);
        var jobs = new J.Obj();
        foreach (var (k, j) in w.Jobs) jobs.Put(k, JobJ(j));
        o.Put("jobs", jobs);
        return o;
    }

    static J JobJ(Job j)
    {
        var o = new J.Obj();
        o.Put("runs-on", Str(j.RunsOn));
        if (j.Strategy is { } s)
        {
            var m = new J.Obj();
            m.Put("runs-on", Strings(s.Matrix.RunsOn));
            var so = new J.Obj();
            so.Put("matrix", m);
            o.Put("strategy", so);
        }
        if (j.Container != null) o.Put("container", ContainerJ(j.Container));
        if (j.Services != null)
        {
            var sv = new J.Obj();
            foreach (var (k, x) in j.Services)
            {
                var so = new J.Obj();
                so.Put("image", Str(x.Image));
                so.Put("ports", Strings(x.Ports));
                sv.Put(k, so);
            }
            o.Put("services", sv);
        }
        if (j.Env != null)
        {
            var e = new J.Obj();
            foreach (var (k, v) in j.Env) e.Put(k, Str(v));
            o.Put("env", e);
        }
        o.Put("steps", new J.Arr(j.Steps.Select(StepJ).ToList()));
        return o;
    }

    /// <summary>A string is an image name; a mapping has an image and options (null when absent).</summary>
    static J ContainerJ(object c)
    {
        var x = FromObject(c);
        if (x is J.Str) return x;
        var m = x as J.Obj ?? throw new InvalidOperationException("a container is a string or a mapping");
        var o = new J.Obj();
        o.Put("image", m.V.GetValueOrDefault("image") ?? throw new InvalidOperationException("a container mapping has an image"));
        o.Put("options", m.V.GetValueOrDefault("options") ?? J.Null.I);
        return o;
    }

    static J StepJ(Step s)
    {
        var o = new J.Obj();
        if (s.Id != null) o.Put("id", Str(s.Id));
        if (s.Name != null) o.Put("name", Str(s.Name));
        if (s.Condition != null) o.Put("if", Str(s.Condition));
        if (s.Uses != null) o.Put("uses", Str(s.Uses));
        if (s.Run != null) o.Put("run", Str(s.Run));
        if (s.Shell != null) o.Put("shell", Str(s.Shell));
        if (s.With != null)
        {
            var w = new J.Obj();
            foreach (var (k, v) in s.With)
            {
                var x = FromObject(v);
                // Each value is a string or a boolean.
                if (x is not (J.Str or J.Bool)) throw new InvalidOperationException($"`with` value {k} is neither a string nor a boolean");
                w.Put(k, x);
            }
            o.Put("with", w);
        }
        return o;
    }
}
