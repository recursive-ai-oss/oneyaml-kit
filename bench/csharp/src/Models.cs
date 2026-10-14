using System.Text.Json.Serialization;
using SharpYaml.Serialization;
using VYaml.Annotations;
using YdnMember = YamlDotNet.Serialization.YamlMemberAttribute;

namespace OneYamlKit.Bench.Models;

// The typed models of the typed-load task: the same fields, in the same shapes, as the models of the
// other languages' harnesses, so the serialised result can be compared with the whole expected value.
//
// Each library reads these classes the way its own documentation says. Property names follow a naming
// convention (lower camel case in all three: Jobs is `jobs`), and the three names that are not just a
// lower-cased property (`runs-on`, `pull_request`, `if`) carry an explicit name for each library. The
// attributes of the libraries that are not reading the class are ignored.
//
// A field that holds either of two shapes (a job's `container` is a string or a mapping, a step's `with`
// value is a string or a boolean) is an `object`: the plain way to take a union without writing a
// converter. The harness reads the object back as a string, a boolean or a mapping.

[YamlObject]
public partial class Record
{
    public long Id { get; set; }
    public string Name { get; set; } = "";
    public string Email { get; set; } = "";
    public bool Active { get; set; }
    public double Score { get; set; }
    public List<string> Tags { get; set; } = [];
    public Address Address { get; set; } = new();
    public string? Note { get; set; }
}

[YamlObject]
public partial class Address
{
    public string City { get; set; } = "";
    public string Zip { get; set; } = "";
    public Geo? Geo { get; set; }
}

[YamlObject]
public partial class Geo
{
    public double Lat { get; set; }
    public double Lng { get; set; }
}

[YamlObject]
public partial class Workflow
{
    public string Name { get; set; } = "";
    public Triggers On { get; set; } = new();
    public Dictionary<string, Job> Jobs { get; set; } = [];
}

[YamlObject]
public partial class Triggers
{
    [YamlMember("pull_request"), YdnMember(ApplyNamingConventions = false, Alias = "pull_request"), JsonPropertyName("pull_request")]
    public string? PullRequest { get; set; }
    public Push Push { get; set; } = new();
}

[YamlObject]
public partial class Push
{
    public List<string> Branches { get; set; } = [];
}

[YamlObject]
public partial class Job
{
    [YamlMember("runs-on"), YdnMember(ApplyNamingConventions = false, Alias = "runs-on"), JsonPropertyName("runs-on")]
    public string RunsOn { get; set; } = "";
    public Strategy? Strategy { get; set; }
    /// <summary>A string (an image name) or a mapping with `image` and `options`.</summary>
    public object? Container { get; set; }
    public Dictionary<string, Service>? Services { get; set; }
    public Dictionary<string, string>? Env { get; set; }
    public List<Step> Steps { get; set; } = [];
}

[YamlObject]
public partial class Strategy
{
    public Matrix Matrix { get; set; } = new();
}

[YamlObject]
public partial class Matrix
{
    [YamlMember("runs-on"), YdnMember(ApplyNamingConventions = false, Alias = "runs-on"), JsonPropertyName("runs-on")]
    public List<string> RunsOn { get; set; } = [];
}

[YamlObject]
public partial class Service
{
    public string Image { get; set; } = "";
    public List<string> Ports { get; set; } = [];
}

[YamlObject]
public partial class Step
{
    public string? Id { get; set; }
    public string? Name { get; set; }
    [YamlMember("if"), YdnMember(ApplyNamingConventions = false, Alias = "if"), JsonPropertyName("if")]
    public string? Condition { get; set; }
    public string? Uses { get; set; }
    public string? Run { get; set; }
    public string? Shell { get; set; }
    /// <summary>Each value is a string or a boolean.</summary>
    public Dictionary<string, object?>? With { get; set; }
}

/// <summary>SharpYaml's source-generated metadata: what its users generate at build time for speed.</summary>
[YamlSourceGenerationOptions(PropertyNamingPolicy = JsonKnownNamingPolicy.CamelCase)]
[YamlSerializable(typeof(Workflow))]
[YamlSerializable(typeof(List<Record>))]
public partial class SharpYamlContext : YamlSerializerContext
{
}
